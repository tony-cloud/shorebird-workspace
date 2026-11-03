// Benchmark kernels. Everything the patch route may replace lives in the single
// function [patchedKernel]; the rest of this file is never modified by the
// patch, so it stays AOT-compiled in the patched variants.

abstract class Shape {
  int area(int k);
}

class Square extends Shape {
  @override
  int area(int k) => k * k + 1;
}

class Rect extends Shape {
  @override
  int area(int k) => k * 3 + 2;
}

class Tri extends Shape {
  @override
  int area(int k) => (k * k) ~/ 2 + 3;
}

class Circle extends Shape {
  @override
  int area(int k) => k * 7 - 1;
}

abstract class Scorer {
  int score(int k);
}

class ScorerA implements Scorer {
  @override
  int score(int k) => k ^ 0x55;
}

class ScorerB implements Scorer {
  @override
  int score(int k) => k + 13;
}

class ScorerC implements Scorer {
  @override
  int score(int k) => k - 7;
}

/// Single-implementation base class used on AOT hot paths (the unpatched app has
/// no subclass). With closed-world devirtualization the calls to [weight] are
/// direct; the V2o patch adds an override ([HeavyChip]) and so forces the
/// per-class guard fallback.
class Chip {
  int get weight => 3;
}

class Point {
  Point(this.x, this.y);
  final double x;
  final double y;
}

/// Data the AOT driver prepares so the kernel does not depend on setup code.
class Workset {
  final List<Shape> shapes = [Square(), Rect(), Tri(), Circle()];
  final List<Scorer> scorers = [ScorerA(), ScorerB(), ScorerC()];
  final List<int> list = List<int>.generate(1000, (i) => i);
  final Map<int, int> map = {for (var i = 0; i < 1000; i++) i: i * 2};
  final Chip chip = Chip();
}

/// Trivial unchanged AOT callee used by the interpreter->AOT transition test.
@pragma('vm:never-inline')
int aotLeaf(int x) => x + 1;

enum Mode {
  tag,
  numeric,
  virtualCalls,
  collections,
  strings,
  alloc,
  callAot,
  itemWork,
}

/// The one function a patch changes. Mode [Mode.tag] returns the code version,
/// so the app can tell whether the patched body is the one running.
@pragma('vm:never-inline')
int patchedKernel(int mode, int n, Workset w) {
  switch (mode) {
    case 0:
      return 1; // KERNEL_TAG
    case 1:
      {
        var acc = 0;
        for (var i = 0; i < n; i++) {
          acc = (acc * 31 + i * 7 + (i >> 3)) & 0xFFFFFF;
          if ((i & 15) == 0) acc ^= i;
        }
        return acc;
      }
    case 2:
      {
        var acc = 0;
        final shapes = w.shapes;
        final scorers = w.scorers;
        for (var i = 0; i < n; i++) {
          acc += shapes[i & 3].area(i & 15);
          acc += scorers[i % 3].score(i & 31);
        }
        return acc;
      }
    case 3:
      {
        var acc = 0;
        final list = w.list;
        final map = w.map;
        final scratch = <int>[];
        for (var i = 0; i < n; i++) {
          scratch.add(i);
          acc += list[i % 1000];
          acc += map[i % 1000]!;
          map[(i * 7) % 1000] = i;
          if (scratch.length > 64) scratch.clear();
        }
        return acc + scratch.length;
      }
    case 4:
      {
        var total = 0;
        for (var i = 0; i < n; i++) {
          final sb = StringBuffer();
          sb.write('item');
          sb.write(i);
          sb.write(':');
          sb.write('$i-${i * 3}');
          total += sb.toString().length;
        }
        return total;
      }
    case 5:
      {
        var acc = 0.0;
        for (var i = 0; i < n; i++) {
          final p = Point(i.toDouble(), 2.0);
          acc += p.x + p.y;
        }
        return acc.toInt();
      }
    case 6:
      {
        var acc = 0;
        for (var i = 0; i < n; i++) {
          acc += aotLeaf(i);
        }
        return acc;
      }
    case 7:
      {
        var acc = 0;
        for (var i = 0; i < n; i++) {
          acc = (acc * 31 + i) & 0xFFFF;
        }
        final sb = StringBuffer();
        for (var i = 0; i < 8; i++) {
          sb.write(acc + i);
        }
        return acc + sb.length;
      }
  }
  return -1;
}
