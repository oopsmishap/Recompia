# SPDX-License-Identifier: GPL-3.0-or-later
"""Tier 1 against tier 2: the same instruction forms with pseudo-random machine state, no hand-written expectations.

The lifter has two independent implementations of the ISA — the bit-field decoders in `lift/a64.py` (tier 1) and the
Capstone-driven emitters in `lift/tier2/` (tier 2). This test lifts a generated corpus twice, once through each, runs
both, and compares the whole machine state. A disagreement is a defect in one of them; the test prints both statements.

Nothing here is a hand-derived expectation, so the forms can be generated in bulk: 24 forms x 12 states of registers,
vectors and flags. Only instructions that touch no memory are generated, because a randomised address would fault.
"""
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness  # noqa: E402
from corpus import Case  # noqa: E402

# (name, assembly) — every form reads and writes registers only, so a randomised state is safe.
FORMS = [
    ('add_shifted', 'add x0, x1, x2, lsl #3\nret'),
    ('sub_shifted', 'sub x0, x1, x2, asr #2\nret'),
    ('adds_shifted', 'adds x0, x1, x2, lsr #1\nret'),
    ('subs_shifted', 'subs x0, x1, x2, lsr #7\nret'),
    ('add_extended', 'add x0, x1, w2, uxtb #2\nret'),
    ('subs_extended', 'subs x0, x1, w2, sxtw\nret'),
    ('orr_shifted', 'orr x0, x1, x2, lsl #5\nret'),
    ('orn_shifted', 'orn x0, x1, x2, lsr #9\nret'),
    ('eon_shifted', 'eon x0, x1, x2, asr #11\nret'),
    ('bic_shifted', 'bic x0, x1, x2, lsl #17\nret'),
    ('ands_shifted', 'ands x0, x1, x2, ror #23\nret'),
    ('adc', 'adc x0, x1, x2\nret'),
    ('sbcs', 'sbcs x0, x1, x2\nret'),
    ('csel', 'cmp x1, x2\ncsel x0, x3, x4, gt\nret'),
    ('csneg', 'cmp x1, x2\ncsneg x0, x3, x4, mi\nret'),
    ('madd', 'madd x0, x1, x2, x3\nret'),
    ('mneg', 'mneg x0, x1, x2\nret'),
    ('smulh', 'smulh x0, x1, x2\nret'),
    ('umulh', 'umulh x0, x1, x2\nret'),
    ('sdiv', 'sdiv x0, x1, x2\nret'),
    ('udiv', 'udiv x0, x1, x2\nret'),
    ('lslv', 'lslv x0, x1, x2\nret'),
    ('asrv', 'asrv x0, x1, x2\nret'),
    ('rorv', 'rorv x0, x1, x2\nret'),
    ('extr', 'extr x0, x1, x2, #13\nret'),
    ('rbit', 'rbit x0, x1\nret'),
    ('rev16', 'rev16 x0, x1\nret'),
    ('rev32', 'rev32 x0, x1\nret'),
    ('clz', 'clz x0, x1\nret'),
    ('cls', 'cls x0, x1\nret'),
    ('sbfm', 'sbfiz x0, x1, #5, #11\nret'),
    ('ubfm', 'ubfx x0, x1, #7, #13\nret'),
    ('bfi', 'bfi x0, x1, #9, #17\nret'),
    ('ccmp', 'cmp x1, x2\nccmp x3, #5, #0, ne\nret'),
    ('fadd', 'fadd d0, d1, d2\nret'),
    ('fsub', 'fsub d0, d1, d2\nret'),
    ('fmul', 'fmul d0, d1, d2\nret'),
    ('fdiv', 'fdiv d0, d1, d2\nret'),
    ('fmax', 'fmax d0, d1, d2\nret'),
    ('fminnm', 'fminnm d0, d1, d2\nret'),
    ('fsqrt', 'fsqrt d0, d1\nret'),
    ('fabs_neg', 'fabs d0, d1\nfneg d2, d1\nret'),
    ('fcmp', 'fcmp d1, d2\nret'),
    ('frintn', 'frintn d0, d1\nret'),
    ('frintz', 'frintz d0, d1\nret'),
    ('fcvtzs', 'fcvtzs x0, d1\nret'),
    ('scvtf', 'scvtf d0, x1\nret'),
    ('fmadd', 'fmadd d0, d1, d2, d3\nret'),
    ('fnmsub', 'fnmsub d0, d1, d2, d3\nret'),
    ('simd_add', 'add v0.4s, v1.4s, v2.4s\nret'),
    ('simd_sub', 'sub v0.4s, v1.4s, v2.4s\nret'),
    ('simd_mul', 'mul v0.4s, v1.4s, v2.4s\nret'),
    ('simd_eor', 'eor v0.16b, v1.16b, v2.16b\nret'),
    ('simd_cmeq', 'cmeq v0.4s, v1.4s, v2.4s\nret'),
    ('simd_cmgt', 'cmgt v0.4s, v1.4s, v2.4s\nret'),
    ('simd_shl', 'shl v0.4s, v1.4s, #7\nret'),
    ('simd_ushr', 'ushr v0.4s, v1.4s, #11\nret'),
    ('simd_sshr', 'sshr v0.4s, v1.4s, #3\nret'),
    ('simd_addv', 'addv s0, v1.4s\nret'),
    ('simd_zip1', 'zip1 v0.4s, v1.4s, v2.4s\nret'),
    ('simd_uzp1', 'uzp1 v0.4s, v1.4s, v2.4s\nret'),
    ('simd_trn1', 'trn1 v0.4s, v1.4s, v2.4s\nret'),
    ('simd_ext', 'ext v0.16b, v1.16b, v2.16b, #5\nret'),
    ('simd_neg', 'neg v0.4s, v1.4s\nret'),
    ('pac_identity', 'pacia x0, x1\nret'),
    ('aut_identity', 'autia x0, x1\nret'),
    ('xpac_identity', 'xpaci x0\nret'),
    ('pacga_zero', 'pacga x0, x1, x2\nret'),
    ('pacib_identity', 'pacib x3, x4\nret'),
    ('autda_identity', 'autda x5, x6\nret'),
    ('simd_abs', 'abs v0.4s, v1.4s\nret'),
]

