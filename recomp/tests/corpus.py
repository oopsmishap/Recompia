# SPDX-License-Identifier: GPL-3.0-or-later
"""The harness's corpus: one AArch64 snippet per case, with the machine state it must leave behind.

Every `expect_*` value is derived from the Arm Architecture Reference Manual (the pseudocode of the instruction), not from
the lifter: the harness exists to disagree with the lifter when the lifter is wrong.

  regs / vregs    the state before the snippet runs (vregs: a register is (low 64 bits, high 64 bits), or just the low half)
  nzcv            the flags before it runs
  mem             bytes of the case's 4 KiB data page before it runs
  expect_regs     the X registers that must differ after it (every register it does not name must be unchanged)
  expect_any      X registers whose final value is not checked (an address that depends on the case's position)
  expect_v        the vector registers that must differ (same shape as vregs)
  expect_nzcv     the flags after it, when the case states them
  expect_mem      bytes of the page that must differ (the rest of the page must be unchanged)
  expect_ev       the runtime event the snippet must raise: 0 none, 1 trap, 2 SVC, 3 indirect call, 4 host import
  calls           {token: case name}: the token in the assembly becomes that case's entry address

`DATA` in a value stands for the case's own data page address; the harness fills it in.
"""
from harness import DATA


class Case:
    def __init__(self, name, asm, regs=None, vregs=None, nzcv=0, mem=None, expect_regs=None, expect_v=None,
                 expect_nzcv=None, expect_mem=None, expect_any=(), expect_ev=0, expect_evarg=None, calls=None,
                 allow_bad=False, why=''):
        self.name = name
        self.asm = asm
        self.regs = regs or {}
        self.vregs = vregs or {}
        self.nzcv = nzcv
        self.mem = mem or {}
        self.expect_regs = expect_regs or {}
        self.expect_v = expect_v or {}
        self.expect_nzcv = expect_nzcv
        self.expect_mem = expect_mem or {}
        self.expect_any = set(expect_any)
        self.expect_ev = expect_ev
        self.expect_evarg = expect_evarg
        self.calls = calls or {}
        self.allow_bad = allow_bad
        self.why = why


def _w(v):
    return v & 0xffffffffffffffff


def _q(word, low):
    """A 64-bit little-endian word as 8 bytes of the data page."""
    return (_w(word) if low else 0).to_bytes(8, 'little')


