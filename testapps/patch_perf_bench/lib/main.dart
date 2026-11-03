import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'kernel.dart';
import 'stats.dart';

const _variant = String.fromEnvironment(
  'BENCH_VARIANT',
  defaultValue: 'unknown',
);

int sink = 0;

final Map<String, Object?> report = {};

/// iOS container Documents dir, derived from the container tmp dir.
String documentsPath() {
  final tmp = Directory.systemTemp.path;
  return '${Directory(tmp).parent.path}/Documents';
}

String _env(String key, String fallback) =>
    Platform.environment[key] ?? fallback;

/// Times [body] after warm-up; returns microseconds per rep.
List<double> timeReps(
  int Function() body, {
  int warmup = 3,
  int reps = 15,
  Duration budget = const Duration(seconds: 25),
}) {
  for (var i = 0; i < warmup; i++) {
    sink ^= body();
  }
  final samples = <double>[];
  final total = Stopwatch()..start();
  final sw = Stopwatch();
  for (var i = 0; i < reps; i++) {
    sw.reset();
    sw.start();
    final r = body();
    sw.stop();
    sink ^= r;
    samples.add(sw.elapsedMicroseconds.toDouble());
    if (total.elapsed > budget && samples.length >= 5) break;
  }
  return samples;
}

Map<String, Object?> runMicro(Workset w) {
  final out = <String, Object?>{};
  const plan = <(String, int, int)>[
    ('numeric_loop', 1, 3000000),
    ('virtual_interface_calls', 2, 1000000),
    ('list_map_ops', 3, 300000),
    ('string_building', 4, 100000),
    ('object_allocation', 5, 2000000),
  ];
  for (final (name, mode, n) in plan) {
    final samples = timeReps(() => patchedKernel(mode, n, w));
    final s = Stats(samples).toJson();
    s['ops_per_rep'] = n;
    s['unit'] = 'us_per_rep';
    out[name] = s;
  }
  return out;
}

Map<String, Object?> runTransitions(Workset w) {
  final out = <String, Object?>{};
  const calls = 200000;
  // AOT caller -> (possibly patched) kernel, trivial body.
  final aotToKernel = timeReps(() {
    var acc = 0;
    for (var i = 0; i < calls; i++) {
      acc += patchedKernel(0, i, w);
    }
    return acc;
  }, reps: 15);
  final a = Stats(aotToKernel.map((e) => e * 1000 / calls).toList()).toJson();
  a['calls_per_rep'] = calls;
  a['unit'] = 'ns_per_call';
  out['aot_calls_kernel_tag'] = a;

  // Kernel (interpreted when patched) -> trivial AOT leaf.
  final kernelToAot = timeReps(() => patchedKernel(6, calls, w), reps: 15);
  final b = Stats(kernelToAot.map((e) => e * 1000 / calls).toList()).toJson();
  b['calls_per_rep'] = calls;
  b['unit'] = 'ns_per_call';
  out['kernel_calls_aot_leaf'] = b;

  // Loop-only reference inside the kernel (same loop, no call).
  final loopOnly = timeReps(() => patchedKernel(1, calls, w), reps: 15);
  final c = Stats(loopOnly.map((e) => e * 1000 / calls).toList()).toJson();
  c['calls_per_rep'] = calls;
  c['unit'] = 'ns_per_iteration';
  out['kernel_loop_reference'] = c;
  return out;
}

class FrameCollector {
  final List<FrameTiming> timings = [];
  bool recording = false;
  void onTimings(List<FrameTiming> t) {
    if (recording) timings.addAll(t);
  }
}

final FrameCollector collector = FrameCollector();
final Workset workset = Workset();

int itemBuilds = 0;

class Tick extends InheritedWidget {
  const Tick({super.key, required this.frame, required super.child});
  final int frame;
  @override
  bool updateShouldNotify(Tick old) => true;
}

/// A list item whose build() does moderate work in the (patchable) kernel and
/// depends on [Tick], so every visible item rebuilds on every frame.
class Item extends StatelessWidget {
  const Item(this.index, {super.key});
  final int index;

