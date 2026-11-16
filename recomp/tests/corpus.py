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
from harness import DATA, EV_FAULT


class Case:
    def __init__(self, name, asm, regs=None, vregs=None, nzcv=0, mem=None, expect_regs=None, expect_v=None,
                 expect_nzcv=None, expect_mem=None, expect_any=(), expect_ev=0, expect_evarg=None, calls=None,
                 allow_bad=False, pad_index=None, rp=0, sites=None, expect_hook=None, why=''):
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
        self.pad_index = pad_index   # instruction index that is a landing pad: the function becomes two-entry
        self.rp = rp                 # the resume address it is entered with ('pad': its own pad address)
        self.sites = sites or {}     # {'entryhook'|'midhook'|'inline': [instruction index]}
        self.expect_hook = expect_hook   # None, 'entry' (the case's entry) or ('entry', byte offset)
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

# ----------------------------------------------------------------------------------------------------------------
# Third batch: 32-bit and SIMD register pairs, 128-bit exclusives, the vector permute/shift/saturate forms, and the
# conversions that round.
# ----------------------------------------------------------------------------------------------------------------
CASES += [
    Case('stp_w', 'stp w1, w2, [x0]\nret', regs={0: DATA, 1: 0x11223344, 2: 0x55667788},
         expect_mem={0: b'\x44\x33\x22\x11\x88\x77\x66\x55'}, why='STP of two W registers writes eight bytes'),
    Case('ldpsw', 'ldpsw x3, x4, [x0]\nret', regs={0: DATA}, mem={0: b'\xff\xff\xff\xff\x01\x00\x00\x00'},
         expect_regs={3: 0xffffffffffffffff, 4: 1}, why='LDPSW sign-extends both words to 64 bits'),
    Case('ldxp128', 'ldxp x0, x1, [x2]\nret', regs={2: DATA},
         mem={0: _q(0x1111111111111111, True) + _q(0x2222222222222222, True)},
         expect_regs={0: 0x1111111111111111, 1: 0x2222222222222222}, why='LDXP of two X registers is one 128-bit load'),
    Case('stxp128', 'ldxp x4, x5, [x2]\nstxp w3, x0, x1, [x2]\nret', regs={0: 0xaaaa, 1: 0xbbbb, 2: DATA},
         mem={0: _q(0x1111111111111111, True) + _q(0x2222222222222222, True)},
         expect_regs={3: 0, 4: 0x1111111111111111, 5: 0x2222222222222222},
         expect_mem={0: _q(0xaaaa, True) + _q(0xbbbb, True)}, why='STXP stores both halves after a matching LDXP'),
    Case('ldarb', 'ldarb w0, [x1]\nret', regs={1: DATA}, mem={0: b'\x80'}, expect_regs={0: 0x80},
         why='LDARB loads one byte with acquire ordering'),
    Case('stlrb', 'stlrb w0, [x1]\nret', regs={0: 0x1ff, 1: DATA}, expect_mem={0: b'\xff'},
         why='STLRB stores one byte with release ordering'),
    Case('fmov_to_gpr', 'fmov x0, d1\nret', vregs={1: 0x1122334455667788}, expect_regs={0: 0x1122334455667788},
         why='FMOV general <- vector moves the bits'),
    Case('fmov_from_gpr', 'fmov d0, x1\nret', regs={1: 0x1122334455667788}, expect_v={0: 0x1122334455667788},
         why='FMOV vector <- general moves the bits and zeroes the top half'),
    Case('frinta_ties_away', 'frinta d0, d1\nret', vregs={1: 0x3ff8000000000000}, expect_v={0: 0x4000000000000000},
         why='FRINTA rounds 1.5 away from zero to 2.0'),
    Case('frintn_ties_even', 'frintn d0, d1\nret', vregs={1: 0x4004000000000000}, expect_v={0: 0x4000000000000000},
         why='FRINTN rounds 2.5 to even, which is 2.0'),
    Case('fcvtms_floor', 'fcvtms x0, d1\nret', vregs={1: 0x400599999999999a}, expect_regs={0: 2},
         why='FCVTMS rounds 2.7 down to 2'),
    Case('fcvtps_ceil', 'fcvtps x0, d1\nret', vregs={1: 0xc00599999999999a}, expect_regs={0: _w(-2)},
         why='FCVTPS rounds -2.7 up to -2'),
    Case('ngc', 'ngc x0, x1\nret', regs={1: 5}, expect_regs={0: _w(-6)},
         why='NGC is SBC from XZR: 0 + NOT(5) + 0'),
    Case('ext_16b', 'ext v0.16b, v1.16b, v2.16b, #8\nret',
         vregs={1: (0x0706050403020100, 0x0f0e0d0c0b0a0908), 2: (0x1716151413121110, 0x1f1e1d1c1b1a1918)},
         expect_v={0: (0x0f0e0d0c0b0a0908, 0x1716151413121110)}, why='EXT takes bytes 8..23 of v1:v2'),
    Case('tbl_reverse', 'tbl v0.16b, {v1.16b}, v2.16b\nret',
         vregs={1: (0x0706050403020100, 0x0f0e0d0c0b0a0908), 2: (0x08090a0b0c0d0e0f, 0x0001020304050607)},
         expect_v={0: (0x08090a0b0c0d0e0f, 0x0001020304050607)}, why='TBL with descending indices reverses the table'),
    Case('zip1_4s', 'zip1 v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x000000140000000a, 0x000000280000001e)},
         expect_v={0: (0x0000000a00000001, 0x0000001400000002)}, why='ZIP1 interleaves the low halves'),
    Case('cmeq_4s', 'cmeq v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x0000000900000001, 0x0000000900000003)},
         expect_v={0: (0x00000000ffffffff, 0x00000000ffffffff)}, why='CMEQ gives all-ones in the lanes that are equal'),
    Case('addv_4s', 'addv s0, v1.4s\nret', vregs={1: (0x0000000200000001, 0x0000000400000003)},
         expect_v={0: (0x0000000a, 0)}, why='ADDV sums the four lanes into lane 0'),
    Case('shl_imm', 'shl v0.4s, v1.4s, #3\nret', vregs={1: (0x0000000200000001, 0x0000000400000003)},
         expect_v={0: (0x0000001000000008, 0x0000002000000018)}, why='SHL by immediate shifts every lane'),
    Case('ushr_imm', 'ushr v0.4s, v1.4s, #2\nret', vregs={1: (0x0000001000000008, 0x0000002000000018)},
         expect_v={0: (0x0000000400000002, 0x0000000800000006)}, why='USHR by immediate shifts every lane'),
    Case('xtn_4h', 'xtn v0.4h, v1.4s\nret', vregs={1: (0x0002345600012345, 0)},
         expect_v={0: (0x0000000034562345, 0)}, why='XTN keeps the low half of each lane'),
    Case('ld1r', 'ld1r {v0.4s}, [x1]\nret', regs={1: DATA}, mem={0: b'\x07\x00\x00\x00'},
         expect_v={0: (0x0000000700000007, 0x0000000700000007)}, why='LD1R replicates one element'),
    Case('sqadd_saturates', 'sqadd v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x000000017fffffff, 0x0000000100000001), 2: (0x0000000100000001, 0x0000000100000001)},
         expect_v={0: (0x000000027fffffff, 0x0000000200000002)},
         why='SQADD saturates the overflowing lane at INT32_MAX'),
    Case('ld2_4s', 'ld2 {v0.4s, v1.4s}, [x2]\nret', regs={2: DATA},
         mem={0: b''.join(w.to_bytes(4, 'little') for w in (1, 10, 2, 20, 3, 30, 4, 40))},
         expect_v={0: (0x0000000200000001, 0x0000000400000003), 1: (0x000000140000000a, 0x000000280000001e)},
         why='LD2 de-interleaves two structures'),
    Case('fcsel_eq', 'fcsel d0, d1, d2, eq\nret', nzcv=0b0110, vregs={1: 0x4000000000000000, 2: 0x4008000000000000},
         expect_v={0: 0x4000000000000000}, why='FCSEL takes the first value when Z is set'),
    Case('movz_lsl48', 'movz x0, #1, lsl #48\nret', expect_regs={0: 0x0001000000000000},
         why='MOVZ with lsl 48 writes the top half'),
    Case('lsr_63', 'lsr x0, x1, #63\nret', regs={1: 0x8000000000000000}, expect_regs={0: 1},
         why='LSR by 63 keeps the top bit'),
    Case('asr_63', 'asr x0, x1, #63\nret', regs={1: 0x8000000000000000}, expect_regs={0: 0xffffffffffffffff},
         why='ASR by 63 fills with the sign bit'),
]

