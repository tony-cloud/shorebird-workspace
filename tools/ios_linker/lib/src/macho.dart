// Minimal 64-bit Mach-O reader: enough to locate the Dart snapshot images
// in an App.framework binary built from a gen_snapshot assembly snapshot.
// Handles thin arm64 files and fat files (the arm64 slice is used).

import 'dart:typed_data';

const _mhMagic64 = 0xfeedfacf;
const _fatMagic = 0xcafebabe;
const _fatMagic64 = 0xcafebabf;
const _lcSegment64 = 0x19;
const _lcSymtab = 0x2;
const _cpuTypeArm64 = 0x0100000c;
const _nStab = 0xe0;
const _nTypeMask = 0x0e;
const _nSect = 0x0e;

class MachOSection {
  MachOSection(this.index, this.segment, this.name, this.addr, this.size,
      this.offset);

  /// 1-based section ordinal as used by nlist_64.n_sect.
  final int index;
  final String segment, name;
  final int addr, size, offset;
  int get end => addr + size;
}

class MachOSymbol {
  MachOSymbol(this.name, this.value, this.section, this.external);
  final String name;
  final int value;

  /// 1-based section ordinal.
  final int section;
  final bool external;
}

bool isMachO(Uint8List bytes) {
  if (bytes.length < 4) return false;
  final d = ByteData.sublistView(bytes);
  final le = d.getUint32(0, Endian.little);
  final be = d.getUint32(0, Endian.big);
  return le == _mhMagic64 || be == _fatMagic || be == _fatMagic64;
}

class MachO {
  /// [bytes] is the whole file; a fat file is narrowed to its arm64 slice.
  factory MachO(Uint8List bytes) {
    final d = ByteData.sublistView(bytes);
    final be = d.getUint32(0, Endian.big);
    if (be == _fatMagic || be == _fatMagic64) {
      final is64 = be == _fatMagic64;
      final n = d.getUint32(4, Endian.big);
      final archSize = is64 ? 32 : 20;
      for (var i = 0; i < n; i++) {
        final o = 8 + i * archSize;
        final cpu = d.getUint32(o, Endian.big);
        final sub = d.getUint32(o + 4, Endian.big) & 0x00ffffff;
        if (cpu != _cpuTypeArm64 || sub != 0) continue;
        final off = is64 ? d.getUint64(o + 8, Endian.big) : d.getUint32(o + 8, Endian.big);
        final size = is64 ? d.getUint64(o + 16, Endian.big) : d.getUint32(o + 12, Endian.big);
        return MachO._(Uint8List.sublistView(bytes, off, off + size), off);
      }
      throw const FormatException('fat Mach-O has no arm64 slice');
    }
    return MachO._(bytes, 0);
  }

  MachO._(this.bytes, this.sliceOffset) {
    final d = ByteData.sublistView(bytes);
    if (d.getUint32(0, Endian.little) != _mhMagic64) {
      throw const FormatException('not a 64-bit Mach-O');
    }
    cpuType = d.getUint32(4, Endian.little);
    cpuSubtype = d.getUint32(8, Endian.little) & 0x00ffffff;
    fileType = d.getUint32(12, Endian.little);
    final ncmds = d.getUint32(16, Endian.little);
    var lc = 32;
    int? symoff, nsyms, stroff;
    for (var i = 0; i < ncmds; i++) {
      final cmd = d.getUint32(lc, Endian.little);
      final cmdsize = d.getUint32(lc + 4, Endian.little);
      if (cmd == _lcSegment64) {
        final segname = _fixedString(lc + 8, 16);
        final nsects = d.getUint32(lc + 64, Endian.little);
        for (var s = 0; s < nsects; s++) {
          final so = lc + 72 + s * 80;
          sections.add(MachOSection(
            sections.length + 1,
            _fixedString(so + 16, 16).isEmpty ? segname : _fixedString(so + 16, 16),
            _fixedString(so, 16),
            d.getUint64(so + 32, Endian.little),
            d.getUint64(so + 40, Endian.little),
            d.getUint32(so + 48, Endian.little),
          ));
        }
      } else if (cmd == _lcSymtab) {
        symoff = d.getUint32(lc + 8, Endian.little);
        nsyms = d.getUint32(lc + 12, Endian.little);
        stroff = d.getUint32(lc + 16, Endian.little);
      }
      lc += cmdsize;
    }
    if (symoff != null) {
      for (var i = 0; i < nsyms!; i++) {
        final o = symoff + i * 16;
        final strx = d.getUint32(o, Endian.little);
        final type = d.getUint8(o + 4);
        final sect = d.getUint8(o + 5);
        if ((type & _nStab) != 0 || (type & _nTypeMask) != _nSect) continue;
        final sym = MachOSymbol(_cString(stroff! + strx),
            d.getUint64(o + 8, Endian.little), sect, (type & 0x01) != 0);
        symbols.add(sym);
        byName.putIfAbsent(sym.name, () => sym);
      }
    }
  }

  final Uint8List bytes;

  /// Offset of this slice in the original file (0 for a thin file).
  final int sliceOffset;
  late final int cpuType, cpuSubtype, fileType;
  final List<MachOSection> sections = [];
  final List<MachOSymbol> symbols = [];
  final Map<String, MachOSymbol> byName = {};

  String _fixedString(int offset, int max) {
    var end = offset;
    while (end < offset + max && bytes[end] != 0) {
      end++;
    }
    return String.fromCharCodes(bytes, offset, end);
  }

  String _cString(int offset) {
    var end = offset;
    while (bytes[end] != 0) {
      end++;
    }
    return String.fromCharCodes(bytes, offset, end);
  }

  /// The symbol for a gen_snapshot assembly label. Assembly labels are
  /// emitted verbatim, so `_kDartSnapshotText` is the symbol name (C code
  /// sees it as `kDartSnapshotText`); accept a doubled underscore too.
  MachOSymbol? findSnapshotSymbol(String label) =>
      byName[label] ?? byName['_$label'];

  /// The region of [label]: from the symbol to the next symbol in the same
  /// section that starts at a higher address, or to the section end.
  /// [size], when given (e.g. the sidecar's text_image_size), must fit in
  /// that region and narrows it.
  ({int fileOffset, int vmaddr, int size, MachOSection section}) region(
      String label,
      {int? size}) {
    final sym = findSnapshotSymbol(label);
    if (sym == null) throw StateError('symbol $label not found');
    final sect = sections[sym.section - 1];
    var end = sect.end;
    for (final s in symbols) {
      if (s.section == sym.section && s.value > sym.value && s.value < end) {
        end = s.value;
      }
    }
    final available = end - sym.value;
    if (size != null) {
      if (size > available) {
        throw StateError('$label: requested $size bytes, only $available '
            'before the next symbol or section end');
      }
    }
    if (sect.offset == 0) {
      throw StateError('$label lies in a zero-fill section');
    }
    return (
      fileOffset: sect.offset + (sym.value - sect.addr),
      vmaddr: sym.value,
      size: size ?? available,
      section: sect,
    );
  }

  Uint8List regionBytes(String label, {int? size}) {
    final r = region(label, size: size);
    return Uint8List.sublistView(bytes, r.fileOffset, r.fileOffset + r.size);
  }
}
