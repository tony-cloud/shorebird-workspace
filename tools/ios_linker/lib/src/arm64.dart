// AArch64 decoding of PC-relative instructions, independent of the compiler's
// own relocation records. Used to normalize call displacements and to prove
// that a code entry has no other references relative to its own address.

enum PcRelKind { b, bl, bcond, cbz, tbz, adr, adrp, ldrLiteral }

class PcRel {
  PcRel(this.kind, this.delta);
  final PcRelKind kind;

  /// Target minus instruction address (for adrp: page delta in bytes).
  final int delta;
}

int _signExtend(int value, int bits) {
  final sign = 1 << (bits - 1);
  return (value & (sign - 1)) - (value & sign);
}

/// Decodes [insn] if it is a PC-relative instruction, otherwise null.
PcRel? decodePcRel(int insn) {
  if ((insn & 0x7c000000) == 0x14000000) {
    final delta = _signExtend(insn & 0x03ffffff, 26) * 4;
    return PcRel((insn & 0x80000000) != 0 ? PcRelKind.bl : PcRelKind.b, delta);
  }
  if ((insn & 0xff000010) == 0x54000000) {
    return PcRel(PcRelKind.bcond, _signExtend((insn >> 5) & 0x7ffff, 19) * 4);
  }
  if ((insn & 0x7e000000) == 0x34000000) {
    return PcRel(PcRelKind.cbz, _signExtend((insn >> 5) & 0x7ffff, 19) * 4);
  }
  if ((insn & 0x7e000000) == 0x36000000) {
    return PcRel(PcRelKind.tbz, _signExtend((insn >> 5) & 0x3fff, 14) * 4);
  }
  if ((insn & 0x1f000000) == 0x10000000) {
    final imm = (((insn >> 5) & 0x7ffff) << 2) | ((insn >> 29) & 3);
    if ((insn & 0x80000000) == 0) {
      return PcRel(PcRelKind.adr, _signExtend(imm, 21));
    }
    return PcRel(PcRelKind.adrp, _signExtend(imm, 21) << 12);
  }
  // LDR (literal), LDRSW (literal), PRFM (literal), SIMD LDR (literal).
  if ((insn & 0x3b000000) == 0x18000000) {
    return PcRel(PcRelKind.ldrLiteral, _signExtend((insn >> 5) & 0x7ffff, 19) * 4);
  }
  return null;
}

/// Clears the 26-bit displacement of a B/BL instruction.
int clearBranchImm(int insn) => insn & 0xfc000000;

bool isBranchImm(int insn) => (insn & 0x7c000000) == 0x14000000;
