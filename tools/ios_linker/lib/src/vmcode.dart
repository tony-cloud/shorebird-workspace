// The vmcode container: link header followed by the complete patch ELF.
//
// All integers little-endian. Layout (version 1):
//
//   0   u8[8]  magic "OSBVMC\0\1"
//   8   u32    version (1)
//   12  u32    header bytes (offset of the ELF, multiple of 16 KiB)
//   16  u64    ELF offset (same as header bytes)
//   24  u64    ELF size
//   32  u32    link count
//   36  u32    link entry size (16)
//   40  u32    links offset (128)
//   44  u32    flags (reserved, 0)
//   48  u8[32] SHA-256 of the release instructions image
//   80  u8[32] SHA-256 of the patch ELF
//   112 u32    release instructions image size
//   116 u32    patch instructions image size
//   120 u32    linked code bytes
//   124 u32    patch code bytes
//   128 links, sorted by patch offset, each:
//         u32 patch payload offset   (from the patch's instructions image)
//         u32 release payload offset (from the release's instructions image)
//         u32 payload size in bytes  (identical in both)
//         u32 flags (reserved, 0)
//   ...  zero padding up to the ELF offset
//   ELF  the patch snapshot, byte-for-byte as written by gen_snapshot

import 'dart:typed_data';

const vmcodeMagic = [0x4f, 0x53, 0x42, 0x56, 0x4d, 0x43, 0x00, 0x01];
const vmcodeVersion = 1;
const vmcodeHeaderFixed = 128;
const vmcodeLinkEntrySize = 16;
const vmcodeElfAlignment = 16384;

class VmcodeLink {
  VmcodeLink(this.patchOffset, this.baseOffset, this.size, [this.flags = 0]);
  final int patchOffset, baseOffset, size, flags;
}

class Vmcode {
  Vmcode({
    required this.links,
    required this.elf,
    required this.baseTextSha256,
    required this.patchElfSha256,
    required this.baseTextSize,
    required this.patchTextSize,
    required this.linkedCodeBytes,
    required this.patchCodeBytes,
  });

  final List<VmcodeLink> links;
  final Uint8List elf;
  final Uint8List baseTextSha256, patchElfSha256;
  final int baseTextSize, patchTextSize, linkedCodeBytes, patchCodeBytes;
  int elfOffset = 0;

  Uint8List encode() {
    final sorted = [...links]
      ..sort((a, b) => a.patchOffset.compareTo(b.patchOffset));
    final tableEnd = vmcodeHeaderFixed + sorted.length * vmcodeLinkEntrySize;
    elfOffset = (tableEnd + vmcodeElfAlignment - 1) ~/
        vmcodeElfAlignment *
        vmcodeElfAlignment;
    final out = Uint8List(elfOffset + elf.length);
    final d = ByteData.sublistView(out);
    out.setAll(0, vmcodeMagic);
    d.setUint32(8, vmcodeVersion, Endian.little);
    d.setUint32(12, elfOffset, Endian.little);
    d.setUint64(16, elfOffset, Endian.little);
    d.setUint64(24, elf.length, Endian.little);
    d.setUint32(32, sorted.length, Endian.little);
    d.setUint32(36, vmcodeLinkEntrySize, Endian.little);
    d.setUint32(40, vmcodeHeaderFixed, Endian.little);
    d.setUint32(44, 0, Endian.little);
    out.setAll(48, baseTextSha256);
    out.setAll(80, patchElfSha256);
    d.setUint32(112, baseTextSize, Endian.little);
    d.setUint32(116, patchTextSize, Endian.little);
    d.setUint32(120, linkedCodeBytes, Endian.little);
    d.setUint32(124, patchCodeBytes, Endian.little);
    var o = vmcodeHeaderFixed;
    for (final l in sorted) {
      d.setUint32(o, l.patchOffset, Endian.little);
      d.setUint32(o + 4, l.baseOffset, Endian.little);
      d.setUint32(o + 8, l.size, Endian.little);
      d.setUint32(o + 12, l.flags, Endian.little);
      o += vmcodeLinkEntrySize;
    }
    out.setAll(elfOffset, elf);
    return out;
  }

  static Vmcode decode(Uint8List bytes) {
    final d = ByteData.sublistView(bytes);
    for (var i = 0; i < 8; i++) {
      if (bytes[i] != vmcodeMagic[i]) {
        throw const FormatException('bad vmcode magic');
      }
    }
    if (d.getUint32(8, Endian.little) != vmcodeVersion) {
      throw const FormatException('unsupported vmcode version');
    }
    final elfOffset = d.getUint64(16, Endian.little);
    final elfSize = d.getUint64(24, Endian.little);
    final count = d.getUint32(32, Endian.little);
    final entrySize = d.getUint32(36, Endian.little);
    final linksOffset = d.getUint32(40, Endian.little);
    if (d.getUint32(12, Endian.little) != elfOffset ||
        elfOffset % vmcodeElfAlignment != 0 ||
        entrySize != vmcodeLinkEntrySize ||
        linksOffset + count * entrySize > elfOffset ||
        elfOffset + elfSize != bytes.length) {
      throw const FormatException('inconsistent vmcode header');
    }
    for (var i = linksOffset + count * entrySize; i < elfOffset; i++) {
      if (bytes[i] != 0) throw const FormatException('nonzero header padding');
    }
    final links = <VmcodeLink>[];
    for (var i = 0; i < count; i++) {
      final o = linksOffset + i * entrySize;
      links.add(VmcodeLink(
          d.getUint32(o, Endian.little),
          d.getUint32(o + 4, Endian.little),
          d.getUint32(o + 8, Endian.little),
          d.getUint32(o + 12, Endian.little)));
    }
    return Vmcode(
      links: links,
      elf: Uint8List.sublistView(bytes, elfOffset, elfOffset + elfSize),
      baseTextSha256: Uint8List.fromList(bytes.sublist(48, 80)),
      patchElfSha256: Uint8List.fromList(bytes.sublist(80, 112)),
      baseTextSize: d.getUint32(112, Endian.little),
      patchTextSize: d.getUint32(116, Endian.little),
      linkedCodeBytes: d.getUint32(120, Endian.little),
      patchCodeBytes: d.getUint32(124, Endian.little),
    )..elfOffset = elfOffset;
  }
}
