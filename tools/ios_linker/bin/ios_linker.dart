// Command line entry point. See docs/ios-linker/COMPILER_LINKER.md.

import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import '../lib/src/checker.dart';
import '../lib/src/dd_select.dart';
import '../lib/src/gate_budget.dart';
import '../lib/src/hash.dart';
import '../lib/src/link_info.dart';
import '../lib/src/linker.dart';
import '../lib/src/pool_hints.dart';
import '../lib/src/snapshot_binary.dart';
import '../lib/src/vmcode.dart';

const usage = '''
usage:
  ios_linker link --base F --base-link F --patch-elf F --patch-link F
                  --out out.vmcode [--report report.json] [--table table.tsv]
                  [--min-link-percent P] [--gate-count N]
                  [--allow-unlinked-stubs true]
  ios_linker check --vmcode F --base F --patch-elf F
                  [--base-link F] [--patch-link F] [--min-link-percent P]
                  [--gate-count N]
  ios_linker pool-hints --base-link F --patch-link PASS1 --out constraints.link
  ios_linker diff-base --base F [--link F] --out F (release data + text blobs)
  ios_linker info --base F [--link F]   (JSON: format, text/data sizes and SHA-256)
  ios_linker dd-select --base F --link F --max-bytes N --out targets.dd
                  [--only-packages a,b]   (DD targets only from these packages)
  ios_linker compare-text --a F --b F [--link F]

--base is the release binary: the ELF gen_snapshot wrote, or the Mach-O
App.framework/App built from its assembly (the text image is located at
_kDartSnapshotText; its size comes from the sidecar). --base-elf and --elf
are accepted as aliases.

--gate-count is the runtime's gate capacity (default $defaultGateCount; 0
disables the check). A patch needing more gates than that (4 x unlinked
entries + call sites in unlinked entries + $gateReserve) is refused, as the
runtime would refuse it. check needs --patch-link for this.

Exit codes: 0 ok, 1 check failed / texts differ, 2 VM stubs unlinked,
3 below --min-link-percent, 4 sidecar/compiler mismatch, 5 over the runtime
gate budget, 64 usage.
''';

Map<String, String> parseArgs(List<String> args) {
  final out = <String, String>{};
  for (var i = 0; i < args.length; i++) {
    final a = args[i];
    if (!a.startsWith('--')) throw ArgumentError('unexpected $a\n$usage');
    final eq = a.indexOf('=');
    if (eq > 0) {
      out[a.substring(2, eq)] = a.substring(eq + 1);
    } else {
      out[a.substring(2)] = args[++i];
    }
  }
  return out;
}

String need(Map<String, String> o, String k) =>
    o[k] ?? (throw ArgumentError('missing --$k\n$usage'));

String baseArg(Map<String, String> o) =>
    o['base'] ?? o['base-elf'] ?? o['elf'] ?? need(o, 'base');

double? minPercentArg(Map<String, String> o) {
  final v = o['min-link-percent'];
  if (v == null) return null;
  final p = double.parse(v);
  if (p < 0 || p > 100) throw ArgumentError('--min-link-percent out of range');
  return p;
}

// Dart ignores main's return value; the process exit code is set explicitly.
void main(List<String> argv) {
  exitCode = mainWithExitCode(argv);
}

/// Gate capacity of the target runtime; 0 disables the budget check.
int gateCountArg(Map<String, String> o) {
  final v = o['gate-count'];
  if (v == null) return defaultGateCount;
  final n = int.parse(v);
  if (n < 0) throw ArgumentError('--gate-count must be >= 0');
  return n;
}

int mainWithExitCode(List<String> argv) {
  if (argv.isEmpty) {
    stderr.write(usage);
    return 64;
  }
  final o = parseArgs(argv.sublist(1));
  try {
    return run(argv.first, o);
  } on FormatException catch (e) {
    stderr.writeln('${argv.first}: ${e.message}');
    return 4;
  }
}