# ----------------------------------------------------------------------------------------------------------------
# Fourth batch: the by-element and lane-structure forms, the LSE min/max and byte/half atomics, and the long adds.
# ----------------------------------------------------------------------------------------------------------------
CASES += [
    Case('ldsmax', 'ldsmax x0, x1, [x2]\nret', regs={0: 9, 2: DATA}, mem={0: _q(5, True)},
         expect_regs={1: 5}, expect_mem={0: _q(9, True)}, why='LDSMAX stores the signed maximum'),
    Case('ldsmin', 'ldsmin x0, x1, [x2]\nret', regs={0: 3, 2: DATA}, mem={0: _q(5, True)},
         expect_regs={1: 5}, expect_mem={0: _q(3, True)}, why='LDSMIN stores the signed minimum'),
    Case('ldumax_negative', 'ldumax x0, x1, [x2]\nret', regs={0: 3, 2: DATA}, mem={0: _q(_w(-5), True)},
         expect_regs={1: _w(-5)}, expect_mem={0: _q(_w(-5), True)},
         why='LDUMAX compares unsigned, so a small positive loses to -5 as 0xfffffffffffffffb'),
    Case('ldumin', 'ldumin x0, x1, [x2]\nret', regs={0: 3, 2: DATA}, mem={0: _q(5, True)},
         expect_regs={1: 5}, expect_mem={0: _q(3, True)}, why='LDUMIN stores the unsigned minimum'),
    Case('casb', 'casb w0, w1, [x2]\nret', regs={0: 5, 1: 9, 2: DATA}, mem={0: b'\x05'},
         expect_mem={0: b'\x09'}, why='CASB compares and swaps one byte'),
    Case('cash', 'cash w0, w1, [x2]\nret', regs={0: 5, 1: 9, 2: DATA}, mem={0: b'\x05\x00'},
         expect_mem={0: b'\x09\x00'}, why='CASH compares and swaps two bytes'),

    Case('saddl', 'saddl v0.4s, v1.4h, v2.4h\nret',
         vregs={1: 0x0004000300020001, 2: 0x0028001e0014000a},
         expect_v={0: (0x000000160000000b, 0x0000002c00000021)}, why='SADDL widens the halfwords and adds'),
    Case('uaddl', 'uaddl v0.4s, v1.4h, v2.4h\nret',
         vregs={1: 0x0004000300020001, 2: 0x0028001e0014000a},
         expect_v={0: (0x000000160000000b, 0x0000002c00000021)}, why='UADDL is the same when nothing is negative'),
    Case('smlal', 'smlal v0.4s, v1.4h, v2.4h\nret',
         vregs={0: (0x0000000100000001, 0x0000000100000001), 1: 0x0005000400030002,
                2: 0x000a000a000a000a},
         expect_v={0: (0x0000001f00000015, 0x0000003300000029)},
         why='SMLAL accumulates the widened products: 1+2*10, 1+3*10, 1+4*10, 1+5*10'),
    Case('addp_4s', 'addp v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x000000140000000a, 0x000000280000001e)},
         expect_v={0: (0x0000000700000003, 0x000000460000001e)},
         why='ADDP adds adjacent pairs: 1+2, 3+4, 10+20, 30+40'),
    Case('shll', 'shll v0.4s, v1.4h, #16\nret', vregs={1: 0x0004000300020001},
         expect_v={0: (0x0002000000010000, 0x0004000000030000)}, why='SHLL widens each halfword and shifts it left'),
    Case('mul_by_element', 'mul v0.4s, v1.4s, v2.s[1]\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x0000000a00000005, 0)},
         expect_v={0: (0x000000140000000a, 0x000000280000001e)}, why='MUL by an element multiplies every lane'),
    Case('fmla_by_element', 'fmla v0.4s, v1.4s, v2.s[0]\nret',
         vregs={0: (0x3f8000003f800000, 0x3f8000003f800000), 1: (0x4000000040000000, 0x4000000040000000),
                2: (0x0000000040400000, 0)},
         expect_v={0: (0x40e0000040e00000, 0x40e0000040e00000)}, why='FMLA by an element: 1 + 2*3 in every lane'),
    Case('ld2_lane', 'ld2 {v0.s, v1.s}[1], [x2]\nret', regs={2: DATA},
         mem={0: b'\x07\x00\x00\x00\x08\x00\x00\x00'},
         vregs={0: (0x0000000100000001, 0x0000000100000001), 1: (0x0000000200000002, 0x0000000200000002)},
         expect_v={0: (0x0000000700000001, 0x0000000100000001), 1: (0x0000000800000002, 0x0000000200000002)},
         why='LD2 (single lane) loads one element per structure into lane 1'),
    Case('ld1_2regs_post', 'ld1 {v0.16b, v1.16b}, [x2], #32\nret', regs={2: DATA}, mem={0: bytes(range(32))},
         expect_regs={2: DATA + 32},
         expect_v={0: (0x0706050403020100, 0x0f0e0d0c0b0a0908), 1: (0x1716151413121110, 0x1f1e1d1c1b1a1918)},
         why='LD1 of two registers with a post-index writeback'),
    Case('st1_4regs', 'st1 {v0.16b, v1.16b, v2.16b, v3.16b}, [x4]\nret', regs={4: DATA},
         vregs={0: (0x0706050403020100, 0x0f0e0d0c0b0a0908), 1: (0x1716151413121110, 0x1f1e1d1c1b1a1918),
                2: (0x2726252423222120, 0x2f2e2d2c2b2a2928), 3: (0x3736353433323130, 0x3f3e3d3c3b3a3938)},
         expect_mem={0: bytes(range(64))}, why='ST1 stores four 16-byte structures'),
    Case('tbl_two_tables', 'tbl v0.16b, {v1.16b, v2.16b}, v3.16b\nret',
         vregs={1: (0x0706050403020100, 0x0f0e0d0c0b0a0908), 2: (0x1716151413121110, 0x1f1e1d1c1b1a1918),
                3: (0x1716151413121110, 0x1f1e1d1c1b1a1918)},
         expect_v={0: (0x1716151413121110, 0x1f1e1d1c1b1a1918)},
         why='TBL over two tables: indices 16..31 reach the second register'),
]

