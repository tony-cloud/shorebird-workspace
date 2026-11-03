// Independent verification of a vmcode link table against the actual
// release and patch binaries. It deliberately does not use the linker's
// analysis or the compiler's relocation records: it decodes every
// instruction word of every linked range and requires
//  * identical words, except B/BL whose targets leave the range, and
//  * every PC-relative reference that leaves a linked range to be a B/BL
//    whose patch target lies in another linked range and whose release
//    target is the corresponding offset of that range's release code.
// Hence release code reached through the table can only branch to release
// code that is itself linked to the patch code the patch would have run.

import 'dart:io';
import 'dart:typed_data';

import 'arm64.dart';
import 'hash.dart';
import 'link_info.dart';
import 'snapshot_binary.dart';
import 'vmcode.dart';

class CheckReport {
  int links = 0;
  int wordsCompared = 0;
  int externalBranches = 0;
  int internalPcRel = 0;
  double linkPercent = 0;
  final List<String> failures = [];
  bool get ok => failures.isEmpty;

  /// True when the only failure is the --min-link-percent gate.
  bool belowMinimum = false;
  bool get belowMinimumOnly => belowMinimum && failures.length == 1;

  @override
  String toString() =>
      'links=$links link_percent=${linkPercent.toStringAsFixed(5)} '
      'words=$wordsCompared external_branches=$externalBranches '
      'internal_pcrel=$internalPcRel failures=${failures.length}';
}