int run(String command, Map<String, String> o) {
  switch (command) {
    case 'link':
      return link(o);
    case 'check':
      final r = checkVmcode(
        vmcodePath: need(o, 'vmcode'),
        basePath: baseArg(o),
        patchElfPath: need(o, 'patch-elf'),
        baseLinkPath: o['base-link'],
        patchLinkPath: o['patch-link'],
        minLinkPercent: minPercentArg(o),
        gateCount: gateCountArg(o),
      );
      stdout.writeln('check: $r');
      for (final f in r.failures) {
        stdout.writeln('  FAIL $f');
      }
      if (r.ok) return 0;
      if (r.failures.length == 1 && r.belowMinimum) return 3;
      if (r.failures.length == 1 && r.overGateBudget) return 5;
      return 1;
    case 'pool-hints':
      final base = LinkInfo.read(need(o, 'base-link'));
      final pass1 = LinkInfo.read(need(o, 'patch-link'));
      final mismatch = base.identityMismatch(pass1);
      if (mismatch != null) throw FormatException('compiler mismatch: $mismatch');
      final hints = computePoolHints(base, pass1);
      File(need(o, 'out')).writeAsStringSync(constraintsWithHints(base, hints));
      stdout.writeln('pool-hints: assigned=${hints.assigned} '
          'mismatched_positions=${hints.considered}');
      return 0;
    case 'dd-select':
      final info = LinkInfo.read(need(o, 'link'));
      final bin =
          SnapshotBinary.read(baseArg(o), textImageSize: info.textImageSize);
      final snap = Snapshot(info, bin.textImage);
      final only = o['only-packages'];
      final packages = only == null
          ? null
          : {
              for (final p in only.split(','))
                if (p.trim().isNotEmpty) p.trim()
            };
      if (packages != null && packages.isEmpty) {
        throw ArgumentError('--only-packages is empty');
      }
      final sel = selectDD(snap, int.parse(need(o, 'max-bytes')),
          onlyPackages: packages);
      final sb = StringBuffer('# DD targets: max-bytes=${o['max-bytes']} '
          'call-sites=${sel.callSites}\n');
      sel.treeBytes.forEach((k, v) => sb.writeln(k));
      File(need(o, 'out')).writeAsStringSync(sb.toString());
      stdout.writeln('dd-select: ${sel.keys.length} targets, '
          '${sel.callSites} call sites (~${sel.estimatedCodeBytes} code bytes)');
      sel.treeBytes.forEach((k, v) => stdout.writeln('  $v\t$k'));
      return 0;
    case 'diff-base':
      final size = o['link'] == null
          ? null
          : LinkInfo.read(o['link']!).textImageSize;
      final bin = SnapshotBinary.read(baseArg(o), textImageSize: size);
      final out = BytesBuilder(copy: false)
        ..add(bin.dataImage)
        ..add(bin.textImage);
      File(need(o, 'out')).writeAsBytesSync(out.takeBytes());
      stdout.writeln('diff-base: ${bin.format} data=${bin.dataImage.length} '
          'text=${bin.textImage.length}');
      return 0;
    case 'info':
      final size = o['link'] == null
          ? null
          : LinkInfo.read(o['link']!).textImageSize;
      final bin = SnapshotBinary.read(baseArg(o), textImageSize: size);
      stdout.writeln(const JsonEncoder.withIndent(' ').convert({
        'format': bin.format,
        'text_size': bin.textImage.length,
        'text_sha256': hex(sha256(bin.textImage)),
        'data_size': bin.dataImage.length,
        'data_sha256': hex(sha256(bin.dataImage)),
      }));
      return 0;
    case 'compare-text':
      return compareText(o);
    default:
      stderr.write(usage);
      return 64;
  }
}

