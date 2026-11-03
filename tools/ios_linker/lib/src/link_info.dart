// Parser for the link info files written by gen_snapshot
// (--print_link_info_to). See docs/ios-linker/COMPILER_LINKER.md.

import 'dart:io';

class PoolEntry {
  PoolEntry(this.index, this.type, this.patchable, this.key);
  final int index;

  /// ObjectPoolBuilderEntry::EntryType: 0 immediate, 1 tagged object,
  /// 2 native function.
  final int type;
  final bool patchable;
  final String key;
}

class CallSite {
  CallSite(this.returnPcOffset, this.kind, this.entryKind, this.targetKey);
  final int returnPcOffset;

  /// Code::CallKind: 1 pc-relative call, 2 pc-relative TTS call,
  /// 3 pc-relative tail call, 4 call via code (pool).
  final int kind;
  final int entryKind;
  final String targetKey;

  bool get isPcRelative => kind >= 1 && kind <= 3;

  /// Offset of the branch instruction within the payload (all arm64 patterns
  /// are a single instruction).
  int get instructionOffset => returnPcOffset - 4;
}

class CodeEntry {
  CodeEntry(this.image, this.textOffset, this.size, this.key);
  final String image;
  final int textOffset;
  final int size;
  final String key;
  final List<CallSite> calls = [];
  int get end => textOffset + size;
}

/// Sidecar format version written by gen_snapshot --print_link_info_to.
const linkInfoHeader = '#osb-link-info 2';

class LinkInfo {
  /// Compiler identity: `snapshot_hash` and `features` of the gen_snapshot
  /// that wrote the file. A release and its patches must agree.
  final Map<String, String> identity = {};
  final Map<String, int> meta = {};
  final Map<String, int> classCid = {};
  final Map<String, int> staticId = {};
  final Map<String, int> selectorOffset = {};
  final Map<String, int> fieldOffset = {};
  final List<PoolEntry> pool = [];
  final List<CodeEntry> codes = [];
  final Map<String, List<int>> uses = {};
  final List<String> lines = [];

  static LinkInfo read(String path) {
    final info = LinkInfo();
    final text = File(path).readAsStringSync();
    final byOffset = <int, CodeEntry>{};
    var first = true;
    for (final line in text.split('\n')) {
      if (line.isEmpty) continue;
      if (first) {
        if (!line.startsWith('#osb-link-info ')) {
          throw FormatException('$path is not a link info file');
        }
        if (line != linkInfoHeader) {
          throw FormatException(
              '$path has format "$line", this tool reads "$linkInfoHeader"');
        }
        first = false;
        continue;
      }
      info.lines.add(line);
      final c = line.split('\t');
      switch (c[0]) {
        case 'id':
          info.identity[c[1]] = c[2];
        case 'meta':
          info.meta[c[1]] = int.parse(c[2]);
        case 'class':
          info.classCid.putIfAbsent(c[2], () => int.parse(c[1]));
        case 'field':
          info.fieldOffset.putIfAbsent(c[3], () => int.parse(c[1]));
        case 'static':
          info.staticId.putIfAbsent(c[2], () => int.parse(c[1]));
        case 'selector':
          info.selectorOffset.putIfAbsent(c[3], () => int.parse(c[2]));
        case 'pool':
          info.pool.add(PoolEntry(int.parse(c[1]), int.parse(c[2]),
              c[3] == '0', c.length > 4 ? c[4] : ''));
        case 'code':
          final e = CodeEntry(c[1], int.parse(c[2]), int.parse(c[3]), c[4]);
          info.codes.add(e);
          byOffset[e.textOffset] = e;
        case 'call':
          final owner = byOffset[int.parse(c[1])];
          if (owner == null) throw FormatException('call before code: $line');
          owner.calls.add(CallSite(int.parse(c[2]), int.parse(c[3]),
              int.parse(c[4]), c.length > 5 ? c[5] : '?'));
        case 'uses':
          info.uses[c[1]] = c.length > 2 && c[2].isNotEmpty
              ? c[2].split(',').map(int.parse).toList()
              : <int>[];
      }
    }
    if (!info.identity.containsKey('snapshot_hash') ||
        !info.identity.containsKey('features')) {
      throw FormatException('$path has no compiler identity lines');
    }
    info.codes.sort((a, b) => a.textOffset.compareTo(b.textOffset));
    return info;
  }

  /// Returns the code entry containing [textOffset], or null.
  CodeEntry? entryAt(int textOffset) {
    var lo = 0, hi = codes.length - 1;
    while (lo <= hi) {
      final mid = (lo + hi) >> 1;
      final e = codes[mid];
      if (textOffset < e.textOffset) {
        hi = mid - 1;
      } else if (textOffset >= e.end) {
        lo = mid + 1;
      } else {
        return e;
      }
    }
    return null;
  }

  int get codeBytes => codes.fold(0, (s, e) => s + e.size);

  /// Size of the instructions image, when the writer recorded it.
  int? get textImageSize => meta['text_image_size'];

  /// Null when [other] was written by the same compiler, else a reason.
  String? identityMismatch(LinkInfo other) {
    for (final k in const ['snapshot_hash', 'features']) {
      if (identity[k] != other.identity[k]) {
        return '$k differs: "${identity[k]}" vs "${other.identity[k]}"';
      }
    }
    return null;
  }
}