CASES = [
    # ------------------------------------------------------------------ data processing, immediate
    Case('add_imm64', 'add x0, x1, #0x123\nret', regs={1: 0x1000}, expect_regs={0: 0x1123},
         why='ADD (immediate), 64-bit, no flags'),
    Case('add_imm32_wrap', 'add w0, w1, #1\nret', regs={1: 0xffffffff}, expect_regs={0: 0},
         why='the 32-bit form writes the low half and zeroes the top half of the X register'),
    Case('subs_borrow_flags', 'subs x0, x1, #1\nret', regs={1: 0}, expect_regs={0: 0xffffffffffffffff},
         expect_nzcv=0b1000, why='0 - 1: all ones, N=1, Z=0, C=0 (borrow), V=0'),
    Case('adds_carry_flags', 'adds x0, x1, x2\nret', regs={1: 0xffffffffffffffff, 2: 1}, expect_regs={0: 0},
         expect_nzcv=0b0110, why='all ones + 1: 0, Z=1, C=1, N=0, V=0'),
    Case('adds_overflow_flags', 'adds w0, w1, w2\nret', regs={1: 0x7fffffff, 2: 1}, expect_regs={0: 0x80000000},
         expect_nzcv=0b1001, why='INT32_MAX + 1: N=1, Z=0, C=0, V=1'),
    Case('movz_lsl', 'movz x0, #0x1234, lsl #16\nret', expect_regs={0: 0x12340000},
         why='MOVZ zeroes every bit it does not write'),
    Case('movn', 'movn x0, #0\nret', expect_regs={0: 0xffffffffffffffff}, why='MOVN #0 sets every bit'),
    Case('movk_keeps', 'movz x0, #0x1234\nmovk x0, #0x5678, lsl #16\nret', expect_regs={0: 0x56781234},
         why='MOVK writes 16 bits and keeps the rest'),
    Case('and_imm_bitmask', 'and x0, x1, #0xff\nret', regs={1: 0x123456789abcdef0}, expect_regs={0: 0xf0},
         why='AND (immediate): the bitmask immediate 0xff'),
    Case('ands_logical_flags', 'ands x0, x1, #0xff\nret', regs={1: 0x100}, expect_regs={0: 0}, expect_nzcv=0b0100,
         why='ANDS: Z=1, and a logical instruction clears C and V'),

    # ------------------------------------------------------------------ data processing, register
    Case('orr_shifted', 'orr x0, x1, x2, lsl #4\nret', regs={1: 0xf, 2: 1}, expect_regs={0: 0x1f},
         why='ORR (shifted register)'),
    Case('bic_shifted', 'bic x0, x1, x2, lsr #1\nret', regs={1: 0xffff, 2: 0x10}, expect_regs={0: 0xfff7},
         why='BIC clears the shifted bits: ~(0x10 >> 1) = ~8, so 0xffff & ~8'),
    Case('sub_extended', 'sub x0, x1, w2, uxtb\nret', regs={1: 0x100, 2: 0x1234}, expect_regs={0: 0xcc},
         why='SUB (extended register): W2 zero-extended from its low byte, 0x100 - 0x34'),
    Case('adds_extended_flags', 'adds x0, x1, w2, sxtb\nret', regs={1: 0x10, 2: 0xff}, expect_regs={0: 0xf},
         expect_nzcv=0b0010,
         why='SXTB of 0xff is -1, so 0x10 + all-ones carries out of bit 63: C=1 and no other flag'),
    Case('extr_rotate', 'extr x0, x1, x2, #8\nret', regs={1: 0x1122334455667788, 2: 0x99aabbccddeeff00},
         expect_regs={0: 0x8899aabbccddeeff}, why='EXTR shifts the 128-bit x1:x2 right by 8: (x1 << 56) | (x2 >> 8)'),
    Case('adc_carry_in', 'adc x0, x1, x2\nret', regs={1: 5, 2: 6}, nzcv=0b0010, expect_regs={0: 12},
         why='ADC adds the carry in: 5 + 6 + 1'),
    Case('adcs_flags', 'adcs x0, x1, x2\nret', regs={1: 0xffffffffffffffff, 2: 0}, nzcv=0b0010, expect_regs={0: 0},
         expect_nzcv=0b0110, why='all ones + 0 + carry: 0, Z=1, C=1'),
    Case('sbc_carry_in', 'sbc x0, x1, x2\nret', regs={1: 10, 2: 3}, nzcv=0b0010, expect_regs={0: 7},
         why='SBC is x1 + NOT(x2) + C = 10 - 3 + 0'),
    Case('ccmp_immediate_flags', 'cmp x1, #0\nccmp x1, x2, #2, eq\nret', regs={1: 5, 2: 5}, expect_nzcv=0b0010,
         why='the condition (eq) fails, so CCMP sets the flags from its immediate (0010) instead of comparing'),

    # ------------------------------------------------------------------ conditional select
    Case('csel_lt_taken', 'cmp x1, x2\ncsel x0, x3, x4, lt\nret', regs={1: 1, 2: 2, 3: 0xaa, 4: 0xbb},
         expect_regs={0: 0xaa}, why='CSEL: 1 < 2 signed, so x3'),
    Case('csel_lt_not_taken', 'cmp x1, x2\ncsel x0, x3, x4, lt\nret', regs={1: 2, 2: 1, 3: 0xaa, 4: 0xbb},
         expect_regs={0: 0xbb}, why='CSEL: 2 < 1 is false, so x4'),
    Case('csinc', 'cmp x1, x2\ncsinc x0, x3, x4, eq\nret', regs={1: 5, 2: 5, 3: 0x10, 4: 0x20},
         expect_regs={0: 0x10}, why='CSINC with the condition true takes x3 unchanged'),
    Case('cset_alias', 'cmp x1, x2\ncset x0, ne\nret', regs={1: 7, 2: 7}, expect_regs={0: 0},
         why='CSET ne is CSINC x0, xzr, xzr, eq: eq is true, so 0'),

    # ------------------------------------------------------------------ multiply / divide
    Case('madd', 'madd x0, x1, x2, x3\nret', regs={1: 6, 2: 7, 3: 100}, expect_regs={0: 142}, why='6 * 7 + 100'),
    Case('msub', 'msub x0, x1, x2, x3\nret', regs={1: 6, 2: 7, 3: 100}, expect_regs={0: 58}, why='100 - 6 * 7'),
    Case('mul_wrapping', 'mul x0, x1, x2\nret', regs={1: 0xffffffffffffffff, 2: 2},
         expect_regs={0: 0xfffffffffffffffe}, why='MUL wraps at 64 bits'),
    Case('umulh', 'umulh x0, x1, x2\nret', regs={1: 0xffffffffffffffff, 2: 0xffffffffffffffff},
         expect_regs={0: 0xfffffffffffffffe}, why='UMULH: the high half of the 128-bit product'),
    Case('smulh_negative', 'smulh x0, x1, x2\nret', regs={1: _w(-2), 2: _w(-3)}, expect_regs={0: 0},
         why='SMULH of two small negatives: the high half of +6 is 0'),
    Case('sdiv_neg', 'sdiv x0, x1, x2\nret', regs={1: _w(-7), 2: 2}, expect_regs={0: _w(-3)},
         why='SDIV truncates towards zero: -7 / 2 = -3'),
    Case('udiv_by_zero', 'udiv x0, x1, x2\nret', regs={1: 42, 2: 0}, expect_regs={0: 0},
         why='UDIV by zero yields 0'),

    # ------------------------------------------------------------------ shifts by register
    Case('lslv_32', 'lslv w0, w1, w2\nret', regs={1: 1, 2: 33}, expect_regs={0: 2},
         why='the 32-bit form uses the low 5 bits of the amount: 33 & 31 = 1'),
    Case('asrv_64', 'asrv x0, x1, x2\nret', regs={1: 0x8000000000000000, 2: 4},
         expect_regs={0: 0xf800000000000000}, why='ASRV shifts arithmetically'),
    Case('rorv', 'rorv x0, x1, x2\nret', regs={1: 0x123456789abcdef0, 2: 8}, expect_regs={0: 0xf0123456789abcde},
         why='RORV rotates right by the low 6 bits'),

    # ------------------------------------------------------------------ bit manipulation
    Case('rbit', 'rbit x0, x1\nret', regs={1: 1}, expect_regs={0: 0x8000000000000000}, why='RBIT reverses all 64 bits'),
    Case('rev32', 'rev32 x0, x1\nret', regs={1: 0x1122334455667788}, expect_regs={0: 0x4433221188776655},
         why='REV32 reverses the bytes in each 32-bit word, not the words'),
    Case('clz', 'clz x0, x1\nret', regs={1: 0x00ff000000000000}, expect_regs={0: 8}, why='CLZ counts leading zeros'),
    Case('sbfm_sxtb', 'sxtb x0, w1\nret', regs={1: 0x80}, expect_regs={0: 0xffffffffffffff80},
         why='SXTB is SBFM: sign-extends bit 7 to 64 bits'),
    Case('ubfx', 'ubfx x0, x1, #4, #8\nret', regs={1: 0x123456789abcdef0}, expect_regs={0: 0xef},
         why='UBFX extracts 8 bits from bit 4'),
    Case('bfi', 'bfi x0, x1, #8, #8\nret', regs={0: 0xffffffffffffffff, 1: 0xab},
         expect_regs={0: 0xffffffffffffabff}, why='BFI inserts 8 bits at bit 8 and keeps the rest'),

    # ------------------------------------------------------------------ loads and stores
    Case('str_ldr_imm', 'str x1, [x0, #16]\nldr x2, [x0, #16]\nret', regs={0: DATA, 1: 0x1122334455667788},
         expect_regs={2: 0x1122334455667788}, expect_mem={16: _q(0x1122334455667788, True)},
         why='STR then LDR at the same scaled immediate offset'),
    Case('strb_wr8', 'strb w1, [x0, #3]\nret', regs={0: DATA, 1: 0x1ff}, expect_mem={3: b'\xff'},
         why='STRB writes one byte'),
    Case('strh_wr16', 'strh w1, [x0, #2]\nret', regs={0: DATA, 1: 0x12345678}, expect_mem={2: b'\x78\x56'},
         why='STRH writes two bytes'),
    Case('ldrsb', 'ldrsb x2, [x0]\nret', regs={0: DATA}, mem={0: b'\x80'}, expect_regs={2: 0xffffffffffffff80},
         why='LDRSB sign-extends the byte to the whole register'),
    Case('ldr_w_zero_extends', 'ldr w2, [x0]\nret', regs={0: DATA, 2: 0xffffffffffffffff},
         mem={0: b'\x78\x56\x34\x12'}, expect_regs={2: 0x12345678},
         why='LDR (32-bit) zeroes the top half of the destination'),
    Case('stp_ldp', 'stp x1, x2, [x0, #16]\nldp x3, x4, [x0, #16]\nret', regs={0: DATA, 1: 1, 2: 2},
         expect_regs={3: 1, 4: 2}, expect_mem={16: _q(1, True) + _q(2, True)}, why='STP then LDP'),
    Case('str_pre_index', 'str x1, [x0, #8]!\nret', regs={0: DATA, 1: 0x55}, expect_regs={0: DATA + 8},
         expect_mem={8: _q(0x55, True)}, why='a pre-indexed store writes the address back'),
    Case('str_post_index', 'str x1, [x0], #8\nret', regs={0: DATA, 1: 0x66}, expect_regs={0: DATA + 8},
         expect_mem={0: _q(0x66, True)}, why='a post-indexed store writes at the old address and adds afterwards'),
    Case('ldr_literal', 'ldr x0, 1f\nb 2f\n1: .quad 0x123456789abcdef0\n2: ret', expect_regs={0: 0x123456789abcdef0},
         why='LDR (literal) reads a constant that sits in the code', allow_bad=True),

    # ------------------------------------------------------------------ branches
    Case('b_cond_taken', 'subs x0, x1, x1\nb.eq 1f\nmov x0, #1\n1:\nret', regs={1: 9}, expect_regs={0: 0},
         why='B.EQ after a SUBS that sets Z'),
    Case('b_cond_not_taken', 'cmp x1, #0\nb.eq 1f\nmov x0, #1\n1:\nret', regs={1: 9}, expect_regs={0: 1},
         why='B.EQ not taken falls through'),
    Case('cbz_taken', 'cbz x1, 1f\nmov x0, #1\n1:\nret', regs={1: 0}, expect_regs={0: 0},
         why='CBZ branches when the register is zero'),
    Case('tbnz_bit', 'tbnz x1, #3, 1f\nmov x0, #1\n1:\nret', regs={1: 0b1000}, expect_regs={0: 0},
         why='TBNZ branches when the bit is set'),
    Case('tbz_bit_clear', 'tbz x1, #3, 1f\nmov x0, #1\n1:\nret', regs={1: 0b1}, expect_regs={0: 0},
         why='TBZ branches when the bit is clear'),
    Case('call_and_return', 'bl {callee}\nret', calls={'callee': 'callee_set_x0'}, expect_regs={0: 7},
         expect_any={30}, why='BL reaches another lifted function and RET comes back'),
    Case('callee_set_x0', 'mov x0, #7\nret', expect_regs={0: 7}, why='the callee of call_and_return'),
    Case('blr_events', 'mov x2, #0x1234\nblr x2\nret', expect_regs={2: 0x1234}, expect_any={30}, expect_ev=3,
         expect_evarg=0x1234, why='BLR is an indirect call, which the runtime resolves'),

    # ------------------------------------------------------------------ exclusive monitor
    Case('ldxr_stxr_ok', 'ldxr x2, [x0]\nadd x2, x2, #1\nstxr w3, x2, [x0]\nret', regs={0: DATA},
         mem={0: _q(1, True)}, expect_regs={2: 2, 3: 0}, expect_mem={0: _q(2, True)},
         why='LDXR/STXR on an untouched address succeeds'),
    Case('stxr_without_ldxr', 'mov x2, #1\nstxr w3, x2, [x0]\nret', regs={0: DATA}, expect_regs={2: 1, 3: 1},
         why='STXR without a matching LDXR fails'),
    Case('ldar_stlr', 'ldr x1, [x0, #8]\nstlr x1, [x0]\nret', regs={0: DATA},
         mem={8: _q(0x1122334455667788, True)}, expect_regs={1: 0x1122334455667788},
         expect_mem={0: _q(0x1122334455667788, True)}, why='STLR stores with release ordering'),

    # ------------------------------------------------------------------ system
    Case('mrs_nzcv', 'mrs x0, nzcv\nret', nzcv=0b1010, expect_regs={0: 0xa0000000},
         why='MRS NZCV returns the flags in bits 31:28'),
    Case('msr_nzcv', 'msr nzcv, x0\nret', regs={0: 0x60000000}, expect_nzcv=0b0110,
         why='MSR NZCV takes the flags from bits 31:28'),
    Case('mrs_tpidr', 'mrs x0, tpidr_el0\nret', expect_regs={0: 0}, why='TPIDR_EL0 starts at 0 in the harness context'),
    Case('svc', 'svc #0x42\nret', expect_ev=2, expect_evarg=0x42, why='SVC reaches the runtime with its number'),
    Case('udf_traps', 'udf #0\nret', expect_ev=1, why='an unallocated word raises an undefined-instruction trap'),
    Case('nop_only', 'nop\nret', why='NOP changes nothing'),
    Case('ret_returns', 'mov x0, #1\nret', expect_regs={0: 1}, why='RET ends the lifted function'),

    # ------------------------------------------------------------------ floating point
    Case('fadd_double', 'fadd d0, d1, d2\nret', vregs={1: 0x3ff0000000000000, 2: 0x4000000000000000},
         expect_v={0: 0x4008000000000000}, why='FADD 1.0 + 2.0 = 3.0'),
    Case('fsub_double', 'fsub d0, d1, d2\nret', vregs={1: 0x4008000000000000, 2: 0x3ff0000000000000},
         expect_v={0: 0x4000000000000000}, why='FSUB 3.0 - 1.0 = 2.0'),
    Case('fmul_single', 'fmul s0, s1, s2\nret', vregs={1: 0x3fc00000, 2: 0x40000000},
         expect_v={0: 0x40400000}, why='FMUL 1.5 * 2.0 = 3.0 in the low 32 bits of v0'),
    Case('fdiv_double', 'fdiv d0, d1, d2\nret', vregs={1: 0x4000000000000000, 2: 0x4008000000000000},
         expect_v={0: 0x3fe5555555555555}, why='FDIV 2.0 / 3.0'),
    Case('fneg_abs', 'fneg d0, d1\nfabs d2, d1\nret', vregs={1: 0x4008000000000000},
         expect_v={0: 0xc008000000000000, 2: 0x4008000000000000}, why='FNEG flips the sign bit, FABS clears it'),
    Case('fsqrt', 'fsqrt d0, d1\nret', vregs={1: 0x4010000000000000}, expect_v={0: 0x4000000000000000},
         why='FSQRT 4.0 = 2.0'),
    Case('fcmp_lt', 'fcmp d0, d1\nret', vregs={0: 0x3ff0000000000000, 1: 0x4000000000000000}, expect_nzcv=0b1000,
         why='FCMP 1.0 < 2.0: N=1, Z=0, C=0, V=0'),
    Case('fcmp_eq', 'fcmp d0, d1\nret', vregs={0: 0x4000000000000000, 1: 0x4000000000000000}, expect_nzcv=0b0110,
         why='FCMP equal: Z=1, C=1'),
    Case('fcmp_nan', 'fcmp d0, d1\nret', vregs={0: 0x7ff8000000000000, 1: 0x3ff0000000000000}, expect_nzcv=0b0011,
         why='FCMP with a NaN: C=1, V=1'),
    Case('fcvtzs', 'fcvtzs x0, d0\nret', vregs={0: 0xc01e000000000000}, expect_regs={0: _w(-7)},
         why='FCVTZS rounds -7.5 towards zero to -7'),
    Case('scvtf', 'scvtf d0, x1\nret', regs={1: _w(-3)}, expect_v={0: 0xc008000000000000},
         why='SCVTF converts -3 to double'),
    Case('fmadd_double', 'fmadd d0, d1, d2, d3\nret',
         vregs={1: 0x4000000000000000, 2: 0x4008000000000000, 3: 0x3ff0000000000000},
         expect_v={0: 0x401c000000000000}, why='FMADD 2.0 * 3.0 + 1.0 = 7.0'),
    Case('fmov_imm', 'fmov d0, #1.0\nret', expect_v={0: 0x3ff0000000000000}, why='FMOV (immediate) 1.0'),
]

