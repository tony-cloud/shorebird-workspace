// Wide class: with compressed pointers the unboxed-field bitmap covers 256
// bytes, so the stock layout boxes the double fields past that point. A
// patch must keep the release's boxing decision (runtime test pair v3/v4).

class Big {
  double f0;
  double f1;
  double f2;
  double f3;
  double f4;
  double f5;
  double f6;
  double f7;
  double f8;
  double f9;
  double f10;
  double f11;
  double f12;
  double f13;
  double f14;
  double f15;
  double f16;
  double f17;
  double f18;
  double f19;
  double f20;
  double f21;
  double f22;
  double f23;
  double f24;
  double f25;
  double f26;
  double f27;
  double f28;
  double f29;
  double f30;
  double f31;
  double f32;
  double f33;
  double f34;
  double f35;
  double f36;
  double f37;
  double f38;
  double f39;
  int extra = 7;
  Big(this.f0, this.f1, this.f2, this.f3, this.f4, this.f5, this.f6, this.f7, this.f8, this.f9, this.f10, this.f11, this.f12, this.f13, this.f14, this.f15, this.f16, this.f17, this.f18, this.f19, this.f20, this.f21, this.f22, this.f23, this.f24, this.f25, this.f26, this.f27, this.f28, this.f29, this.f30, this.f31, this.f32, this.f33, this.f34, this.f35, this.f36, this.f37, this.f38, this.f39);
}

@pragma('vm:never-inline')
Big make(double s) => Big(1.5 * 0 + s, 1.5 * 1 + s, 1.5 * 2 + s, 1.5 * 3 + s, 1.5 * 4 + s, 1.5 * 5 + s, 1.5 * 6 + s, 1.5 * 7 + s, 1.5 * 8 + s, 1.5 * 9 + s, 1.5 * 10 + s, 1.5 * 11 + s, 1.5 * 12 + s, 1.5 * 13 + s, 1.5 * 14 + s, 1.5 * 15 + s, 1.5 * 16 + s, 1.5 * 17 + s, 1.5 * 18 + s, 1.5 * 19 + s, 1.5 * 20 + s, 1.5 * 21 + s, 1.5 * 22 + s, 1.5 * 23 + s, 1.5 * 24 + s, 1.5 * 25 + s, 1.5 * 26 + s, 1.5 * 27 + s, 1.5 * 28 + s, 1.5 * 29 + s, 1.5 * 30 + s, 1.5 * 31 + s, 1.5 * 32 + s, 1.5 * 33 + s, 1.5 * 34 + s, 1.5 * 35 + s, 1.5 * 36 + s, 1.5 * 37 + s, 1.5 * 38 + s, 1.5 * 39 + s);

@pragma('vm:never-inline')
double total(Big b) => b.f0 + b.f1 + b.f2 + b.f3 + b.f4 + b.f5 + b.f6 + b.f7 + b.f8 + b.f9 + b.f10 + b.f11 + b.f12 + b.f13 + b.f14 + b.f15 + b.f16 + b.f17 + b.f18 + b.f19 + b.f20 + b.f21 + b.f22 + b.f23 + b.f24 + b.f25 + b.f26 + b.f27 + b.f28 + b.f29 + b.f30 + b.f31 + b.f32 + b.f33 + b.f34 + b.f35 + b.f36 + b.f37 + b.f38 + b.f39 + b.extra;

void main(List<String> args) {
  final bs = [for (var i = 0; i < 5; i++) make(i * 0.5 + args.length)];
  for (final b in bs) {
    b.f0 += 0.25;
    b.f3 += 3.25;
    b.f6 += 6.25;
    b.f9 += 2.25;
    b.f12 += 5.25;
    b.f15 += 1.25;
    b.f18 += 4.25;
    b.f21 += 0.25;
    b.f24 += 3.25;
    b.f27 += 6.25;
    b.f30 += 2.25;
    b.f33 += 5.25;
    b.f36 += 1.25;
    b.f39 += 4.25;
  }
  for (final b in bs) {
    print('total ${total(b).toStringAsFixed(3)} f39 ${b.f39} f33 ${b.f33}');
  }
}