/// Compares the instructions images of two binaries (ELF or Mach-O). With
/// --link, also compares every code entry the sidecar lists. Exit 0 when
/// all code entries (or, without --link, the whole images) are identical.
int compareText(Map<String, String> o) {
  final info = o['link'] == null ? null : LinkInfo.read(o['link']!);
  final a = SnapshotBinary.read(need(o, 'a'), textImageSize: info?.textImageSize);
  final b = SnapshotBinary.read(need(o, 'b'), textImageSize: info?.textImageSize);
  final ta = a.textImage, tb = b.textImage;
  stdout.writeln('a: ${a.format} text=${ta.length} at file+${a.textFileOffset} '
      'sha256=${hex(sha256(ta))}');
  stdout.writeln('b: ${b.format} text=${tb.length} at file+${b.textFileOffset} '
      'sha256=${hex(sha256(tb))}');
  final n = ta.length < tb.length ? ta.length : tb.length;
  final diffs = <int>[];
  for (var i = 0; i + 8 <= n; i += 8) {
    for (var j = 0; j < 8; j++) {
      if (ta[i + j] != tb[i + j]) {
        diffs.add(i);
        break;
      }
    }
  }
  final classify = (int off) {
    final e = info?.entryAt(off);
    return e == null ? 'outside code' : 'in ${e.key}';
  };
  stdout.writeln('differing 8-byte words: ${diffs.length}');
  for (final d in diffs.take(10)) {
    stdout.writeln('  +$d (${classify(d)})');
  }
  if (info == null) return ta.length == tb.length && diffs.isEmpty ? 0 : 1;
  var bytes = 0, differing = 0;
  for (final e in info.codes) {
    bytes += e.size;
    if (e.end > n) {
      differing++;
      continue;
    }
    for (var i = e.textOffset; i < e.end; i++) {
      if (ta[i] != tb[i]) {
        differing++;
        break;
      }
    }
  }
  stdout.writeln('code entries: ${info.codes.length} ($bytes bytes), '
      'differing: $differing');
  return differing == 0 ? 0 : 1;
}

