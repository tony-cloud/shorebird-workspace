// Minimal ELF64 little-endian reader: enough to locate a Dart snapshot's
// instructions image by symbol.

import 'dart:typed_data';

class ElfSymbol {
  ElfSymbol(this.name, this.value, this.size);
  final String name;
  final int value;
  final int size;
}

class _Section {
  _Section(this.name, this.type, this.addr, this.offset, this.size,
      this.link, this.entsize);
  final int name, type, addr, offset, size, link, entsize;
}

class Elf {
  Elf(this.bytes) {
    final d = ByteData.sublistView(bytes);
    if (bytes.length < 64 ||
        bytes[0] != 0x7f ||
        bytes[1] != 0x45 ||
        bytes[2] != 0x4c ||
        bytes[3] != 0x46 ||
        bytes[4] != 2 ||
        bytes[5] != 1) {
      throw const FormatException('not an ELF64 little-endian file');
    }
    machine = d.getUint16(0x12, Endian.little);
    final shoff = d.getUint64(0x28, Endian.little);
    final shentsize = d.getUint16(0x3a, Endian.little);
    final shnum = d.getUint16(0x3c, Endian.little);
    for (var i = 0; i < shnum; i++) {
      final o = shoff + i * shentsize;
      _sections.add(_Section(
        d.getUint32(o, Endian.little),
        d.getUint32(o + 4, Endian.little),
        d.getUint64(o + 16, Endian.little),
        d.getUint64(o + 24, Endian.little),
        d.getUint64(o + 32, Endian.little),
        d.getUint32(o + 40, Endian.little),
        d.getUint64(o + 56, Endian.little),
      ));
    }
    for (final s in _sections) {
      if (s.type != 2 && s.type != 11) continue; // SHT_SYMTAB / SHT_DYNSYM
      final strtab = _sections[s.link];
      for (var o = s.offset; o + 24 <= s.offset + s.size; o += s.entsize) {
        final nameOff = d.getUint32(o, Endian.little);
        final name = _cString(strtab.offset + nameOff);
        if (name.isEmpty) continue;
        symbols.putIfAbsent(
            name,
            () => ElfSymbol(name, d.getUint64(o + 8, Endian.little),
                d.getUint64(o + 16, Endian.little)));
      }
    }
  }

  final Uint8List bytes;
  late final int machine;
  final List<_Section> _sections = [];
  final Map<String, ElfSymbol> symbols = {};

  String _cString(int offset) {
    var end = offset;
    while (bytes[end] != 0) {
      end++;
    }
    return String.fromCharCodes(bytes, offset, end);
  }

  /// File offset of virtual address [vaddr].
  int fileOffsetOf(int vaddr) {
    for (final s in _sections) {
      if (s.type == 8) continue; // SHT_NOBITS
      if (s.addr != 0 && vaddr >= s.addr && vaddr < s.addr + s.size) {
        return s.offset + (vaddr - s.addr);
      }
    }
    throw StateError('address 0x${vaddr.toRadixString(16)} not in a section');
  }

  /// Bytes of the region starting at symbol [name], with the symbol's size.
  Uint8List symbolBytes(String name) {
    final sym = symbols[name];
    if (sym == null) throw StateError('symbol $name not found');
    final start = fileOffsetOf(sym.value);
    return Uint8List.sublistView(bytes, start, start + sym.size);
  }
}

/// The instructions image of a Dart AOT ELF snapshot. Code offsets in link
/// info are relative to the start of this image.
Uint8List dartTextImage(Elf elf) {
  for (final name in const [
    '_kDartSnapshotText',
    '_kDartIsolateSnapshotInstructions',
  ]) {
    if (elf.symbols.containsKey(name)) return elf.symbolBytes(name);
  }
  throw StateError('no Dart instructions image symbol');
}
