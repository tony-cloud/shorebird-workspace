// Code-entry identity analysis and link table computation.
//
// A patch code entry P may run as release code entry B only if
//  1. P and B have identical instruction bytes after clearing the
//     displacement of branches that leave the entry (B/BL to other entries),
//  2. neither contains any other reference relative to its own address that
//     leaves the entry (ADR/ADRP/literal loads/conditional branches), and
//  3. every branch leaving P targets entry P' at offset o, the matching
//     branch in B targets entry B' at the same offset o, and P' is itself
//     linked to B' (greatest fixpoint, so mutually recursive groups link).
// Everything else that the code depends on (object pool slots, field and
// class ids, dispatch table offsets, static field ids, thread offsets) is
// encoded in the instruction bytes and is resolved at run time through the
// patch's own tables, so byte equality makes B behave exactly like P.
// See docs/ios-linker/COMPILER_LINKER.md.

import 'dart:typed_data';

import 'arm64.dart';
import 'hash.dart';
import 'link_info.dart';

class ResolvedCall {
  ResolvedCall(this.instructionOffset, this.target, this.offsetIntoTarget,
      this.isCall);
  final int instructionOffset;
  final int target; // index into Snapshot.entries
  final int offsetIntoTarget;
  final bool isCall; // BL (true) or B (tail call)
}

class EntryAnalysis {
  EntryAnalysis(this.entry);
  final CodeEntry entry;
  late Uint8List normalized;
  late int selfHash;
  final List<ResolvedCall> calls = [];

  /// Non-null if the entry has a PC-relative reference that cannot be
  /// normalized (it can never run as release code).
  String? unlinkableReason;
  int subgraphHash = 0;
}

class Snapshot {
  Snapshot(this.info, this.text) {
    for (final e in info.codes) {
      entries.add(EntryAnalysis(e));
    }
    for (var i = 0; i < entries.length; i++) {
      indexByOffset[entries[i].entry.textOffset] = i;
      byKey.putIfAbsent(entries[i].entry.key, () => []).add(i);
    }
    for (final a in entries) {
      _analyze(a);
    }
    _computeSubgraphHashes();
  }

  final LinkInfo info;
  final Uint8List text;
  final List<EntryAnalysis> entries = [];
  final Map<int, int> indexByOffset = {};
  final Map<String, List<int>> byKey = {};

  int? indexAt(int textOffset) {
    final e = info.entryAt(textOffset);
    return e == null ? null : indexByOffset[e.textOffset];
  }

  void _analyze(EntryAnalysis a) {
    final e = a.entry;
    if (e.end > text.length) {
      a.unlinkableReason = 'outside-image';
      a.normalized = Uint8List(0);
      a.selfHash = 0;
      return;
    }
    final bytes = Uint8List.fromList(text.sublist(e.textOffset, e.end));
    final bd = ByteData.sublistView(bytes);
    final recorded = <int>{
      for (final c in e.calls)
        if (c.isPcRelative) c.instructionOffset
    };
    for (var off = 0; off + 4 <= bytes.length; off += 4) {
      final insn = bd.getUint32(off, Endian.little);
      final r = decodePcRel(insn);
      if (r == null) continue;
      if (r.kind == PcRelKind.adrp) {
        a.unlinkableReason ??= 'adrp@$off';
        continue;
      }
      final target = e.textOffset + off + r.delta;
      if (target >= e.textOffset && target < e.end) continue; // internal
      if (r.kind == PcRelKind.b || r.kind == PcRelKind.bl) {
        final t = indexAt(target);
        if (t == null) {
          a.unlinkableReason ??= 'branch-outside-code@$off';
          continue;
        }
        if (!recorded.contains(off)) {
          // An external branch the compiler did not record as a call. Data
          // words can decode as branches; be conservative.
          a.unlinkableReason ??= 'unrecorded-branch@$off';
        }
        a.calls.add(ResolvedCall(off, t,
            target - entries[t].entry.textOffset, r.kind == PcRelKind.bl));
        bd.setUint32(off, clearBranchImm(insn), Endian.little);
      } else {
        a.unlinkableReason ??= '${r.kind.name}-outside@$off';
      }
    }
    a.normalized = bytes;
    var h = fnv64(bytes);
    for (final c in a.calls) {
      h = fnvInt(h, c.instructionOffset);
      h = fnvInt(h, c.offsetIntoTarget);
    }
    a.selfHash = h;
  }

