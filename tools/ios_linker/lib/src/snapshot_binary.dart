// A release or patch snapshot binary: an ELF written by gen_snapshot
// (--snapshot_kind=app-aot-elf) or a Mach-O built from its assembly output
// (--snapshot_kind=app-aot-assembly, assembled and linked into
// App.framework/App as flutter_tools does).

import 'dart:io';
import 'dart:typed_data';

import 'elf.dart';
import 'macho.dart';

const textSymbol = '_kDartSnapshotText';
const dataSymbol = '_kDartSnapshotData';

class SnapshotBinary {
  SnapshotBinary._(this.path, this.format, this.bytes, this.textImage,
      this.dataImage, this.textFileOffset);

  /// Reads [path]. [textImageSize] is the sidecar's `meta text_image_size`;
  /// it is required to be consistent with an ELF symbol size and is used to
  /// bound the text image of a Mach-O (whose symbols carry no size).
  factory SnapshotBinary.read(String path, {int? textImageSize}) {
    final bytes = File(path).readAsBytesSync();
    if (isMachO(bytes)) {
      final m = MachO(bytes);
      if (m.cpuType != 0x0100000c) {
        throw FormatException('$path: Mach-O is not arm64');
      }
      final text = m.region(textSymbol, size: textImageSize);
      final data = m.region(dataSymbol);
      return SnapshotBinary._(
        path,
        'macho',
        bytes,
        Uint8List.sublistView(m.bytes, text.fileOffset, text.fileOffset + text.size),
        Uint8List.sublistView(m.bytes, data.fileOffset, data.fileOffset + data.size),
        m.sliceOffset + text.fileOffset,
      );
    }
    final elf = Elf(bytes);
    final text = dartTextImage(elf);
    if (textImageSize != null && text.length != textImageSize) {
      throw FormatException('$path: text image is ${text.length} bytes, the '
          'link info says $textImageSize (sidecar from another build?)');
    }
    final data = elf.symbolBytes(elf.symbols.containsKey(dataSymbol)
        ? dataSymbol
        : '_kDartIsolateSnapshotData');
    return SnapshotBinary._(
        path, 'elf', bytes, text, data, text.offsetInBytes);
  }

  final String path;

  /// 'elf' or 'macho'.
  final String format;
  final Uint8List bytes;

  /// The instructions image; link info code offsets are relative to it.
  final Uint8List textImage;
  final Uint8List dataImage;

  /// File offset of the instructions image (within the whole file).
  final int textFileOffset;
}
