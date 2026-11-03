class Stats {
  Stats(List<double> samples)
    : count = samples.length,
      sorted = (List<double>.of(samples)..sort());

  final int count;
  final List<double> sorted;

  double percentile(double p) {
    if (sorted.isEmpty) return 0;
    var idx = (p * sorted.length).ceil() - 1;
    if (idx < 0) idx = 0;
    if (idx >= sorted.length) idx = sorted.length - 1;
    return sorted[idx];
  }

  double get median {
    if (sorted.isEmpty) return 0;
    final m = sorted.length ~/ 2;
    return sorted.length.isOdd ? sorted[m] : (sorted[m - 1] + sorted[m]) / 2;
  }

  Map<String, Object> toJson() => {
    'n': count,
    'median': median,
    'p90': percentile(0.9),
    'min': sorted.isEmpty ? 0 : sorted.first,
    'max': sorted.isEmpty ? 0 : sorted.last,
  };
}