# ----------------------------------------------------------------------------------------------------------------
# Fifth batch: the 32-bit exclusive pair, the permute family, vector shifts by immediate and the saturating narrowing.
# ----------------------------------------------------------------------------------------------------------------
CASES += [
    Case('ldxp32', 'ldxp w0, w1, [x2]\nret', regs={2: DATA}, mem={0: b'\x01\x00\x00\x00\x02\x00\x00\x00'},
         expect_regs={0: 1, 1: 2}, why='LDXP of two W registers is one 64-bit access'),
    Case('ldaxp64', 'ldaxp x0, x1, [x2]\nret', regs={2: DATA},
         mem={0: _q(0x1122334455667788, True) + _q(0x99aabbccddeeff00, True)},
         expect_regs={0: 0x1122334455667788, 1: 0x99aabbccddeeff00},
         why='LDAXP is the acquire form of the 128-bit exclusive pair (op1 set, op2 clear)'),
    Case('ldaxr_stlxr', 'ldaxr x2, [x1]\nadd x2, x2, #1\nstlxr w3, x2, [x1]\nret', regs={1: DATA},
         mem={0: _q(0x40, True)}, expect_regs={2: 0x41, 3: 0}, expect_mem={0: _q(0x41, True)},
         why='LDAXR / STLXR: the release store completes against the acquire load'),
    Case('stlxr_without_ldaxr', 'mov x2, #7\nstlxr w3, x2, [x1]\nret', regs={1: DATA}, expect_regs={2: 7, 3: 1},
         why='STLXR with no preceding load fails like STXR'),
    Case('casal', 'casal x0, x1, [x2]\nret', regs={0: 5, 1: 9, 2: DATA}, mem={0: _q(5, True)},
         expect_regs={0: 5}, expect_mem={0: _q(9, True)}, why='CASAL is CAS with acquire-release ordering'),
    Case('stlxp', 'ldxp x4, x5, [x2]\nstlxp w3, x0, x1, [x2]\nret', regs={0: 0xaaaa, 1: 0xbbbb, 2: DATA},
         mem={0: _q(0x11, True) + _q(0x22, True)},
         expect_regs={3: 0, 4: 0x11, 5: 0x22}, expect_mem={0: _q(0xaaaa, True) + _q(0xbbbb, True)},
         why='STLXP stores both halves after a matching LDXP'),
    Case('zip2_4s', 'zip2 v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x000000140000000a, 0x000000280000001e)},
         expect_v={0: (0x0000001e00000003, 0x0000002800000004)}, why='ZIP2 interleaves the high halves'),
    Case('uzp1_4s', 'uzp1 v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x000000140000000a, 0x000000280000001e)},
         expect_v={0: (0x0000000300000001, 0x0000001e0000000a)}, why='UZP1 takes the even lanes'),
    Case('trn1_4s', 'trn1 v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x000000140000000a, 0x000000280000001e)},
         expect_v={0: (0x0000000a00000001, 0x0000001e00000003)}, why='TRN1 interleaves the even lanes of both'),
    Case('rev64_vec', 'rev64 v0.4s, v1.4s\nret', vregs={1: (0x0000000200000001, 0x0000000400000003)},
         expect_v={0: (0x0000000100000002, 0x0000000300000004)},
         why='REV64 reverses the 32-bit lanes inside each 64-bit half'),
    Case('cnt_8b', 'cnt v0.8b, v1.8b\nret', vregs={1: (0x5580ff0f07030100, 0)},
         expect_v={0: (0x0401080403020100, 0)}, why='CNT counts the set bits of every byte'),
    Case('sqxtn_saturates', 'sqxtn v0.4h, v1.4s\nret', vregs={1: (0xffff000000010000, 0xfffffffb00000005)},
         expect_v={0: (0xfffb000580007fff, 0)},
         why='SQXTN saturates 0x10000 and -0x10000 and packs four halfwords into the low half'),
    Case('shrn', 'shrn v0.4h, v1.4s, #4\nret', vregs={1: (0x0002345000012340, 0)},
         expect_v={0: (0x23451234, 0)}, why='SHRN shifts each lane right and keeps the low half'),
    Case('sshr_imm', 'sshr v0.4s, v1.4s, #4\nret', vregs={1: (0xfffffffffffffff0, 0x0000000000000010)},
         expect_v={0: (0xffffffffffffffff, 0x0000000000000001)}, why='SSHR shifts arithmetically by an immediate'),
    Case('neg_vec', 'neg v0.4s, v1.4s\nret', vregs={1: (0x0000000200000001, 0x0000000400000003)},
         expect_v={0: (0xfffffffeffffffff, 0xfffffffcfffffffd)}, why='NEG negates every lane'),
    Case('cmeq_zero', 'cmeq v0.4s, v1.4s, #0\nret', vregs={1: (0x0000000100000000, 0x0000000200000000)},
         expect_v={0: (0x00000000ffffffff, 0x00000000ffffffff)}, why='CMEQ against zero gives all-ones where a lane is 0'),
]