CheckReport checkVmcode({
  required String vmcodePath,
  required String basePath,
  required String patchElfPath,
  String? baseLinkPath,
  String? patchLinkPath,
  double? minLinkPercent,
}) {
  final report = CheckReport();
  void fail(String m) {
    if (report.failures.length < 50) report.failures.add(m);
  }

  final vmBytes = File(vmcodePath).readAsBytesSync();
  final vm = Vmcode.decode(vmBytes);
  final baseInfo = baseLinkPath == null ? null : LinkInfo.read(baseLinkPath);
  final patchInfo =
      patchLinkPath == null ? null : LinkInfo.read(patchLinkPath);
  if (baseInfo != null && patchInfo != null) {
    final mismatch = baseInfo.identityMismatch(patchInfo);
    if (mismatch != null) fail('compiler mismatch: $mismatch');
  }
  final patchBin = SnapshotBinary.read(patchElfPath,
      textImageSize: patchInfo?.textImageSize);
  if (patchBin.format != 'elf') fail('patch is not an ELF snapshot');
  final patchElfBytes = patchBin.bytes;
  // Without a sidecar a Mach-O release image extends to the next symbol or
  // section end; the header's size bounds it exactly.
  final baseBin = SnapshotBinary.read(basePath,
      textImageSize: baseInfo?.textImageSize ?? vm.baseTextSize);
  final baseText = baseBin.textImage;
  final patchText = patchBin.textImage;
  if (vm.baseTextSize != baseText.length) {
    fail('release image size in header (${vm.baseTextSize}) is not the '
        'release image size (${baseText.length})');
  }
  if (vm.patchTextSize != patchText.length) {
    fail('patch image size in header is wrong');
  }

  if (vm.elf.length != patchElfBytes.length ||
      hex(sha256(vm.elf)) != hex(sha256(patchElfBytes))) {
    fail('embedded ELF differs from the patch ELF');
  }
  if (hex(vm.patchElfSha256) != hex(sha256(patchElfBytes))) {
    fail('patch ELF hash in header is wrong');
  }
  if (hex(vm.baseTextSha256) != hex(sha256(baseText))) {
    fail('release image hash in header does not match the release');
  }

  final links = vm.links;
  report.links = links.length;
  final tableBytes = links.fold<int>(0, (s, l) => s + l.size);
  if (tableBytes != vm.linkedCodeBytes) {
    fail('header linked bytes ${vm.linkedCodeBytes} != table total '
        '$tableBytes');
  }
  if (patchInfo != null && patchInfo.codeBytes != vm.patchCodeBytes) {
    fail('header patch code bytes ${vm.patchCodeBytes} != sidecar total '
        '${patchInfo.codeBytes}');
  }
  if (vm.patchCodeBytes <= 0 || vm.linkedCodeBytes > vm.patchCodeBytes) {
    fail('header code byte counts are inconsistent');
  }
  report.linkPercent = vm.patchCodeBytes <= 0
      ? 0
      : 100.0 * vm.linkedCodeBytes / vm.patchCodeBytes;
  if (minLinkPercent != null && report.linkPercent < minLinkPercent) {
    report.belowMinimum = true;
    fail('link percent ${report.linkPercent.toStringAsFixed(5)} is below '
        '--min-link-percent $minLinkPercent');
  }
  for (var i = 1; i < links.length; i++) {
    if (links[i].patchOffset < links[i - 1].patchOffset + links[i - 1].size) {
      fail('links not sorted or overlapping at $i');
    }
  }

  // Optional cross-check: links cover whole code entries.
  if (patchInfo != null) {
    final p = patchInfo;
    final sizes = {for (final e in p.codes) e.textOffset: e.size};
    for (final l in links) {
      if (sizes[l.patchOffset] != l.size) {
        fail('link at patch ${l.patchOffset} is not a whole patch entry');
      }
    }
  }
  if (baseInfo != null) {
    final b = baseInfo;
    final sizes = {for (final e in b.codes) e.textOffset: e.size};
    for (final l in links) {
      if (sizes[l.baseOffset] != l.size) {
        fail('link at release ${l.baseOffset} is not a whole release entry');
      }
    }
  }

  VmcodeLink? linkContaining(int patchTextOffset) {
    var lo = 0, hi = links.length - 1;
    while (lo <= hi) {
      final mid = (lo + hi) >> 1;
      final l = links[mid];
      if (patchTextOffset < l.patchOffset) {
        hi = mid - 1;
      } else if (patchTextOffset >= l.patchOffset + l.size) {
        lo = mid + 1;
      } else {
        return l;
      }
    }
    return null;
  }

  final pd = ByteData.sublistView(patchText);
  final bd = ByteData.sublistView(baseText);
  for (final l in links) {
    if (l.size % 4 != 0 ||
        l.patchOffset + l.size > patchText.length ||
        l.baseOffset + l.size > baseText.length) {
      fail('link ${l.patchOffset}->${l.baseOffset} out of range');
      continue;
    }
    for (var off = 0; off < l.size; off += 4) {
      report.wordsCompared++;
      final wp = pd.getUint32(l.patchOffset + off, Endian.little);
      final wb = bd.getUint32(l.baseOffset + off, Endian.little);
      final rp = decodePcRel(wp);
      final rb = decodePcRel(wb);
      if (rp == null || rb == null) {
        if (wp != wb) {
          fail('word differs at patch ${l.patchOffset}+$off');
        }
        continue;
      }
      if (rp.kind != rb.kind) {
        fail('pc-relative kind differs at patch ${l.patchOffset}+$off');
        continue;
      }
      if (rp.kind == PcRelKind.adrp) {
        fail('adrp at patch ${l.patchOffset}+$off');
        continue;
      }
      final tp = l.patchOffset + off + rp.delta;
      final tb = l.baseOffset + off + rb.delta;
      final internal = tp >= l.patchOffset && tp < l.patchOffset + l.size;
      if (internal) {
        report.internalPcRel++;
        if (wp != wb) fail('internal reference differs at ${l.patchOffset}+$off');
        continue;
      }
      if (rp.kind != PcRelKind.b && rp.kind != PcRelKind.bl) {
        fail('${rp.kind.name} leaves linked code at ${l.patchOffset}+$off');
        continue;
      }
      if ((wp & 0xfc000000) != (wb & 0xfc000000)) {
        fail('branch opcode differs at ${l.patchOffset}+$off');
        continue;
      }
      final target = linkContaining(tp);
      if (target == null) {
        fail('release code at ${l.baseOffset}+$off would reach unlinked '
            'patch code at $tp');
        continue;
      }
      if (tb != target.baseOffset + (tp - target.patchOffset)) {
        fail('release branch at ${l.baseOffset}+$off reaches $tb, expected '
            '${target.baseOffset + (tp - target.patchOffset)}');
        continue;
      }
      report.externalBranches++;
    }
  }
  return report;
}