  // Subgraph hash: self hash combined with the subgraph hashes of everything
  // reachable through branches leaving the entry. Strongly connected
  // components are hashed as a unit. Reported for diagnostics; the link
  // decision uses the pairwise fixpoint, which is at least as strict.
  void _computeSubgraphHashes() {
    final n = entries.length;
    final index = List<int>.filled(n, -1);
    final low = List<int>.filled(n, 0);
    final onStack = List<bool>.filled(n, false);
    final stack = <int>[];
    var counter = 0;
    final sccOf = List<int>.filled(n, -1);
    final sccs = <List<int>>[];

    // Iterative Tarjan.
    for (var root = 0; root < n; root++) {
      if (index[root] != -1) continue;
      final work = <List<int>>[
        [root, 0]
      ];
      while (work.isNotEmpty) {
        final frame = work.last;
        final v = frame[0];
        if (frame[1] == 0 && index[v] == -1) {
          index[v] = low[v] = counter++;
          stack.add(v);
          onStack[v] = true;
        }
        final calls = entries[v].calls;
        if (frame[1] < calls.length) {
          final w = calls[frame[1]++].target;
          if (index[w] == -1) {
            work.add([w, 0]);
          } else if (onStack[w]) {
            low[v] = low[v] < index[w] ? low[v] : index[w];
          }
          continue;
        }
        if (low[v] == index[v]) {
          final scc = <int>[];
          int w;
          do {
            w = stack.removeLast();
            onStack[w] = false;
            sccOf[w] = sccs.length;
            scc.add(w);
          } while (w != v);
          sccs.add(scc);
        }
        work.removeLast();
        if (work.isNotEmpty) {
          final p = work.last[0];
          low[p] = low[p] < low[v] ? low[p] : low[v];
        }
      }
    }
    // Tarjan emits SCCs in reverse topological order (callees first).
    final sccHash = List<int>.filled(sccs.length, 0);
    for (var s = 0; s < sccs.length; s++) {
      final members = sccs[s];
      final selfs = members.map((m) => entries[m].selfHash).toList()..sort();
      var h = 0xcbf29ce484222325;
      for (final x in selfs) {
        h = fnvInt(h, x);
      }
      final external = <int>[];
      for (final m in members) {
        for (final c in entries[m].calls) {
          if (sccOf[c.target] != s) external.add(sccHash[sccOf[c.target]]);
        }
      }
      external.sort();
      for (final x in external) {
        h = fnvInt(h, x);
      }
      sccHash[s] = h;
    }
    for (var i = 0; i < n; i++) {
      entries[i].subgraphHash = sccHash[sccOf[i]];
    }
  }
}

bool _bytesEqual(Uint8List a, Uint8List b) {
  if (a.length != b.length) return false;
  for (var i = 0; i < a.length; i++) {
    if (a[i] != b[i]) return false;
  }
  return true;
}

class LinkResult {
  /// patch entry index -> base entry index.
  final Map<int, int> links = {};

  /// patch entry index -> reason it is not linked.
  final Map<int, String> unlinked = {};
  int patchCodeBytes = 0;
  int linkedCodeBytes = 0;
  int subgraphHashAgreements = 0;

  double get linkPercent =>
      patchCodeBytes == 0 ? 0 : 100.0 * linkedCodeBytes / patchCodeBytes;
}

LinkResult computeLinks(Snapshot base, Snapshot patch) {
  final result = LinkResult();
  final candidate = <int, int>{};
  final baseBySelf = <int, List<int>>{};
  for (var i = 0; i < base.entries.length; i++) {
    baseBySelf.putIfAbsent(base.entries[i].selfHash, () => []).add(i);
  }

  for (var p = 0; p < patch.entries.length; p++) {
    final pa = patch.entries[p];
    if (pa.unlinkableReason != null) {
      result.unlinked[p] = 'patch-${pa.unlinkableReason}';
      continue;
    }
    int? b;
    final sameKey = base.byKey[pa.entry.key];
    final patchSameKey = patch.byKey[pa.entry.key];
    if (sameKey != null && sameKey.length == 1 && patchSameKey!.length == 1) {
      b = sameKey.single;
    } else {
      // Identity unknown or ambiguous: pair by content if unambiguous.
      final same = baseBySelf[pa.selfHash];
      if (same != null && same.length == 1) b = same.single;
    }
    if (b == null) {
      result.unlinked[p] = sameKey == null ? 'new' : 'ambiguous-key';
      continue;
    }
    final ba = base.entries[b];
    if (ba.unlinkableReason != null) {
      result.unlinked[p] = 'base-${ba.unlinkableReason}';
      continue;
    }
    if (!_bytesEqual(pa.normalized, ba.normalized) ||
        pa.calls.length != ba.calls.length) {
      result.unlinked[p] = 'code-differs';
      continue;
    }
    var shapeOk = true;
    for (var k = 0; k < pa.calls.length; k++) {
      final pc = pa.calls[k], bc = ba.calls[k];
      if (pc.instructionOffset != bc.instructionOffset ||
          pc.offsetIntoTarget != bc.offsetIntoTarget ||
          pc.isCall != bc.isCall) {
        shapeOk = false;
        break;
      }
    }
    if (!shapeOk) {
      result.unlinked[p] = 'call-shape-differs';
      continue;
    }
    candidate[p] = b;
  }

  // Greatest fixpoint: drop candidates whose callees are not linked to the
  // corresponding release callees.
  var changed = true;
  while (changed) {
    changed = false;
    final drop = <int>[];
    candidate.forEach((p, b) {
      final pa = patch.entries[p], ba = base.entries[b];
      for (var k = 0; k < pa.calls.length; k++) {
        if (candidate[pa.calls[k].target] != ba.calls[k].target) {
          drop.add(p);
          return;
        }
      }
    });
    for (final p in drop) {
      candidate.remove(p);
      result.unlinked[p] = 'callee-not-linked';
      changed = true;
    }
  }

  // Two patch entries must not share a release entry unless they are the
  // same code (deduplicated instructions are separate entries only once).
  result.links.addAll(candidate);
  for (var p = 0; p < patch.entries.length; p++) {
    final size = patch.entries[p].entry.size;
    result.patchCodeBytes += size;
    final b = result.links[p];
    if (b != null) {
      result.linkedCodeBytes += size;
      if (patch.entries[p].subgraphHash == base.entries[b].subgraphHash) {
        result.subgraphHashAgreements++;
      }
    }
  }
  return result;
}
