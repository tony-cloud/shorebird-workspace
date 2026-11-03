// Command line entry point. See docs/ios-linker/COMPILER_LINKER.md.

import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import '../lib/src/checker.dart';
import '../lib/src/dd_select.dart';
import '../lib/src/elf.dart';
import '../lib/src/hash.dart';
import '../lib/src/link_info.dart';
import '../lib/src/linker.dart';
import '../lib/src/pool_hints.dart';
import '../lib/src/vmcode.dart';

const usage = '''
usage:
  ios_linker link --base-elf F --base-link F --patch-elf F --patch-link F
                  --out out.vmcode [--report report.json] [--table table.tsv]
                  [--allow-unlinked-stubs true]
  ios_linker check --vmcode F --base-elf F --patch-elf F
                  [--base-link F] [--patch-link F]
  ios_linker pool-hints --base-link F --patch-link PASS1 --out constraints.link
  ios_linker diff-base --elf F --out F   (release data + text blobs)
  ios_linker dd-select --elf F --link F --max-bytes N --out targets.dd
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

int main(List<String> argv) {
  if (argv.isEmpty) {
    stderr.write(usage);
    return 64;
  }
  final o = parseArgs(argv.sublist(1));
  switch (argv.first) {
    case 'link':
      return link(o);
    case 'check':
      final r = checkVmcode(
        vmcodePath: need(o, 'vmcode'),
        baseElfPath: need(o, 'base-elf'),
        patchElfPath: need(o, 'patch-elf'),
        baseLinkPath: o['base-link'],
        patchLinkPath: o['patch-link'],
      );
      stdout.writeln('check: $r');
      for (final f in r.failures) {
        stdout.writeln('  FAIL $f');
      }
      return r.ok ? 0 : 1;
    case 'pool-hints':
      final base = LinkInfo.read(need(o, 'base-link'));
      final pass1 = LinkInfo.read(need(o, 'patch-link'));
      final hints = computePoolHints(base, pass1);
      File(need(o, 'out')).writeAsStringSync(constraintsWithHints(base, hints));
      stdout.writeln('pool-hints: assigned=${hints.assigned} '
          'mismatched_positions=${hints.considered}');
      return 0;
    case 'dd-select':
      final snap = Snapshot(LinkInfo.read(need(o, 'link')),
          dartTextImage(Elf(File(need(o, 'elf')).readAsBytesSync())));
      final sel = selectDD(snap, int.parse(need(o, 'max-bytes')));
      final sb = StringBuffer('# DD targets: max-bytes=${o['max-bytes']} '
          'call-sites=${sel.callSites}\n');
      sel.treeBytes.forEach((k, v) => sb.writeln(k));
      File(need(o, 'out')).writeAsStringSync(sb.toString());
      stdout.writeln('dd-select: ${sel.keys.length} targets, '
          '${sel.callSites} call sites (~${sel.estimatedCodeBytes} code bytes)');
      sel.treeBytes.forEach((k, v) => stdout.writeln('  $v\t$k'));
      return 0;
    case 'diff-base':
      final elf = Elf(File(need(o, 'elf')).readAsBytesSync());
      final out = BytesBuilder(copy: false)
        ..add(elf.symbolBytes(elf.symbols.containsKey('_kDartSnapshotData')
            ? '_kDartSnapshotData'
            : '_kDartIsolateSnapshotData'))
        ..add(dartTextImage(elf));
      File(need(o, 'out')).writeAsBytesSync(out.takeBytes());
      return 0;
    default:
      stderr.write(usage);
      return 64;
  }
}

int link(Map<String, String> o) {
  final baseElf = Elf(File(need(o, 'base-elf')).readAsBytesSync());
  final patchElfBytes = File(need(o, 'patch-elf')).readAsBytesSync();
  final patchElf = Elf(patchElfBytes);
  final baseText = dartTextImage(baseElf);
  final patchText = dartTextImage(patchElf);
  final base = Snapshot(LinkInfo.read(need(o, 'base-link')), baseText);
  final patch = Snapshot(LinkInfo.read(need(o, 'patch-link')), patchText);
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
  File(need(o, 'out')).writeAsBytesSync(encoded);

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
  if (unlinkedStubs.isNotEmpty && o['allow-unlinked-stubs'] == null) {
    stderr.writeln('link: ${unlinkedStubs.length} VM stubs do not link '
        '(toolchain mismatch?): ${unlinkedStubs.take(5).join(', ')}');
    File(need(o, 'out')).deleteSync();
    return 2;
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
      'vmcode_bytes': encoded.length,
      'vmcode_header_bytes': vm.elfOffset,
      'unlinked_by_reason': {
        for (final r in reasons.entries)
          r.key: {'entries': r.value[0], 'bytes': r.value[1]}
      },
      'unlinked': unlinked,
    }));
  }
  return 0;
}
