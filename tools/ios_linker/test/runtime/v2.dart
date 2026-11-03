// Runtime fixture: patched version of v1.dart.
import 'dart:collection';

abstract class Shape {
  double area();
  String get name => 'shape';
}

class Circle extends Shape {
  final double r;
  Circle(this.r);
  @override
  double area() => 3.0 * r * r;
  @override
  String get name => 'circle';
}

class Square extends Shape {
  final double s;
  Square(this.s);
  @override
  double area() => s * s;
}

class Base {
  int a;
  double d;
  String label;
  double extra = 0.25;
  int added = 40;
  Base(this.a, this.d, this.label);
  int total() => a + d.toInt() + added + (extra * 4).toInt();
}

class Derived extends Base {
  int b;
  final List<int> items = [];
  Derived(int a, this.b) : super(a, 1.5, 'derived');
  @override
  int total() => super.total() + b + items.length;
}

class Triangle extends Shape {
  final double b, h;
  Triangle(this.b, this.h);
  @override
  double area() => b * h / 2;
  @override
  String get name => 'triangle';
}

class Pair<A, B> {
  final A first;
  final B second;
  const Pair(this.first, this.second);
}

class Box<T> {
  T value;
  Box(this.value);
  @override
  String toString() => 'Box<$T>($value)';
}

final Map<String, int> registry = {'one': 1, 'two': 2, 'three': 3};
final List<Pair<String, int>> pairs = [const Pair('p', 1), Pair('q', 2)];
late final int lazyValue = computeLazy();
int counter = 0;

int computeLazy() {
  counter++;
  return registry.values.fold(0, (a, b) => a + b) * 10;
}

@pragma('vm:never-inline')
int helper(int x) => (x * 7 + 2) & 0xffff;

@pragma('vm:never-inline')
String describe(Object o) => switch (o) {
      Circle c => 'C${c.r}',
      Shape s => 'S${s.area()}',
      int i => 'I$i',
      _ => 'O$o',
    };

void main() {
  final shapes = <Shape>[Circle(1.5), Square(2.0), Circle(0.5), Triangle(3, 4)];
  var sum = 0.0;
  for (final s in shapes) {
    sum += s.area();
  }
  print('shapes ${shapes.map((s) => s.name).join(',')} area=$sum');
  final d = Derived(3, 4)..items.addAll([1, 2, 3]);
  final b = Base(1, 2.5, 'base');
  print('total ${d.total()} ${b.total()} ${d.label} ${b.label}');
  print('${Box<int>(7)} ${Box<String>('x')} ${Box<Shape>(Square(1))}');
  print('lazy $lazyValue $lazyValue counter=$counter');
  dynamic dyn = shapes.first;
  print('dynamic ${dyn.area()} ${dyn.name}');
  final fns = [for (var i = 0; i < 4; i++) () => helper(i * 7)];
  print('closures ${fns.map((f) => f()).toList()}');
  try {
    throw StateError('boom');
  } on StateError catch (e) {
    print('caught (patched) ${e.message}');
  }
  final q = Queue<int>()..addAll([5, 6, 7]);
  print('queue ${q.removeFirst()} ${q.length}');
  print('describe ${describe(Circle(2))} ${describe(Square(3))} ${describe(9)} ${describe('z')}');
  final m = SplayTreeMap<String, double>()..['b'] = 2.0..['a'] = 1.0;
  print('map $m ${identical(const [1, 2], const [1, 2])}');
  print('helper ${helper(1000)}');
  print('pairs ${pairs.map((p) => '${p.first}${p.second}').join()} ${d.added} ${d.extra}');
}
