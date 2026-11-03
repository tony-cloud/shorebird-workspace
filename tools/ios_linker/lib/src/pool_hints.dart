// Second-pass object pool assignment.
//
// The first patch compilation pins every pool entry whose key the release
// also has to the release's slot. Entries that are new to the patch (a
// changed string, a new call site) are appended, which changes the code of
// every function using them even if that function only swapped one
// constant. This pass looks at functions present in both builds whose
// sequences of pool requests have the same length, and assigns a new patch
// entry to the release slot used at the same position when that slot is
// unused by the first patch build and holds the same kind of entry. The
// patch is then recompiled with these extra assignments. Correctness never
// depends on this: release code is only reused if its bytes are identical
// to the patch's, and it always runs against the patch's pool.

import 'link_info.dart';

class PoolHints {
  PoolHints(this.lines, this.assigned, this.considered);
  final List<String> lines;
  final int assigned;
  final int considered;
}

PoolHints computePoolHints(LinkInfo base, LinkInfo pass1) {
  final basePool = {for (final e in base.pool) e.index: e};
  final patchPool = {for (final e in pass1.pool) e.index: e};
  final baseLength = base.meta['pool_length'] ?? base.pool.length;
  final stubLength = base.meta['stub_pool_length'] ?? 0;

  // Release slots the first patch build left empty (filler immediates).
  bool isHole(int index) {
    if (index < stubLength || index >= baseLength) return false;
    final e = patchPool[index];
    return e == null || (e.type == 0 && e.key.isEmpty);
  }

  final taken = <int>{};
  final assignedKeys = <String>{};
  final lines = <String>[];
  var considered = 0;

  void assign(String key, int index, PoolEntry be) {
    taken.add(index);
    assignedKeys.add(key);
    lines.add('pool\t$index\t${be.type}\t${be.patchable ? 0 : 1}\t$key');
  }

  final keys = base.uses.keys.where(pass1.uses.containsKey).toList()..sort();
  for (final fn in keys) {
    final bu = base.uses[fn]!, pu = pass1.uses[fn]!;
    if (bu.length != pu.length) continue;
    for (var j = 0; j < bu.length; j++) {
      final b = bu[j], p = pu[j];
      if (b == p) continue;
      considered++;
      if (p < baseLength) continue; // already placed in the release range
      final pe = patchPool[p], be = basePool[b];
      if (pe == null || be == null || pe.key.isEmpty) continue;
      if (pe.type != be.type || pe.patchable != be.patchable) continue;
      if (!isHole(b) || taken.contains(b) || assignedKeys.contains(pe.key)) {
        continue;
      }
      if (pe.patchable) {
        // Pairs: both halves must move together.
        if (j + 1 >= bu.length || bu[j + 1] != b + 1 || pu[j + 1] != p + 1) {
          continue;
        }
        final pe2 = patchPool[p + 1], be2 = basePool[b + 1];
        if (pe2 == null || be2 == null || !isHole(b + 1)) continue;
        assign(pe.key, b, be);
        assign(pe2.key, b + 1, be2);
        j++;
        continue;
      }
      assign(pe.key, b, be);
    }
  }
  return PoolHints(lines, lines.length, considered);
}

/// The release link info with the extra pool assignments placed before the
/// release's own pool lines (the first definition of a key wins).
String constraintsWithHints(LinkInfo base, PoolHints hints) {
  final out = StringBuffer('#osb-link-info 1\n');
  for (final l in hints.lines) {
    out.writeln(l);
  }
  for (final l in base.lines) {
    out.writeln(l);
  }
  return out.toString();
}