# ----------------------------------------------------------------------------------------------------------------
# Sixth batch: the rest of the permute family, the narrowing add, signed comparisons, 4-structure loads and the
# fixed-point conversion.
# ----------------------------------------------------------------------------------------------------------------
CASES += [
    Case('uzp2_4s', 'uzp2 v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x000000140000000a, 0x000000280000001e)},
         expect_v={0: (0x0000000400000002, 0x0000002800000014)}, why='UZP2 takes the odd lanes'),
    Case('trn2_4s', 'trn2 v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x000000140000000a, 0x000000280000001e)},
         expect_v={0: (0x0000001400000002, 0x0000002800000004)}, why='TRN2 interleaves the odd lanes of both'),
    Case('cmlt_zero', 'cmlt v0.4s, v1.4s, #0\nret', vregs={1: (0x00000001ffffffff, 0x00000005fffffffb)},
         expect_v={0: (0x00000000ffffffff, 0x00000000ffffffff)},
         why='CMLT against zero gives all-ones in lanes 0 and 2, which are negative'),
    Case('addhn', 'addhn v0.4h, v1.4s, v2.4s\nret', vregs={1: (0x0000000012345678, 0), 2: (0x0000000011110000, 0)},
         expect_v={0: (0x00002345, 0)}, why='ADDHN adds the wide lanes and keeps the high half'),
    Case('ld4_4s', 'ld4 {v0.4s, v1.4s, v2.4s, v3.4s}, [x4]\nret', regs={4: DATA},
         mem={0: b''.join(w.to_bytes(4, 'little') for w in (1, 11, 21, 31, 2, 12, 22, 32,
                                                           3, 13, 23, 33, 4, 14, 24, 34))},
         expect_v={0: (0x0000000200000001, 0x0000000400000003), 1: (0x0000000c0000000b, 0x0000000e0000000d),
                   2: (0x0000001600000015, 0x0000001800000017), 3: (0x000000200000001f, 0x0000002200000021)},
         why='LD4 de-interleaves four structures'),
    Case('ld1_3regs', 'ld1 {v0.16b, v1.16b, v2.16b}, [x3]\nret', regs={3: DATA}, mem={0: bytes(range(48))},
         expect_v={0: (0x0706050403020100, 0x0f0e0d0c0b0a0908), 1: (0x1716151413121110, 0x1f1e1d1c1b1a1918),
                   2: (0x2726252423222120, 0x2f2e2d2c2b2a2928)},
         why='LD1 loads three consecutive 16-byte structures'),
    Case('st2_lane', 'st2 {v0.s, v1.s}[0], [x2]\nret', regs={2: DATA},
         vregs={0: (0x0000000700000001, 0), 1: (0x0000000800000002, 0)},
         expect_mem={0: b'\x01\x00\x00\x00\x02\x00\x00\x00'},
         why='ST2 (single lane) writes one element per structure from lane 0'),
    Case('scvtf_fbits', 'scvtf d0, x1, #4\nret', regs={1: 3}, expect_v={0: 0x3fc8000000000000},
         why='SCVTF with 4 fractional bits converts 3 to 3 * 2^-4 = 0.1875'),
    Case('faddp_scalar', 'faddp d0, v1.2d\nret', vregs={1: (0x3ff0000000000000, 0x4000000000000000)},
         expect_v={0: 0x4008000000000000}, why='scalar FADDP adds the two doubles of the source'),
]

# ----------------------------------------------------------------------------------------------------------------
# Seventh batch: the accumulating shifts, the saturating narrowing pair, the across-lane maximum and FP narrowing.
# ----------------------------------------------------------------------------------------------------------------
CASES += [
    Case('usra_imm', 'usra v0.4s, v1.4s, #4\nret', vregs={0: (0x0000001000000010, 0), 1: (0x0000020000000200, 0)},
         expect_v={0: (0x0000003000000030, 0)}, why='USRA adds 0x200 >> 4 into 0x10: 0x30 in every lane'),
    Case('sri_imm', 'sri v0.4s, v1.4s, #4\nret', vregs={0: (0, 0), 1: (0x1234567812345678, 0x1234567812345678)},
         expect_v={0: (0x0123456701234567, 0x0123456701234567)},
         why='SRI shifts right and keeps the destination low bits (here zero)'),
    Case('uqxtn_saturates', 'uqxtn v0.4h, v1.4s\nret', vregs={1: (0x0000000500010000, 0x00000000ffffffff)},
         expect_v={0: (0x0000ffff0005ffff, 0)},
         why='UQXTN saturates 0x10000 and 0xffffffff at 0xffff, and packs four halfwords into the low half'),
    Case('sqxtun_saturates', 'sqxtun v0.4h, v1.4s\nret', vregs={1: (0x00000005ffffffff, 0x0000000000010000)},
         expect_v={0: (0x0000ffff00050000, 0)},
         why='SQXTUN clamps a negative lane to 0 and a too-large one to 0xffff'),
    Case('smaxv', 'smaxv s0, v1.4s\nret', vregs={1: (0x0000000100000003, 0x0000000200000004)},
         expect_v={0: (0x00000004, 0)}, why='SMAXV takes the largest lane into lane 0'),
    Case('fabd', 'fabd d0, d1, d2\nret', vregs={1: 0x3ff0000000000000, 2: 0x4008000000000000},
         expect_v={0: 0x4000000000000000}, why='FABD is |1.0 - 3.0| = 2.0'),
    Case('fmls', 'fmls v0.4s, v1.4s, v2.4s\nret',
         vregs={0: (0x4120000041200000, 0x4120000041200000), 1: (0x4000000040000000, 0x4000000040000000),
                2: (0x4040000040400000, 0x4040000040400000)},
         expect_v={0: (0x4080000040800000, 0x4080000040800000)}, why='FMLS: 10.0 - 2.0*3.0 = 4.0 in every lane'),
    Case('bic_vec', 'bic v0.16b, v1.16b, v2.16b\nret',
         vregs={1: (0xffffffffffffffff, 0xffffffffffffffff), 2: (0x0f0f0f0f0f0f0f0f, 0x0f0f0f0f0f0f0f0f)},
         expect_v={0: (0xf0f0f0f0f0f0f0f0, 0xf0f0f0f0f0f0f0f0)}, why='BIC clears the bits of the second operand'),
]

# ----------------------------------------------------------------------------------------------------------------
# Eighth batch: the second-half narrowing and widening forms, the halving and rounding adds, and the vector compares.
# ----------------------------------------------------------------------------------------------------------------
CASES += [
    Case('xtn2_upper_half', 'xtn2 v0.8h, v1.4s\nret', vregs={0: (0xaaaa, 0), 1: (0x0002222200011111, 0x0004444400033333)},
         expect_v={0: (0xaaaa, 0x4444333322221111)},
         why='XTN2 narrows into the UPPER half of the destination and keeps the lower half'),
    Case('raddhn_rounds', 'raddhn v0.4h, v1.4s, v2.4s\nret', vregs={1: (0x12345678, 0), 2: (0x11110000, 0)},
         expect_v={0: (0x2345, 0)},
         why='RADDHN rounds before taking the high half: (0x12345678 + 0x11110000 + 0x8000) >> 16'),
    Case('shadd', 'shadd v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000600000004, 0x0000000a00000008), 2: (0x0000000200000002, 0x0000000200000002)},
         expect_v={0: (0x0000000400000003, 0x0000000600000005)}, why='SHADD adds and halves'),
    Case('uhadd', 'uhadd v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000600000004, 0x0000000a00000008), 2: (0x0000000200000002, 0x0000000200000002)},
         expect_v={0: (0x0000000400000003, 0x0000000600000005)}, why='UHADD is the same when nothing is negative'),
    Case('rshrn_rounds', 'rshrn v0.4h, v1.4s, #4\nret', vregs={1: (0x0002345500012345, 0)},
         expect_v={0: (0x23451234, 0)}, why='RSHRN rounds before shifting and narrowing'),
    Case('cmgt_zero', 'cmgt v0.4s, v1.4s, #0\nret', vregs={1: (0x00000001ffffffff, 0x00000005ffffffff)},
         expect_v={0: (0xffffffff00000000, 0xffffffff00000000)},
         why='CMGT against zero is all-ones in lanes 1 and 3, which are the positive ones'),
    Case('fcmeq_vec', 'fcmeq v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x400000003f800000, 0x4080000040400000), 2: (0x411000003f800000, 0x4110000040400000)},
         expect_v={0: (0x00000000ffffffff, 0x00000000ffffffff)},
         why='FCMEQ gives all-ones in the lanes whose floats are equal'),
    Case('addv_8b', 'addv b0, v1.8b\nret', vregs={1: (0x0807060504030201, 0)},
         expect_v={0: (0x24, 0)}, why='ADDV sums the eight bytes: 1+2+...+8 = 36'),
    Case('zip1_2d', 'zip1 v0.2d, v1.2d, v2.2d\nret', vregs={1: (0xaaaa, 0xbbbb), 2: (0xcccc, 0xdddd)},
         expect_v={0: (0xaaaa, 0xcccc)}, why='ZIP1 of 64-bit lanes takes the low lane of each source'),
]