# ----------------------------------------------------------------------------------------------------------------
# Second batch: the forms the game leans on that the first batch did not reach (tier-2 atomics, SIMD, unaligned and
# negative-offset accesses, tail branches).
# ----------------------------------------------------------------------------------------------------------------
CASES += [
    # ---- unscaled and negative offsets
    Case('ldur_negative', 'ldur x0, [x1, #-8]\nret', regs={1: DATA + 16}, mem={8: _q(0xaa, True)},
         expect_regs={0: 0xaa}, why='LDUR reads at base - 8'),
    Case('stur_negative', 'stur x0, [x1, #-8]\nret', regs={0: 0x1234, 1: DATA + 16},
         expect_mem={8: _q(0x1234, True)}, why='STUR writes at base - 8'),
    Case('ldp_negative', 'ldp x0, x1, [x2, #-16]\nret', regs={2: DATA + 16}, mem={0: _q(1, True), 8: _q(2, True)},
         expect_regs={0: 1, 1: 2}, why='LDP with a negative signed offset'),
    Case('strb_reg_offset', 'strb w0, [x1, x2]\nret', regs={0: 0x7f, 1: DATA, 2: 2}, expect_mem={2: b'\x7f'},
         why='STRB with a register offset'),
    Case('ldrb_sxtw_offset', 'ldrb w0, [x1, w2, sxtw]\nret', regs={1: DATA + 4, 2: 0xffffffff},
         mem={3: b'\x5a'}, expect_regs={0: 0x5a}, why='LDRB with a sign-extended register offset of -1'),
    Case('add_imm_max', 'add x0, x1, #0xfff\nret', regs={1: 1}, expect_regs={0: 0x1000},
         why='ADD (immediate) with the largest 12-bit immediate'),

    # ---- ordered and exclusive
    Case('ldar_w', 'ldar w0, [x1]\nret', regs={1: DATA}, mem={0: b'\x44\x33\x22\x11'},
         expect_regs={0: 0x11223344}, why='LDAR (32-bit) zero-extends into the X register'),
    Case('stlr_w', 'stlr w0, [x1]\nret', regs={0: 0x55667788, 1: DATA}, expect_mem={0: b'\x88\x77\x66\x55'},
         why='STLR (32-bit) writes four bytes'),
    Case('clrex_clears_monitor', 'ldxr x0, [x1]\nclrex\nstxr w2, x0, [x1]\nret', regs={1: DATA},
         mem={0: _q(1, True)}, expect_regs={0: 1, 2: 1},
         why='CLREX closes the monitor, so the following STXR fails'),

    # ---- tier 2: LSE atomics
    Case('cas_match', 'cas x0, x1, [x2]\nret', regs={0: 5, 1: 9, 2: DATA}, mem={0: _q(5, True)},
         expect_regs={0: 5}, expect_mem={0: _q(9, True)}, why='CAS swaps when the memory holds the expected value'),
    Case('cas_mismatch', 'cas x0, x1, [x2]\nret', regs={0: 7, 1: 9, 2: DATA}, mem={0: _q(5, True)},
         expect_regs={0: 5}, expect_mem={0: _q(5, True)}, why='CAS loads the memory when the comparison fails'),
    Case('swp', 'swp x0, x1, [x2]\nret', regs={0: 9, 2: DATA}, mem={0: _q(5, True)},
         expect_regs={1: 5}, expect_mem={0: _q(9, True)},
         why='SWP stores the FIRST operand and puts the old value in the second'),
    Case('ldadd', 'ldadd x0, x1, [x2]\nret', regs={0: 3, 2: DATA}, mem={0: _q(5, True)},
         expect_regs={1: 5}, expect_mem={0: _q(8, True)},
         why='LDADD adds the first operand and puts the old value in the second'),
    Case('ldset', 'ldset x0, x1, [x2]\nret', regs={0: 0b0100, 2: DATA}, mem={0: _q(0b1010, True)},
         expect_regs={1: 0b1010}, expect_mem={0: _q(0b1110, True)},
         why='LDSET sets the bits of the first operand and returns the old value in the second'),
    Case('ldclr', 'ldclr x0, x1, [x2]\nret', regs={0: 0b0100, 2: DATA}, mem={0: _q(0b1110, True)},
         expect_regs={1: 0b1110}, expect_mem={0: _q(0b1010, True)},
         why='LDCLR clears the bits of the first operand and returns the old value in the second'),
    Case('ldeor', 'ldeor x0, x1, [x2]\nret', regs={0: 0b1100, 2: DATA}, mem={0: _q(0b1010, True)},
         expect_regs={1: 0b1010}, expect_mem={0: _q(0b0110, True)},
         why='LDEOR flips the bits of the first operand and returns the old value in the second'),

    # ---- SIMD
    Case('simd_add_4s', 'add v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x000000140000000a, 0x000000280000001e)},
         expect_v={0: (0x000000160000000b, 0x0000002c00000021)}, why='vector ADD on four 32-bit lanes'),
    Case('simd_movi_4s', 'movi v0.4s, #1\nret', expect_v={0: (0x0000000100000001, 0x0000000100000001)},
         why='MOVI fills all four 32-bit lanes'),
    Case('simd_dup_4s', 'dup v0.4s, w1\nret', regs={1: 0x1234},
         expect_v={0: (0x0000123400001234, 0x0000123400001234)}, why='DUP broadcasts a general register'),
    Case('simd_orr_16b', 'orr v0.16b, v1.16b, v2.16b\nret',
         vregs={1: (0x00ff00ff00ff00ff, 0x0f0f0f0f0f0f0f0f), 2: (0x0000ffff0000ffff, 0xf0f0f0f0f0f0f0f0)},
         expect_v={0: (0x00ffffff00ffffff, 0xffffffffffffffff)}, why='bitwise OR over the 16 bytes'),
    Case('simd_ld1_16b', 'ld1 {v0.16b}, [x1]\nret', regs={1: DATA},
         mem={0: bytes(range(16))}, expect_v={0: (0x0706050403020100, 0x0f0e0d0c0b0a0908)},
         why='LD1 loads one 16-byte structure'),
    Case('simd_st1_16b', 'st1 {v0.16b}, [x1]\nret', regs={1: DATA},
         vregs={0: (0x0706050403020100, 0x0f0e0d0c0b0a0908)}, expect_mem={0: bytes(range(16))},
         why='ST1 stores one 16-byte structure'),
    Case('simd_ins_s', 'ins v0.s[1], w1\nret', regs={1: 0xdeadbeef},
         vregs={0: (0x1111111122222222, 0x3333333344444444)}, expect_v={0: (0xdeadbeef22222222, 0x3333333344444444)},
         why='INS writes one lane and keeps the rest'),
    Case('simd_umov_s', 'umov w0, v1.s[2]\nret', vregs={1: (0x1111111122222222, 0x3333333344444444)},
         expect_regs={0: 0x44444444}, why='UMOV reads lane 2, which is the low half of the vector high half'),
    Case('simd_mov_16b', 'mov v0.16b, v1.16b\nret', vregs={1: (0x0706050403020100, 0x0f0e0d0c0b0a0908)},
         expect_v={0: (0x0706050403020100, 0x0f0e0d0c0b0a0908)}, why='MOV (vector) copies all 16 bytes'),

    # ---- floating point conversions and rounding
    Case('fcvt_d_to_s', 'fcvt s0, d1\nret', vregs={1: 0x400c000000000000}, expect_v={0: 0x40600000},
         why='FCVT 3.5 double -> single'),
    Case('fcvt_s_to_d', 'fcvt d0, s1\nret', vregs={1: 0x40600000}, expect_v={0: 0x400c000000000000},
         why='FCVT 3.5 single -> double'),
    Case('frintz', 'frintz d0, d1\nret', vregs={1: 0x400599999999999a}, expect_v={0: 0x4000000000000000},
         why='FRINTZ 2.7 towards zero is 2.0'),
    Case('frintp', 'frintp d0, d1\nret', vregs={1: 0x3ff8000000000000}, expect_v={0: 0x4000000000000000},
         why='FRINTP 1.5 upwards is 2.0'),
    Case('frintm_negative', 'frintm d0, d1\nret', vregs={1: 0xbff8000000000000}, expect_v={0: 0xc000000000000000},
         why='FRINTM -1.5 downwards is -2.0'),
    Case('scvtf_w', 'scvtf s0, w1\nret', regs={1: 3}, expect_v={0: 0x40400000}, why='SCVTF 3 -> 3.0f'),
    Case('fcvtzs_s', 'fcvtzs w0, s1\nret', vregs={1: 0x40600000}, expect_regs={0: 3},
         why='FCVTZS 3.5 towards zero is 3'),
    Case('fcvtzu_s', 'fcvtzu w0, s1\nret', vregs={1: 0xc0600000}, expect_regs={0: 0},
         why='FCVTZU of a negative single saturates to 0'),
    Case('fmax_propagates_nan', 'fmax d0, d1, d2\nret', vregs={1: 0x7ff8000000000000, 2: 0x3ff0000000000000},
         expect_v={0: 0x7ff8000000000000},
         why='FMAX propagates a NaN (it is FMAXNM that returns the number)'),
    Case('fmaxnm_returns_number', 'fmaxnm d0, d1, d2\nret', vregs={1: 0x7ff8000000000000, 2: 0x3ff0000000000000},
         expect_v={0: 0x3ff0000000000000}, why='FMAXNM with one NaN returns the other operand'),

    # ---- more integer forms
    Case('sxtw', 'sxtw x0, w1\nret', regs={1: 0x80000000}, expect_regs={0: 0xffffffff80000000},
         why='SXTW sign-extends bit 31 to 64 bits'),
    Case('sxtb_w', 'sxtb w0, w1\nret', regs={1: 0x80}, expect_regs={0: 0xffffff80},
         why='the 32-bit SXTB sign-extends to 32 bits and zeroes the top half'),
    Case('neg', 'neg x0, x1\nret', regs={1: 5}, expect_regs={0: 0xfffffffffffffffb}, why='NEG is SUB from XZR'),
    Case('mvn', 'mvn x0, x1\nret', regs={1: 0}, expect_regs={0: 0xffffffffffffffff}, why='MVN is ORN with XZR'),
    Case('tst_zero', 'tst x1, x2\nret', regs={1: 0b1100, 2: 0b0011}, expect_nzcv=0b0100,
         why='TST sets Z when the AND is zero'),
    Case('rev64', 'rev x0, x1\nret', regs={1: 0x1122334455667788}, expect_regs={0: 0x8877665544332211},
         why='REV reverses all eight bytes'),
    Case('rev16_64', 'rev16 x0, x1\nret', regs={1: 0x1122334455667788}, expect_regs={0: 0x2211443366558877},
         why='REV16 reverses the bytes in each 16-bit half'),
    Case('ubfiz', 'ubfiz x0, x1, #4, #8\nret', regs={1: 0xab}, expect_regs={0: 0xab0},
         why='UBFIZ inserts the low 8 bits at bit 4'),
    Case('sbfiz', 'sbfiz x0, x1, #4, #8\nret', regs={1: 0x80}, expect_regs={0: 0xfffffffffffff800},
         why='SBFIZ sign-extends 8 bits and shifts them to bit 4'),
    Case('mul_32', 'mul w0, w1, w2\nret', regs={1: 0x10000, 2: 0x10000}, expect_regs={0: 0},
         why='the 32-bit MUL wraps at 32 bits and zeroes the top half'),
    Case('smull', 'smull x0, w1, w2\nret', regs={1: _w(-2), 2: 3}, expect_regs={0: _w(-6)},
         why='SMULL sign-extends both 32-bit operands and multiplies into 64 bits'),
    Case('umull', 'umull x0, w1, w2\nret', regs={1: 0xffffffff, 2: 2}, expect_regs={0: 0x1fffffffe},
         why='UMULL multiplies two 32-bit values into 64 bits'),
    Case('ldrsh', 'ldrsh x0, [x1]\nret', regs={1: DATA}, mem={0: b'\x00\x80'}, expect_regs={0: 0xffffffffffff8000},
         why='LDRSH sign-extends a halfword to 64 bits'),

    # ---- system and no-ops
    Case('prfm_is_nothing', 'prfm pldl1keep, [x0]\nret', regs={0: DATA}, why='PRFM changes no register and no memory'),
    Case('dmb_is_nothing', 'dmb ish\nret', why='DMB has no visible effect on one thread'),
    Case('isb_is_nothing', 'isb\nret', why='ISB has no visible effect'),
    Case('mrs_ctr_el0', 'mrs x0, ctr_el0\nret', expect_regs={0: 0x8444c004},
         why='the console Cortex-A57 CTR_EL0 reset value'),
    Case('mrs_dczid_el0', 'mrs x0, dczid_el0\nret', expect_regs={0: 0x10},
         why='DC ZVA is reported as prohibited'),
    Case('tpidr_roundtrip', 'msr tpidr_el0, x0\nmrs x1, tpidr_el0\nret', regs={0: 0x1234},
         expect_regs={1: 0x1234}, why='TPIDR_EL0 keeps what was written'),

    # ---- tail branches
    Case('ret_x1_branches', 'mov x1, #0x1234\nret x1\n', expect_regs={1: 0x1234}, expect_ev=3, expect_evarg=0x1234,
         why='RET Xn branches to Xn: with n != 30 it is not a return of the lifted function'),
    Case('br_x1_branches', 'mov x1, #0x1234\nbr x1\n', expect_regs={1: 0x1234}, expect_ev=3, expect_evarg=0x1234,
         why='BR Xn branches to Xn'),
    Case('b_to_callee', 'b {callee}', calls={'callee': 'callee_set_x0'}, expect_regs={0: 7},
         why='B to another function is a tail call'),
    Case('ccmp_imm_operand', 'cmp x1, #0\nccmp x1, #5, #4, eq\nret', regs={1: 0}, expect_nzcv=0b1000,
         why='the condition (eq) holds, so CCMP compares 0 with 5: N=1, C=0 (borrow)'),
]