# The 32-bit forms and the SIMD arrangements: a different flag path and different lane widths from the forms above.
FORMS += [
    ('add_w', 'add w0, w1, w2, lsl #2\nret'),
    ('subs_w', 'subs w0, w1, w2\nret'),
    ('adds_w', 'adds w0, w1, w2\nret'),
    ('ands_w', 'ands w0, w1, w2, ror #5\nret'),
    ('adc_w', 'adc w0, w1, w2\nret'),
    ('sbcs_w', 'sbcs w0, w1, w2\nret'),
    ('madd_w', 'madd w0, w1, w2, w3\nret'),
    ('sdiv_w', 'sdiv w0, w1, w2\nret'),
    ('udiv_w', 'udiv w0, w1, w2\nret'),
    ('lslv_w', 'lslv w0, w1, w2\nret'),
    ('asrv_w', 'asrv w0, w1, w2\nret'),
    ('rorv_w', 'rorv w0, w1, w2\nret'),
    ('rbit_w', 'rbit w0, w1\nret'),
    ('rev16_w', 'rev16 w0, w1\nret'),
    ('clz_w', 'clz w0, w1\nret'),
    ('cls_w', 'cls w0, w1\nret'),
    ('ubfx_w', 'ubfx w0, w1, #3, #7\nret'),
    ('sbfiz_w', 'sbfiz w0, w1, #3, #9\nret'),
    ('extr_w', 'extr w0, w1, w2, #5\nret'),
    ('mul_w', 'mul w0, w1, w2\nret'),
    ('smull', 'smull x0, w1, w2\nret'),
    ('umull', 'umull x0, w1, w2\nret'),
    ('sxtw', 'sxtw x0, w1\nret'),
    ('movk32', 'movz w0, #0x1234\nmovk w0, #0xabcd, lsl #16\nret'),
    ('csel_w', 'cmp w1, w2\ncsel w0, w3, w4, hi\nret'),
    ('simd_add_8b', 'add v0.8b, v1.8b, v2.8b\nret'),
    ('simd_add_4h', 'add v0.4h, v1.4h, v2.4h\nret'),
    ('simd_add_2d', 'add v0.2d, v1.2d, v2.2d\nret'),
    ('simd_sub_8h', 'sub v0.8h, v1.8h, v2.8h\nret'),
    ('simd_mul_8h', 'mul v0.8h, v1.8h, v2.8h\nret'),
    ('simd_cmeq_8b', 'cmeq v0.8b, v1.8b, v2.8b\nret'),
    ('simd_cmgt_8h', 'cmgt v0.8h, v1.8h, v2.8h\nret'),
    ('simd_cmge_2d', 'cmge v0.2d, v1.2d, v2.2d\nret'),
    ('simd_shl_8h', 'shl v0.8h, v1.8h, #5\nret'),
    ('simd_ushr_2d', 'ushr v0.2d, v1.2d, #13\nret'),
    ('simd_sshr_8h', 'sshr v0.8h, v1.8h, #3\nret'),
    ('simd_xtn', 'xtn v0.4h, v1.4s\nret'),
    ('simd_uqxtn', 'uqxtn v0.8b, v1.8h\nret'),
    ('simd_sqxtn', 'sqxtn v0.8b, v1.8h\nret'),
    ('simd_addhn', 'addhn v0.8b, v1.8h, v2.8h\nret'),
    ('simd_saddl', 'saddl v0.4s, v1.4h, v2.4h\nret'),
    ('simd_umull', 'umull v0.4s, v1.4h, v2.4h\nret'),
    ('simd_addp', 'addp v0.4s, v1.4s, v2.4s\nret'),
    ('simd_smaxv', 'smaxv s0, v1.4s\nret'),
    ('simd_uminv', 'uminv h0, v1.4h\nret'),
    ('simd_cnt', 'cnt v0.8b, v1.8b\nret'),
    ('simd_rev16', 'rev16 v0.16b, v1.16b\nret'),
    ('simd_rev32', 'rev32 v0.8h, v1.8h\nret'),
    ('simd_rev64', 'rev64 v0.4s, v1.4s\nret'),
    ('simd_sqadd', 'sqadd v0.4h, v1.4h, v2.4h\nret'),
    ('simd_uqsub', 'uqsub v0.8b, v1.8b, v2.8b\nret'),
    ('simd_zip2', 'zip2 v0.8h, v1.8h, v2.8h\nret'),
    ('simd_uzp2', 'uzp2 v0.4s, v1.4s, v2.4s\nret'),
    ('simd_trn2', 'trn2 v0.2d, v1.2d, v2.2d\nret'),
    ('simd_tbl', 'tbl v0.16b, {v1.16b}, v2.16b\nret'),
    ('simd_dup', 'dup v0.4s, w1\nret'),
    ('simd_ins', 'ins v0.s[2], w1\nret'),
    ('simd_umov', 'umov w0, v1.s[3]\nret'),
    ('fadd_s', 'fadd s0, s1, s2\nret'),
    ('fdiv_s', 'fdiv s0, s1, s2\nret'),
    ('fmaxnm_s', 'fmaxnm s0, s1, s2\nret'),
    ('fsqrt_s', 'fsqrt s0, s1\nret'),
    ('fcvt_sd', 'fcvt s0, d1\nret'),
    ('fcvt_ds', 'fcvt d0, s1\nret'),
    ('fcvtms', 'fcvtms x0, d1\nret'),
    ('fcvtps', 'fcvtps x0, d1\nret'),
    ('fcvtzs_w', 'fcvtzs w0, d1\nret'),
    ('scvtf_s', 'scvtf s0, w1\nret'),
    ('ucvtf', 'ucvtf d0, x1\nret'),
    ('frinta', 'frinta d0, d1\nret'),
    ('frintx', 'frintx d0, d1\nret'),
    ('fmadd_s', 'fmadd s0, s1, s2, s3\nret'),
]

