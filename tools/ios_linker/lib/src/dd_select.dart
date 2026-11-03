// DD (dynamic dispatch) slot selection for a release.
//
// A change to function f un-links every entry that reaches f through direct
// branches (its transitive caller tree). Routing the static calls to f through
// its object pool slot (a DD call) cuts those edges: callers keep linking and
// only f itself (plus its own callees' effects) runs unlinked. Each DD call
// costs an extra load and an indirect branch, about 8 more bytes per call
// site, the checked entry instead of the unchecked one, and no inlining of f.
//
// Greedy heuristic: visit candidates in decreasing order of transitive caller
// bytes; select f if, given the targets selected so far (whose incoming edges
// are already cut), its remaining transitive caller tree still exceeds
// maxBytes. Only plain Dart functions called by pc-relative static calls are
// candidates (not stubs, closures or tear-offs).

import 'linker.dart';

class DDSelection {
  DDSelection(this.keys, this.callSites, this.treeBytes);
  final List<String> keys; // function keys (without the "fn:" prefix)
  final int callSites; // static call sites that become DD calls
  final Map<String, int> treeBytes; // caller-tree bytes at selection time
  int get estimatedCodeBytes => callSites * 8;
}

DDSelection selectDD(Snapshot snapshot, int maxBytes) {
  final n = snapshot.entries.length;
  final preds = List<List<int>>.generate(n, (_) => []);
  final sitesInto = List<int>.filled(n, 0);
  for (var u = 0; u < n; u++) {
    final seen = <int>{};
    for (final c in snapshot.entries[u].calls) {
      if (!c.isCall) continue;
      sitesInto[c.target]++;
      if (c.target != u && seen.add(c.target)) preds[c.target].add(u);
    }
  }
  bool eligible(int i) {
    final key = snapshot.entries[i].entry.key;
    return key.startsWith('fn:') &&
        !key.contains('<closure') &&
        !key.contains('<tearoff>') &&
        sitesInto[i] > 0;
  }

  final selected = <int>{};
  int treeBytes(int f) {
    final visited = <int>{f};
    final work = [f];
    var bytes = 0;
    while (work.isNotEmpty) {
      final v = work.removeLast();
      if (v != f && selected.contains(v)) continue; // edges into DD are cut
      for (final u in preds[v]) {
        if (visited.add(u)) {
          bytes += snapshot.entries[u].entry.size;
          work.add(u);
        }
      }
    }
    return bytes;
  }

  final candidates = [
    for (var i = 0; i < n; i++)
      if (eligible(i)) i
  ];
  final initial = {for (final c in candidates) c: treeBytes(c)};
  candidates.sort((a, b) => initial[b]!.compareTo(initial[a]!));
  final chosen = <String, int>{};
  var sites = 0;
  for (final c in candidates) {
    if (initial[c]! <= maxBytes) break;
    final t = treeBytes(c);
    if (t > maxBytes) {
      selected.add(c);
      chosen[snapshot.entries[c].entry.key.substring(3)] = t;
      sites += sitesInto[c];
    }
  }
  return DDSelection(chosen.keys.toList(), sites, chosen);
}

/// Reads the selection back (one key per line).
List<String> readDDTargets(String text) => [
      for (final l in text.split('\n'))
        if (l.isNotEmpty && !l.startsWith('#')) l
    ];

