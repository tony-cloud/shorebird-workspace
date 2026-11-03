// The mixed-mode runtime's gate budget (RUNTIME.md section 8.3), computed
// the same way the runtime does, so the linker refuses a patch the runtime
// would reject: every unlinked patch entry may need up to four entry gates,
// every call site (bl, blr) in unlinked code one return gate, plus a fixed
// reserve for exception-handler pcs.

import 'dart:typed_data';

import 'link_info.dart';

/// GATE_COUNT in the runtime's mixed_mode_gates_arm64.S (engine build
/// constant). Override with --gate-count when the engine differs.
const defaultGateCount = 131072;
const gateReserve = 1024;

class GateBudget {
  GateBudget(this.unlinkedEntries, this.unlinkedCallSites);
  final int unlinkedEntries, unlinkedCallSites;
  int get gatesNeeded => 4 * unlinkedEntries + unlinkedCallSites + gateReserve;

  @override
  String toString() => 'gates_needed=$gatesNeeded (4*$unlinkedEntries '
      'unlinked entries + $unlinkedCallSites call sites + $gateReserve)';
}

bool _isCall(int w) =>
    (w & 0xfc000000) == 0x94000000 || // bl
    (w & 0xfffffc1f) == 0xd63f0000; // blr

/// [linkedPatchOffsets]: patch payload offsets that have a link record.
/// Entry spans run to the next entry (or the image end), like the runtime's
/// instructions table.
GateBudget computeGateBudget(
    LinkInfo patch, Uint8List patchText, Set<int> linkedPatchOffsets) {
  final d = ByteData.sublistView(patchText);
  final codes = patch.codes; // sorted by offset
  var unlinked = 0, calls = 0;
  for (var i = 0; i < codes.length; i++) {
    final start = codes[i].textOffset;
    if (linkedPatchOffsets.contains(start)) continue;
    unlinked++;
    final end = i + 1 < codes.length ? codes[i + 1].textOffset : patchText.length;
    for (var pc = start; pc + 4 <= end; pc += 4) {
      if (_isCall(d.getUint32(pc, Endian.little))) calls++;
    }
  }
  return GateBudget(unlinked, calls);
}