# Different encodings with identical semantics: the two statements must leave the same state on any input.
PAIRS = [
    ('add x0, x1, #0\nret', 'mov x0, x1\nret'),
    ('sub x0, x1, #0\nret', 'mov x0, x1\nret'),
    ('orr x0, x1, x1\nret', 'mov x0, x1\nret'),
    ('and x0, x1, x1\nret', 'mov x0, x1\nret'),
    ('eor x0, x1, x1\nret', 'mov x0, #0\nret'),
    ('lsl x0, x1, #0\nret', 'mov x0, x1\nret'),
    ('extr x0, x1, x1, #0\nret', 'mov x0, x1\nret'),
    ('ror x0, x1, #8\nret', 'extr x0, x1, x1, #8\nret'),
    ('neg x0, x1\nret', 'sub x0, xzr, x1\nret'),
    ('mvn x0, x1\nret', 'orn x0, xzr, x1\nret'),
    ('sxtw x0, w1\nret', 'sbfm x0, x1, #0, #31\nret'),
    ('uxtb w0, w1\nret', 'ubfm w0, w1, #0, #7\nret'),
    ('uxth w0, w1\nret', 'ubfm w0, w1, #0, #15\nret'),
    ('cmp x1, x2\nret', 'subs xzr, x1, x2\nret'),
    ('tst x1, x2\nret', 'ands xzr, x1, x2\nret'),
    ('cset x0, eq\nret', 'csinc x0, xzr, xzr, ne\nret'),
    ('csetm x0, ne\nret', 'csinv x0, xzr, xzr, eq\nret'),
    ('cneg x0, x1, mi\nret', 'csneg x0, x1, x1, pl\nret'),
    ('mov v0.16b, v1.16b\nret', 'orr v0.16b, v1.16b, v1.16b\nret'),
    ('fmov d0, d1\nret', 'fmov d0, d1\nret'),
]