# ----------------------------------------------------------------------------------------------------------------
# Landing pads: resuming a function at a pad starts from the context, not from the top (the game has 8,934 pads).
# ----------------------------------------------------------------------------------------------------------------
CASES += [
    Case('pad_resume_skips_prefix', 'mov x0, #1\nmov x1, #2\nadd x0, x0, x1\nret', regs={0: 10, 1: 20},
         pad_index=2, rp='pad', expect_regs={0: 30},
         why='resuming at the pad runs only the add, on the registers the context holds, not the two movs'),
    Case('pad_resume_first', 'mov x0, #1\nadd x0, x0, x1\nret', regs={0: 10, 1: 5},
         pad_index=0, rp='pad', expect_regs={0: 6},
         why='resuming at the function entry runs it from the top: mov sets 1, then 1 + 5 = 6'),
    Case('pad_normal_entry_unaffected', 'mov x0, #1\nadd x0, x0, x1\nret', regs={0: 10, 1: 5},
         pad_index=1, rp=0, expect_regs={0: 6},
         why='entered normally (rp 0) a two-entry function still runs from the top: 1 + 5 = 6'),
]

CASES += [
    Case('unmapped_load_faults', 'ldr x0, [x1]\nret', regs={1: 0}, expect_ev=EV_FAULT,
         why='a load from an unmapped address comes back as the case\'s fault event, not as a dead run'),
]

# ----------------------------------------------------------------------------------------------------------------
# Hook sites: the statements the game layer's mods are reached through.
# ----------------------------------------------------------------------------------------------------------------
CASES += [
    Case('entryhook_called', 'mov x0, #1\nret', sites={'entryhook': [0]}, expect_hook='entry',
         expect_regs={0: 1}, why='an entry hook is called with the function address before the body runs'),
    Case('inline_site_called', 'mov x0, #1\nmov x1, #2\nret', sites={'inline': [1]}, expect_hook=('entry', 4),
         expect_regs={0: 1, 1: 2}, why='an inline site is called with its own address before its instruction'),
    Case('midhook_called', 'mov x0, #1\nmov x1, #2\nret', sites={'midhook': [1]}, expect_hook=('entry', 4),
         expect_regs={0: 1, 1: 2}, why='a mid-function hook is called with its own address before its instruction'),
]

CASES += [
    Case('retaa_returns', 'mov x0, #1\n.inst 0xd65f0bff', expect_regs={0: 1},
         why='RETAA returns through LR; its Rn field reads 31, which is not a register'),
    Case('retab_returns', 'mov x0, #1\n.inst 0xd65f0fff', expect_regs={0: 1},
         why='RETAB returns through LR; its Rn field reads 31, which is not a register'),
]

# Pointer authentication is not modelled (x_pac.py): PAC* sign nothing, AUT* never fail, XPAC* strips nothing.
CASES += [
    Case('pacia_identity', 'pacia x0, x1\nret', regs={0: 0x1234, 1: 0x5678}, expect_regs={0: 0x1234},
         why='PACIA signs nothing: the pointer is unchanged'),
    Case('autia_identity', 'autia x0, x1\nret', regs={0: 0x1234, 1: 0x5678}, expect_regs={0: 0x1234},
         why='AUTIA never fails: the pointer is unchanged'),
    Case('xpaci_identity', 'xpaci x0\nret', regs={0: 0xdeadbeef00001234}, expect_regs={0: 0xdeadbeef00001234},
         why='XPACI has no code to strip'),
    Case('pacga_zero', 'pacga x0, x1, x2\nret', regs={0: 0x1234, 1: 0x5678, 2: 0x9abc}, expect_regs={0: 0},
         why='PACGA yields 0: the generic code is never checked'),
]


