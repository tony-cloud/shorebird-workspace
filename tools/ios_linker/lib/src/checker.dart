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
import 'elf.dart';
import 'hash.dart';
import 'link_info.dart';
import 'vmcode.dart';

class CheckReport {
  int links = 0;
  int wordsCompared = 0;
  int externalBranches = 0;
  int internalPcRel = 0;
  final List<String> failures = [];
  bool get ok => failures.isEmpty;

  @override
  String toString() =>
      'links=$links words=$wordsCompared external_branches=$externalBranches '
      'internal_pcrel=$internalPcRel failures=${failures.length}';
}

CheckReport checkVmcode({
  required String vmcodePath,
  required String baseElfPath,
  required String patchElfPath,
  String? baseLinkPath,
  String? patchLinkPath,
}) {
  final report = CheckReport();
  void fail(String m) {
    if (report.failures.length < 50) report.failures.add(m);
  }

  final vmBytes = File(vmcodePath).readAsBytesSync();
  final vm = Vmcode.decode(vmBytes);
  final patchElfBytes = File(patchElfPath).readAsBytesSync();
  final baseText = dartTextImage(Elf(File(baseElfPath).readAsBytesSync()));
  final patchText = dartTextImage(Elf(patchElfBytes));

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
  for (var i = 1; i < links.length; i++) {
    if (links[i].patchOffset < links[i - 1].patchOffset + links[i - 1].size) {
      fail('links not sorted or overlapping at $i');
    }
  }

  // Optional cross-check: links cover whole code entries.
  if (patchLinkPath != null) {
    final p = LinkInfo.read(patchLinkPath);
    final sizes = {for (final e in p.codes) e.textOffset: e.size};
    for (final l in links) {
      if (sizes[l.patchOffset] != l.size) {
        fail('link at patch ${l.patchOffset} is not a whole patch entry');
      }
    }
  }
  if (baseLinkPath != null) {
    final b = LinkInfo.read(baseLinkPath);
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