  @override
  Widget build(BuildContext context) {
    context.dependOnInheritedWidgetOfExactType<Tick>();
    itemBuilds++;
    final work = patchedKernel(7, 2000, workset);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      child: DecoratedBox(
        decoration: BoxDecoration(
          color: Color(0xFF000000 | ((index * 2654435761) & 0xFFFFFF)),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Row(
          children: [
            Padding(
              padding: const EdgeInsets.all(8),
              child: Text(
                'Item $index  w=$work',
                style: const TextStyle(fontSize: 16, color: Color(0xFFFFFFFF)),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class BenchApp extends StatefulWidget {
  const BenchApp({super.key, required this.onReady});
  final void Function(_BenchState) onReady;

  @override
  State<BenchApp> createState() => _BenchState();
}

class _BenchState extends State<BenchApp> with SingleTickerProviderStateMixin {
  final ScrollController controller = ScrollController();
  late final Ticker ticker;
  String status = 'starting';
  bool scrolling = false;
  double offset = 0;
  int ticks = 0;
  Duration? last;

  @override
  void initState() {
    super.initState();
    ticker = createTicker((elapsed) {
      if (!scrolling) return;
      final dt = last == null ? 0 : (elapsed - last!).inMicroseconds / 1e6;
      last = elapsed;
      offset += 3000 * dt;
      if (controller.hasClients) controller.jumpTo(offset);
      // Rebuild all visible items every frame (they depend on Tick).
      ticks++;
      setState(() {});
    });
    ticker.start();
    WidgetsBinding.instance.addPostFrameCallback((_) => widget.onReady(this));
  }

  void setStatus(String s) {
    try {
      final dir = Directory(documentsPath())..createSync(recursive: true);
      File('${dir.path}/progress.txt').writeAsStringSync(
        '${DateTime.now().toIso8601String()} $_variant $s\n',
        mode: FileMode.append,
      );
    } catch (_) {}
    setState(() => status = s);
  }

  @override
  void dispose() {
    ticker.dispose();
    controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Directionality(
      textDirection: TextDirection.ltr,
      child: Tick(
        frame: ticks,
        child: ColoredBox(
          color: const Color(0xFFFFFFFF),
          child: Column(
            children: [
              const SizedBox(height: 48),
              Text(
                'bench $_variant: $status',
                style: const TextStyle(fontSize: 18, color: Color(0xFF000000)),
              ),
              Expanded(
                child: ListView.builder(
                  controller: controller,
                  itemExtent: 56,
                  itemBuilder: (context, i) => Item(i),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

Map<String, Object?> frameStats(List<FrameTiming> t) {
  final build = t.map((e) => e.buildDuration.inMicroseconds / 1000).toList();
  final raster = t.map((e) => e.rasterDuration.inMicroseconds / 1000).toList();
  final total = t.map((e) => e.totalSpan.inMicroseconds / 1000).toList();
  final over16 = total.where((e) => e > 16.7).length;
  final over8 = total.where((e) => e > 8.4).length;
  return {
    'frames': t.length,
    'build_ms': Stats(build).toJson(),
    'raster_ms': Stats(raster).toJson(),
    'total_span_ms': Stats(total).toJson(),
    'frames_over_16_7ms': over16,
    'frames_over_8_4ms': over8,
    'unit': 'ms',
  };
}

Future<void> runAll(_BenchState app) async {
  final run = _env(
    'BENCH_RUN',
    DateTime.now().millisecondsSinceEpoch.toString(),
  );
  final variantLabel = _env('BENCH_VARIANT', _variant);
  report['variant'] = variantLabel;
  report['run'] = run;
  report['kernel_tag'] = patchedKernel(0, 0, workset);
  report['started'] = DateTime.now().toUtc().toIso8601String();
  report['dart_version'] = Platform.version;
  report['dpr'] = ui.PlatformDispatcher.instance.views.first.devicePixelRatio;
  report['refresh_hz'] =
      ui.PlatformDispatcher.instance.views.first.display.refreshRate;
  app.setStatus('micro');
  await Future<void>.delayed(const Duration(milliseconds: 300));
  report['micro'] = runMicro(workset);
  app.setStatus('transitions');
  await Future<void>.delayed(const Duration(milliseconds: 100));
  report['transitions'] = runTransitions(workset);
  app.setStatus('frames');
  await Future<void>.delayed(const Duration(milliseconds: 500));
  app.scrolling = true;
  await Future<void>.delayed(const Duration(seconds: 3)); // warm-up frames
  collector.timings.clear();
  final buildsBefore = itemBuilds;
  collector.recording = true;
  await Future<void>.delayed(const Duration(seconds: 10));
  collector.recording = false;
  app.scrolling = false;
  await Future<void>.delayed(const Duration(milliseconds: 500));
  report['frames'] = frameStats(collector.timings);
  (report['frames'] as Map<String, Object?>)['item_builds'] =
      itemBuilds - buildsBefore;
  report['sink'] = sink;
  report['finished'] = DateTime.now().toUtc().toIso8601String();
  final dir = Directory(documentsPath());
  dir.createSync(recursive: true);
  final file = File('${dir.path}/bench_${variantLabel}_$run.json');
  final tmp = File('${file.path}.tmp');
  tmp.writeAsStringSync(const JsonEncoder.withIndent(' ').convert(report));
  tmp.renameSync(file.path);
  app.setStatus('DONE ${file.uri.pathSegments.last}');
  // ignore: avoid_print
  print('BENCH_DONE ${file.path}');
}

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  SchedulerBinding.instance.addTimingsCallback(collector.onTimings);
  runApp(
    BenchApp(
      onReady: (s) {
        unawaited(
          runAll(s).catchError((Object e, StackTrace st) {
            // ignore: avoid_print
            print('BENCH_ERROR $e\n$st');
            s.setStatus('ERROR $e');
          }),
        );
      },
    ),
  );
}