# ----------------------------------------------------------------------------------------------------------------
# Ninth batch: the bit-select family, the conditional float compare, the CRC32 instructions and the long conversion.
# The CRC expectations come from the reflected table (0xEDB88320) applied to the accumulator, which is what the ARM
# CRC32B/H/W/X pseudocode does — no pre- or post-inversion.
# ----------------------------------------------------------------------------------------------------------------
CASES += [
    Case('bsl', 'bsl v0.16b, v1.16b, v2.16b\nret',
         vregs={0: (0xAAAAAAAAAAAAAAAA, 0xAAAAAAAAAAAAAAAA), 1: (0x5555555555555555, 0x5555555555555555),
                2: (0xFFFF0000FFFF0000, 0xFFFF0000FFFF0000)},
         expect_v={0: (0x5555AAAA5555AAAA, 0x5555AAAA5555AAAA)},
         why='BSL takes Vn where Vm is set and keeps Vd where it is clear'),
    Case('bit', 'bit v0.16b, v1.16b, v2.16b\nret',
         vregs={0: (0xAAAAAAAAAAAAAAAA, 0xAAAAAAAAAAAAAAAA), 1: (0x5555555555555555, 0x5555555555555555),
                2: (0xFFFF0000FFFF0000, 0xFFFF0000FFFF0000)},
         expect_v={0: (0x5555AAAA5555AAAA, 0x5555AAAA5555AAAA)},
         why='BIT inserts Vn where Vm is set and keeps Vd where it is clear'),
    Case('bif', 'bif v0.16b, v1.16b, v2.16b\nret',
         vregs={0: (0xAAAAAAAAAAAAAAAA, 0xAAAAAAAAAAAAAAAA), 1: (0x5555555555555555, 0x5555555555555555),
                2: (0xFFFF0000FFFF0000, 0xFFFF0000FFFF0000)},
         expect_v={0: (0xAAAA5555AAAA5555, 0xAAAA5555AAAA5555)},
         why='BIF inserts Vn where Vm is clear and keeps Vd where it is set'),
    Case('fccmp_taken', 'fccmp s0, s1, #0, eq\nret', nzcv=4, vregs={0: 0x3F800000, 1: 0x40000000},
         expect_nzcv=8, why='FCCMP with the condition true compares: s0 < s1 sets N'),
    Case('fccmp_not_taken', 'fccmp s0, s1, #15, eq\nret', nzcv=0, vregs={0: 0x3F800000, 1: 0x40000000},
         expect_nzcv=15, why='FCCMP with the condition false sets NZCV to its immediate'),
    Case('crc32b', 'crc32b w0, w1, w2\nret', regs={1: 0, 2: 1}, expect_regs={0: 0x77073096},
         why='CRC32B of 0x01 against a zero accumulator'),
    Case('crc32h', 'crc32h w0, w1, w2\nret', regs={1: 0, 2: 0x1234}, expect_regs={0: 0x489382BF},
         why='CRC32H of 0x1234 against a zero accumulator'),
    Case('crc32w', 'crc32w w0, w1, w2\nret', regs={1: 0, 2: 0x12345678}, expect_regs={0: 0x8E2958CE},
         why='CRC32W of 0x12345678 against a zero accumulator'),
    Case('crc32x', 'crc32x w0, w1, x2\nret', regs={1: 0, 2: 0x123456789ABCDEF0}, expect_regs={0: 0x7C00D823},
         why='CRC32X of a 64-bit value against a zero accumulator'),
    Case('fcvtl', 'fcvtl v0.4s, v1.4h\nret', vregs={1: (0x4200380040003C00, 0)},
         expect_v={0: (0x400000003F800000, 0x404000003F000000)},
         why='FCVTL widens four halves (1, 2, 0.5, 3) to four singles'),
    Case('cnt', 'cnt v0.8b, v1.8b\nret', vregs={1: (0x0102030405060708, 0)},
         expect_v={0: (0x0101020102020301, 0)},
         why='CNT counts each byte: 08 07 06 05 04 03 02 01 become 1 3 2 2 1 2 1 1'),
    Case('addv', 'addv b0, v1.8b\nret', vregs={1: (0x0102030405060708, 0)},
         expect_v={0: (0x24, 0)}, why='ADDV sums the eight bytes: 1..8 = 36'),
    Case('addp_4s', 'addp v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x0000000200000001, 0x0000000400000003), 2: (0x0000000600000005, 0x0000000800000007)},
         expect_v={0: (0x0000000700000003, 0x0000000F0000000B)},
         why='ADDP adds lane pairs across both sources: 1+2, 3+4, 5+6, 7+8'),
    Case('sqxtn', 'sqxtn v0.8b, v1.8h\nret',
         vregs={1: (0x007FFF8000800001, 0x80007FFFFFFF00FF)},
         expect_v={0: (0x807FFF7F7F807F01, 0)},
         why='SQXTN narrows eight halves to signed bytes, saturating 0x80 to 0x7F and 0x8000 to 0x80'),
    Case('cmgt_signed', 'cmgt v0.8b, v1.8b, v2.8b\nret',
         vregs={1: (0x050403020100FF80, 0), 2: (0x0001020304050607, 0)},
         expect_v={0: (0xFFFFFF0000000000, 0)},
         why='CMGT compares as signed bytes: only 0x03, 0x04 and 0x05 exceed their partners'),
    Case('cmeq', 'cmeq v0.8b, v1.8b, v2.8b\nret',
         vregs={1: (0x0102030405060708, 0), 2: (0x0102030405060708, 0)},
         expect_v={0: (0xFFFFFFFFFFFFFFFF, 0)}, why='CMEQ of equal vectors sets every byte'),
    Case('dup_element', 'dup v0.4s, v1.s[2]\nret',
         vregs={1: (0x0000000400000003, 0x0000000600000005)},
         expect_v={0: (0x0000000500000005, 0x0000000500000005)},
         why='DUP of lane 2 broadcasts it to every lane'),
    Case('ins_gpr', 'ins v0.b[3], w1\nret', regs={1: 0xAB}, vregs={0: (0, 0)},
         expect_v={0: (0x00000000AB000000, 0)}, why='INS writes the general register into byte 3'),
    Case('umov_byte', 'umov w0, v1.b[5]\nret', vregs={1: (0x0102030405060708, 0)}, expect_regs={0: 3},
         why='UMOV reads byte 5 of the vector into a W register'),
    Case('movi_lsl8', 'movi v0.4s, #0x12, lsl #8\nret', expect_v={0: (0x0000120000001200, 0x0000120000001200)},
         why='MOVI shifts its immediate left by 8 into every word'),
    Case('xtn2', 'xtn2 v0.8h, v1.4s\nret', vregs={0: (0x1111111122222222, 0x3333333344444444),
                                                 1: (0x0000000200000001, 0x0000000400000003)},
         expect_v={0: (0x1111111122222222, 0x0004000300020001)},
         why='XTN2 narrows into the upper half and leaves the lower half alone'),
    Case('shl_imm', 'shl v0.4s, v1.4s, #3\nret', vregs={1: (0x0000000200000001, 0x0000000400000003)},
         expect_v={0: (0x0000001000000008, 0x0000002000000018)}, why='SHL by 3 multiplies every word by eight'),
    Case('ushll', 'ushll v0.8h, v1.8b, #1\nret', vregs={1: (0x0102030405060708, 0)},
         expect_v={0: (0x000A000C000E0010, 0x0002000400060008)},
         why='USHLL widens the eight bytes and shifts each left by one (byte 0 is 0x08, so half 0 is 0x10)'),
    Case('sadalp', 'sadalp v0.4s, v1.8h\nret', vregs={1: (0x0004000300020001, 0x0008000700060005)},
         expect_v={0: (0x0000000700000003, 0x0000000F0000000B)},
         why='SADALP accumulates the pairwise sums of the halves into the words'),
    Case('frinta_tie', 'frinta s0, s1\nret', vregs={1: 0xC0200000}, expect_v={0: 0xC0400000},
         why='FRINTA rounds -2.5 away from zero to -3'),
    Case('frintn_tie', 'frintn s0, s1\nret', vregs={1: 0xC0200000}, expect_v={0: 0xC0000000},
         why='FRINTN rounds -2.5 to even, which is -2'),
    Case('frintm_pos', 'frintm s0, s1\nret', vregs={1: 0x40200000}, expect_v={0: 0x40000000},
         why='FRINTM floors 2.5 to 2'),
    Case('frintm_neg', 'frintm s0, s1\nret', vregs={1: 0xC0200000}, expect_v={0: 0xC0400000},
         why='FRINTM floors -2.5 to -3'),
    Case('frintp_pos', 'frintp s0, s1\nret', vregs={1: 0x40200000}, expect_v={0: 0x40400000},
         why='FRINTP ceils 2.5 to 3'),
    Case('frintp_neg', 'frintp s0, s1\nret', vregs={1: 0xC0200000}, expect_v={0: 0xC0000000},
         why='FRINTP ceils -2.5 to -2'),
    Case('frintz_neg', 'frintz s0, s1\nret', vregs={1: 0xC0200000}, expect_v={0: 0xC0000000},
         why='FRINTZ truncates -2.5 to -2'),
    Case('tbx_keeps', 'tbx v0.16b, {v1.16b, v2.16b}, v3.16b\nret',
         vregs={0: (0xAAAAAAAAAAAAAAAA, 0xAAAAAAAAAAAAAAAA),
                1: (0x1716151413121110, 0x1F1E1D1C1B1A1918),
                2: (0x1F1E1D1C1B1A1918, 0x2726252423222120),
                3: (0x0000000021201000, 0)},
         expect_v={0: (0x10101010AAAA1810, 0x1010101010101010)},
         why='TBX takes table bytes for indices 0 and 16 and keeps the destination for 32 and 33'),
    Case('ld2r', 'ld2r {v0.16b, v1.16b}, [x2]\nret', regs={2: DATA}, mem={0: b'\x11\x22'},
         expect_v={0: (0x1111111111111111, 0x1111111111111111), 1: (0x2222222222222222, 0x2222222222222222)},
         why='LD2R loads two bytes and replicates each across its register'),
    Case('fmla_element', 'fmla v0.4s, v1.4s, v2.s[1]\nret',
         vregs={0: (0x41A0000041200000, 0x4220000041F00000),
                1: (0x400000003F800000, 0x4080000040400000),
                2: (0x4000000000000000, 0)},
         expect_v={0: (0x41C0000041400000, 0x4240000042100000)},
         why='FMLA by element adds v1 * v2[1] to the accumulator'),
    Case('uqxtn', 'uqxtn v0.8b, v1.8h\nret', vregs={1: (0x010000FF00800001, 0x000000000000FFFF)},
         expect_v={0: (0x000000FFFFFF8001, 0)},
         why='UQXTN narrows eight halves to unsigned bytes, saturating 0x0100 and 0xFFFF to 0xFF'),
    Case('sqshl_imm', 'sqshl v0.4s, v1.4s, #4\nret',
         vregs={1: (0x0800000010000000, 0x0000000200000001)},
         expect_v={0: (0x7FFFFFFF7FFFFFFF, 0x0000002000000010)},
         why='SQSHL saturates the two lanes that overflow 32 bits'),
    Case('pmul', 'pmul v0.16b, v1.16b, v2.16b\nret',
         vregs={1: (0x0000000000000003, 0), 2: (0x0000000000000005, 0)},
         expect_v={0: (0x000000000000000F, 0)},
         why='PMUL multiplies bytes without carries: (x+1)(x^2+1) = x^3+x^2+x+1'),
    Case('addhn', 'addhn v0.8b, v1.8h, v2.8h\nret',
         vregs={1: (0x0100010001000100, 0x0100010001000100), 2: (0x0100010001000100, 0x0100010001000100)},
         expect_v={0: (0x0202020202020202, 0)}, why='ADDHN adds the halves and keeps the high byte of each'),
    Case('sqrdmulh', 'sqrdmulh v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x4000000040000000, 0x4000000040000000), 2: (0x4000000040000000, 0x4000000040000000)},
         expect_v={0: (0x2000000020000000, 0x2000000020000000)},
         why='SQRDMULH doubles the 64-bit product and keeps the high word'),
    Case('sxtl', 'sxtl v0.8h, v1.8b\nret', vregs={1: (0x00000000017F80FF, 0)},
         expect_v={0: (0x0001007FFF80FFFF, 0)}, why='SXTL sign-extends the bytes: 0xFF becomes 0xFFFF'),
    Case('shrn', 'shrn v0.8b, v1.8h, #4\nret', vregs={1: (0x000100FF001000F0, 0)},
         expect_v={0: (0x00000000000F010F, 0)}, why='SHRN shifts each half right by 4 and narrows to a byte'),
    Case('lsrv', 'lsrv x0, x1, x2\nret', regs={1: 0x8000000000000000, 2: 4}, expect_regs={0: 0x0800000000000000},
         why='LSRV shifts by the register value'),
    Case('lsrv_mod64', 'lsrv x0, x1, x2\nret', regs={1: 0x8000000000000000, 2: 64},
         expect_regs={0: 0x8000000000000000}, why='LSRV takes the amount modulo 64, so 64 shifts by none'),
    Case('lsrv_mod65', 'lsrv x0, x1, x2\nret', regs={1: 0x8000000000000000, 2: 65},
         expect_regs={0: 0x4000000000000000}, why='LSRV of 65 is a shift by one'),
    Case('cls', 'cls x0, x1\nret', regs={1: 1}, expect_regs={0: 62},
         why='CLS counts the bits after the sign bit that match it: 62 for 1'),
    Case('csinv_taken', 'csinv x0, x1, x2, eq\nret', nzcv=4, regs={1: 5, 2: 7}, expect_regs={0: 5},
         why='CSINV with the condition true takes the register'),
    Case('csinv_not_taken', 'csinv x0, x1, x2, eq\nret', nzcv=0, regs={1: 5, 2: 7},
         expect_regs={0: 0xFFFFFFFFFFFFFFF8}, why='CSINV with the condition false inverts the second register'),
    Case('csneg_not_taken', 'csneg x0, x1, x2, eq\nret', nzcv=0, regs={1: 5, 2: 7},
         expect_regs={0: 0xFFFFFFFFFFFFFFF9}, why='CSNEG with the condition false negates the second register'),
    Case('fcvtns_tie', 'fcvtns w0, s1\nret', vregs={1: 0x40600000}, expect_regs={0: 4},
         why='FCVTNS rounds 3.5 to even, which is 4'),
    Case('fcvtas_tie', 'fcvtas w0, s1\nret', vregs={1: 0xC0200000}, expect_regs={0: 0xFFFFFFFD},
         why='FCVTAS rounds -2.5 away from zero to -3'),
    Case('fmulx_zero_inf', 'fmulx s0, s1, s2\nret', vregs={1: 0x00000000, 2: 0x7F800000},
         expect_v={0: 0x40000000}, why='FMULX returns 2.0 for 0 x infinity instead of NaN'),
    Case('sqdmulh', 'sqdmulh v0.4s, v1.4s, v2.4s\nret',
         vregs={1: (0x4000000040000000, 0x4000000040000000), 2: (0x2000000020000000, 0x2000000020000000)},
         expect_v={0: (0x1000000010000000, 0x1000000010000000)},
         why='SQDMULH doubles the product and keeps the high word without rounding'),
    Case('fcvtn', 'fcvtn v0.4h, v1.4s\nret', vregs={1: (0x400000003F800000, 0x404000003F000000)},
         expect_v={0: (0x4200380040003C00, 0)}, why='FCVTN narrows four singles (1, 2, 0.5, 3) to four halves'),
    Case('ldursb', 'ldursb x0, [x1, #-3]\nret', regs={1: DATA + 8}, mem={5: b'\x80'},
         expect_regs={0: 0xFFFFFFFFFFFFFF80}, why='LDURSB loads one byte at a negative unscaled offset and sign-extends'),
    Case('ldursh', 'ldursh x0, [x1, #2]\nret', regs={1: DATA}, mem={2: b'\x00\x80'},
         expect_regs={0: 0xFFFFFFFFFFFF8000}, why='LDURSH sign-extends a half from an unscaled offset'),
    Case('ldursw', 'ldursw x0, [x1, #4]\nret', regs={1: DATA}, mem={4: b'\x00\x00\x00\x80'},
         expect_regs={0: 0xFFFFFFFF80000000}, why='LDURSW sign-extends a word from an unscaled offset'),
    Case('ldrsw', 'ldrsw x0, [x1, #4]\nret', regs={1: DATA}, mem={4: b'\x00\x00\x00\x80'},
         expect_regs={0: 0xFFFFFFFF80000000}, why='LDRSW sign-extends a scaled word load'),
    Case('bfxil', 'bfxil x0, x1, #8, #16\nret', regs={0: 0xFFFFFFFFFFFFFFFF, 1: 0xABCD},
         expect_regs={0: 0xFFFFFFFFFFFF00AB},
         why='BFXIL rotates the source field into the low bits: src[23:8] lands at bits 0..7'),
    Case('sbcs_borrow', 'sbcs x0, x1, x2\nret', nzcv=2, regs={1: 10, 2: 3},
         expect_regs={0: 7}, expect_nzcv=2, why='SBCS with carry set subtracts without the borrow'),
    Case('cinc_taken', 'cinc x0, x1, eq\nret', nzcv=4, regs={1: 5}, expect_regs={0: 6},
         why='CINC with the condition true adds one'),
    Case('ld1_post_reg', 'ld1 {v0.16b}, [x1], x2\nret', regs={1: DATA, 2: 16},
         mem={0: bytes(range(0x10, 0x20))},
         expect_regs={1: DATA + 16}, expect_v={0: (0x1716151413121110, 0x1F1E1D1C1B1A1918)},
         why='LD1 post-indexed by a register loads 16 bytes and advances the base by that register'),
    Case('ld1_post_imm', 'ld1 {v0.16b}, [x1], #16\nret', regs={1: DATA}, mem={0: bytes(range(0x10, 0x20))},
         expect_regs={1: DATA + 16}, expect_v={0: (0x1716151413121110, 0x1F1E1D1C1B1A1918)},
         why='LD1 post-indexed by an immediate advances the base by 16'),
    Case('ld2_post_reg', 'ld2 {v0.16b, v1.16b}, [x1], x2\nret', regs={1: DATA, 2: 16},
         mem={0: bytes(range(0x10, 0x20))},
         expect_regs={1: DATA + 16},
         expect_v={0: (0x1E1C1A1816141210, 0), 1: (0x1F1D1B1917151311, 0)},
         why='LD2 deinterleaves the even and odd bytes and advances the base by the register'),
    Case('hint_nop', 'mov x0, #1\nnop\nret', expect_regs={0: 1}, why='NOP changes nothing'),
    Case('hint_yield', 'mov x0, #1\nyield\nret', expect_regs={0: 1}, why='YIELD is a scheduling hint, not a wait'),
    Case('hint_wfe', 'mov x0, #1\nwfe\nret', expect_regs={0: 1},
         why='WFE must return at once: the recompiled code has no event stream to wait on'),
    Case('hint_wfi', 'mov x0, #1\nwfi\nret', expect_regs={0: 1},
         why='WFI must return at once for the same reason as WFE'),
    Case('hint_sev', 'mov x0, #1\nsev\nret', expect_regs={0: 1}, why='SEV changes nothing observable here'),
    Case('hint_sevl', 'mov x0, #1\nsevl\nret', expect_regs={0: 1}, why='SEVL only sets the local event register'),
    Case('barrier_isb', 'mov x0, #1\nisb\nret', expect_regs={0: 1}, why='ISB is a no-op on the host'),
    Case('barrier_dsb', 'mov x0, #1\ndsb sy\nret', expect_regs={0: 1}, why='DSB is a no-op on the host'),
    Case('barrier_dmb', 'mov x0, #1\ndmb ish\nret', expect_regs={0: 1}, why='DMB is a no-op on the host'),
]