STATES = 12


def generated():
    """One case per (form, state): registers, vectors and flags from a seeded generator, so the run is reproducible."""
    rnd = random.Random(0xC0FFEE)
    out = []
    for name, asm in FORMS:
        for k in range(STATES):
            regs = {r: rnd.getrandbits(64) for r in range(31)}
            vregs = {r: (rnd.getrandbits(64), rnd.getrandbits(64)) for r in range(32)}
            out.append(Case('%s_%02d' % (name, k), asm, regs=regs, vregs=vregs, nzcv=rnd.getrandbits(4)))
    return out


class Tier2Agrees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = generated()
        cls.workdir = tempfile.mkdtemp(prefix='nx-fuzz-')
        cls.runs = {}
        for label, tier2 in (('tier1', False), ('tier2', True), ('lean', False)):
            mode = 'eager' if label == 'tier1' else 'lean'
            words, out = harness.lift_cases(cls.cases, mode, tier2_only=tier2)
            exe = harness.compile_runner(cls.cases, [t for _, t, _ in out], cls.workdir, label)
            recs = harness.run_cases(cls.cases, exe, cls.workdir,
                                     [harness.setup_record(c, i) for i, c in enumerate(cls.cases)], words)
            cls.runs[label] = (recs, [t for _, t, _ in out], dict(harness.TIER2_STATS))

    def test_locals_modes_agree(self):
        """'eager' keeps every register in the Ctx; 'lean' holds them in C locals and syncs only where other code can
        see them. The two must leave the same machine state, on any input."""
        eager, _, _ = self.runs['tier1']
        recs, _, _ = self.runs['lean']
        bad = []
        for i, case in enumerate(self.cases):
            if eager[i] != recs[i]:
                diffs = [n for n, (a, b) in enumerate(zip(eager[i], recs[i])) if a != b]
                bad.append('%s (%s): %d field(s) differ, first at %d (eager %r, lean %r)'
                           % (case.name, case.asm.splitlines()[0], len(diffs), diffs[0],
                              eager[i][diffs[0]], recs[i][diffs[0]]))
        if bad:
            self.fail('%d of %d generated cases disagree between the eager and lean locals modes:\n\n%s'
                      % (len(bad), len(self.cases), '\n'.join(bad[:20])))

    def test_agreement(self):
        recs1, texts1, _ = self.runs['tier1']
        recs2, texts2, stats = self.runs['tier2']
        self.assertGreaterEqual(stats.get('t2', 0), len(self.cases) // 2,
                                'only %d statements came from tier 2' % stats.get('t2', 0))
        bad = []
        for i, case in enumerate(self.cases):
            if recs1[i] != recs2[i]:
                diffs = [n for n, (a, b) in enumerate(zip(recs1[i], recs2[i])) if a != b]
                bad.append('%s (%s): %d field(s) differ, first at %d\n--- tier1 ---\n%s\n--- tier2 ---\n%s'
                           % (case.name, case.asm.splitlines()[0], len(diffs), diffs[0],
                              '\n'.join(texts1[i].splitlines()[1:7]),
                              '\n'.join(texts2[i].splitlines()[1:7])))
        if bad:
            self.fail('%d of %d generated cases disagree between tier 1 and tier 2:\n\n%s'
                      % (len(bad), len(self.cases), '\n\n'.join(bad[:20])))

    def test_alias_pairs_agree(self):
        """Two encodings with the same meaning must leave the same state; a disagreement is a decoding defect."""
        rnd = random.Random(0xA11A5)
        cases, tag = [], []
        for a, b in PAIRS:
            for k in range(6):
                regs = {r: rnd.getrandbits(64) for r in range(31)}
                vregs = {r: (rnd.getrandbits(64), rnd.getrandbits(64)) for r in range(32)}
                nz = rnd.getrandbits(4)
                cases.append(Case('pair_%s_%02d' % (a.split()[0], k), a, regs=regs, vregs=vregs, nzcv=nz))
                cases.append(Case('pair_%s_%02d_b' % (a.split()[0], k), b, regs=regs, vregs=vregs, nzcv=nz))
                tag.append((a, b))
        words, out = harness.lift_cases(cases, 'eager')
        exe = harness.compile_runner(cases, [t for _, t, _ in out], self.workdir, 'pairs')
        recs = harness.run_cases(cases, exe, self.workdir,
                                 [harness.setup_record(c, i) for i, c in enumerate(cases)], words)
        bad = []
        for i in range(0, len(cases), 2):
            if recs[i] != recs[i + 1]:
                diffs = [n for n, (x, y) in enumerate(zip(recs[i], recs[i + 1])) if x != y]
                bad.append('%s vs %s: field %d (%r vs %r)' % (tag[i][0].splitlines()[0], tag[i][1].splitlines()[0],
                                                              diffs[0], recs[i][diffs[0]], recs[i + 1][diffs[0]]))
        if bad:
            self.fail('%d of %d encoding pairs disagree:\n\n%s' % (len(bad), len(PAIRS) * 6, '\n'.join(bad[:20])))

    def test_spin_rewrite_agrees(self):
        """A busy-wait loop's loads become poll loads (spin.py); the value they return must not change."""
        loops = [
            ('spin_cbz', '1: ldr w0, [x1]\ncbz w0, 1b\nret'),
            ('spin_ldrb', '1: ldrb w0, [x1]\ncbz w0, 1b\nret'),
            ('spin_two_loads', '1: ldr w0, [x1]\nldr w2, [x1, #4]\ncbz w0, 1b\nret'),
            ('spin_wfe', '1: ldr w0, [x1]\ncbnz w0, 2f\nwfe\nb 1b\n2: ret'),
        ]
        rnd = random.Random(0x5917)
        cases = []
        for name, asm in loops:
            for k in range(4):
                cases.append(Case('%s_%d' % (name, k), asm,
                                  regs={1: harness.DATA, 2: 0x1234, 3: 0x5678},
                                  mem={0: (k + 1).to_bytes(4, 'little'), 4: rnd.getrandbits(32).to_bytes(4, 'little')},
                                  nzcv=rnd.getrandbits(4)))
        runs = {}
        for label, spin in (('plain', False), ('spin', True)):
            words, out = harness.lift_cases(cases, 'eager', spin=spin)
            exe = harness.compile_runner(cases, [t for _, t, _ in out], self.workdir, label)
            runs[label] = (harness.run_cases(cases, exe, self.workdir,
                                             [harness.setup_record(c, i) for i, c in enumerate(cases)], words),
                           [t for _, t, _ in out])
        self.assertNotEqual(runs['plain'][1], runs['spin'][1],
                            'spin.py rewrote nothing, so this comparison proves nothing')
        bad = []
        for i, case in enumerate(cases):
            if runs['plain'][0][i] != runs['spin'][0][i]:
                diffs = [n for n, (a, b) in enumerate(zip(runs['plain'][0][i], runs['spin'][0][i])) if a != b]
                bad.append('%s: field %d differs (plain %r, spin %r)'
                           % (case.name, diffs[0], runs['plain'][0][i][diffs[0]], runs['spin'][0][i][diffs[0]]))
        if bad:
            self.fail('%d of %d spin-loop cases disagree:\n\n%s' % (len(bad), len(cases), '\n'.join(bad[:20])))

    def test_calls_agree_across_modes(self):
        """Registers dirty before a call must reach the callee and survive it, in every locals mode. This is the
        spill/reload path the whole locals transform exists for."""
        callees = [
            ('callee_read_x0', 'add x0, x0, #1\nret'),
            ('callee_read_x19', 'add x19, x19, #1\nret'),
            ('callee_write_x8', 'mov x8, #0\nret'),
            ('callee_read_x1_x2', 'add x1, x1, x2\nret'),
            ('callee_read_x29', 'add x29, x29, #8\nret'),
        ]
        callers = [
            ('caller_dirty', 'add x5, x0, x1\nmov x19, x2\nbl {callee}\nret'),
            ('caller_dirty2', 'eor x6, x0, x1\nmov x29, x3\nbl {callee}\nadd x7, x6, x5\nret'),
        ]
        rnd = random.Random(0xCA11)
        cases = []
        for cname, casm in callees:
            cases.append(Case(cname, casm))
            for kname, kasm in callers:
                for k in range(4):
                    regs = {r: rnd.getrandbits(64) for r in range(31)}
                    vregs = {r: (rnd.getrandbits(64), rnd.getrandbits(64)) for r in range(32)}
                    cases.append(Case('%s_%s_%d' % (kname, cname, k), kasm, regs=regs, vregs=vregs,
                                      nzcv=rnd.getrandbits(4), calls={'callee': cname}))
        runs = {}
        for mode in ('eager', 'exact', 'lean', 'aapcs'):
            words, out = harness.lift_cases(cases, mode)
            exe = harness.compile_runner(cases, [t for _, t, _ in out], self.workdir, 'calls_' + mode)
            runs[mode] = harness.run_cases(cases, exe, self.workdir,
                                           [harness.setup_record(c, i) for i, c in enumerate(cases)], words)
        bad = []
        for i, case in enumerate(cases):
            for mode in ('exact', 'lean', 'aapcs'):
                if runs[mode][i] != runs['eager'][i]:
                    diffs = [n for n, (a, b) in enumerate(zip(runs['eager'][i], runs[mode][i])) if a != b]
                    bad.append('%s: %s vs eager, field %d (%r vs %r)'
                               % (case.name, mode, diffs[0], runs['eager'][i][diffs[0]], runs[mode][i][diffs[0]]))
        if bad:
            self.fail('%d of %d call cases disagree across locals modes:\n\n%s'
                      % (len(bad), len(cases) * 3, '\n'.join(bad[:20])))


if __name__ == '__main__':
    unittest.main()