int link(Map<String, String> o) {
  final baseInfo = LinkInfo.read(need(o, 'base-link'));
  final patchInfo = LinkInfo.read(need(o, 'patch-link'));
  final mismatch = baseInfo.identityMismatch(patchInfo);
  if (mismatch != null) {
    stderr.writeln('link: release and patch were compiled by different '
        'gen_snapshots ($mismatch)');
    return 4;
  }
  final minPercent = minPercentArg(o);
  final baseBin =
      SnapshotBinary.read(baseArg(o), textImageSize: baseInfo.textImageSize);
  final patchBin = SnapshotBinary.read(need(o, 'patch-elf'),
      textImageSize: patchInfo.textImageSize);
  if (patchBin.format != 'elf') {
    throw const FormatException('the patch must be an ELF snapshot');
  }
  final patchElfBytes = patchBin.bytes;
  final baseText = baseBin.textImage;
  final patchText = patchBin.textImage;
  final base = Snapshot(baseInfo, baseText);
  final patch = Snapshot(patchInfo, patchText);
  final result = computeLinks(base, patch);

  final links = [
    for (final e in result.links.entries)
      VmcodeLink(patch.entries[e.key].entry.textOffset,
          base.entries[e.value].entry.textOffset, patch.entries[e.key].entry.size)
  ];
  final vm = Vmcode(
    links: links,
    elf: patchElfBytes,
    baseTextSha256: sha256(baseText),
    patchElfSha256: sha256(patchElfBytes),
    baseTextSize: baseText.length,
    patchTextSize: patchText.length,
    linkedCodeBytes: result.linkedCodeBytes,
    patchCodeBytes: result.patchCodeBytes,
  );
  final encoded = vm.encode();
  final outPath = need(o, 'out');
  final outFile = File(outPath);
  if (outFile.existsSync()) outFile.deleteSync();

  final reasons = <String, List<int>>{};
  result.unlinked.forEach((p, r) {
    final bucket = r.contains('@') ? r.substring(0, r.indexOf('@')) : r;
    final v = reasons.putIfAbsent(bucket, () => [0, 0]);
    v[0]++;
    v[1] += patch.entries[p].entry.size;
  });
  final unlinkedBytes = result.patchCodeBytes - result.linkedCodeBytes;
  stdout.writeln('link: ${result.linkPercent.toStringAsFixed(5)}% native '
      '(${result.linkedCodeBytes}/${result.patchCodeBytes} code bytes), '
      'linked=${result.links.length} unlinked=${result.unlinked.length} '
      '($unlinkedBytes bytes), vmcode=${encoded.length} '
      'header=${vm.elfOffset}');
  for (final r in reasons.entries) {
    stdout.writeln('  unlinked ${r.key}: ${r.value[0]} entries, '
        '${r.value[1]} bytes');
  }

  // VM stubs (entered from C++: InvokeDartCode, exception and suspend
  // stubs, ...) only change with the SDK. If one does not link, the release
  // and patch were built by different toolchains: fail closed.
  final unlinkedStubs = [
    for (final p in result.unlinked.keys)
      if (patch.entries[p].entry.key.startsWith('stub:'))
        patch.entries[p].entry.key
  ];
  final gateCount = gateCountArg(o);
  final budget = computeGateBudget(patchInfo, patchText,
      {for (final l in links) l.patchOffset});
  stdout.writeln('link: runtime $budget of $gateCount gates');
  var exit = 0;
  if (unlinkedStubs.isNotEmpty && o['allow-unlinked-stubs'] == null) {
    stderr.writeln('link: ${unlinkedStubs.length} VM stubs do not link '
        '(toolchain mismatch?): ${unlinkedStubs.take(5).join(', ')}');
    exit = 2;
  } else if (minPercent != null && result.linkPercent < minPercent) {
    stderr.writeln('link: ${result.linkPercent.toStringAsFixed(5)}% is below '
        '--min-link-percent $minPercent; no vmcode written');
    exit = 3;
  } else if (gateCount > 0 && budget.gatesNeeded > gateCount) {
    stderr.writeln('link: the runtime would refuse this patch: '
        '${budget.gatesNeeded} gates needed, $gateCount available '
        '(--gate-count); no vmcode written');
    exit = 5;
  } else {
    outFile.writeAsBytesSync(encoded);
  }

  if (o['table'] != null) {
    final sb = StringBuffer('patch_offset\tbase_offset\tsize\tkey\n');
    final sorted = result.links.entries.toList()
      ..sort((a, b) => patch.entries[a.key].entry.textOffset
          .compareTo(patch.entries[b.key].entry.textOffset));
    for (final e in sorted) {
      final pe = patch.entries[e.key].entry;
      sb.writeln('${pe.textOffset}\t${base.entries[e.value].entry.textOffset}'
          '\t${pe.size}\t${pe.key}');
    }
    File(o['table']!).writeAsStringSync(sb.toString());
  }
  if (o['report'] != null) {
    final unlinked = [
      for (final e in result.unlinked.entries)
        {
          'key': patch.entries[e.key].entry.key,
          'size': patch.entries[e.key].entry.size,
          'reason': e.value,
        }
    ]..sort((a, b) => (b['size'] as int).compareTo(a['size'] as int));
    File(o['report']!).writeAsStringSync(const JsonEncoder.withIndent(' ')
        .convert({
      'link_percent': result.linkPercent,
      'patch_code_bytes': result.patchCodeBytes,
      'linked_code_bytes': result.linkedCodeBytes,
      'patch_entries': patch.entries.length,
      'release_entries': base.entries.length,
      'linked_entries': result.links.length,
      'unlinked_entries': result.unlinked.length,
      'unlinked_code_bytes': unlinkedBytes,
      'subgraph_hash_agreements': result.subgraphHashAgreements,
      'release_format': baseBin.format,
      'min_link_percent': minPercent,
      'exit_code': exit,
      'gates_needed': budget.gatesNeeded,
      'gate_count': gateCount,
      'unlinked_call_sites': budget.unlinkedCallSites,
      'vmcode_bytes': exit == 0 ? encoded.length : null,
      'vmcode_header_bytes': vm.elfOffset,
      'unlinked_by_reason': {
        for (final r in reasons.entries)
          r.key: {'entries': r.value[0], 'bytes': r.value[1]}
      },
      'unlinked': unlinked,
    }));
  }
  return exit;
}