# ----------------------------------------------------------------------------------------------------------------
# Every condition code against every flag state (16 x 16): cond() is what every branch, CSEL and CSET rests on.
# ----------------------------------------------------------------------------------------------------------------
def _cond_holds(cc, nzcv):
    """The ARM ARM's ConditionHolds, written from the pseudocode — not from ctx.h's cond()."""
    n, z, c, v = (nzcv >> 3) & 1, (nzcv >> 2) & 1, (nzcv >> 1) & 1, nzcv & 1
    group, invert = cc >> 1, cc & 1
    if group == 7:
        return True                       # AL and NV both hold, and neither is inverted
    holds = [z == 1, c == 1, n == 1, v == 1,
             c == 1 and z == 0, n == v, n == v and z == 0][group]
    return (not holds) if invert else holds


COND = ['eq', 'ne', 'cs', 'cc', 'mi', 'pl', 'vs', 'vc', 'hi', 'ls', 'ge', 'lt', 'gt', 'le', 'al', 'nv']
for _cc, _name in enumerate(COND):
    for _nz in range(16):
        CASES.append(Case(
            'cond_%s_%s' % (_name, format(_nz, '04b')),
            'b.%s 1f\nmov x0, #1\nb 2f\n1: mov x0, #2\n2: ret' % _name,
            nzcv=_nz, expect_regs={0: 2 if _cond_holds(_cc, _nz) else 1},
            why='condition %s with NZCV %s' % (_name, format(_nz, '04b'))))

# MTE tag stores (x_pac.py): STG changes no data, STZG / STZ2G zero the granule(s) at the address rounded down to 16.
CASES += [
    Case('stg_changes_nothing', 'stg x0, [x1]\nret', regs={0: 0x1234, 1: DATA}, mem={0: b'\xaa' * 64},
         why='STG only writes a tag: no data changes'),
    Case('stzg_zeroes_granule', 'stzg x0, [x1]\nret', regs={0: 0x1234, 1: DATA + 0x10},
         mem={0: b'\xaa' * 64},
         expect_mem={0x10: b'\x00' * 16},
         why='STZG zeroes the 16 bytes of the granule at the address rounded down to 16'),
    Case('stz2g_zeroes_two_granules', 'stz2g x0, [x1]\nret', regs={0: 0x1234, 1: DATA + 0x10},
         mem={0: b'\xaa' * 64},
         expect_mem={0x10: b'\x00' * 32},
         why='STZ2G zeroes 32 bytes: two granules'),
]
