# SPDX-License-Identifier: GPL-3.0-or-later
"""The lifter's semantics, checked against the corpus (recomp/tests/corpus.py) in every locals mode.

    python -m unittest discover -s recomp/tests        (from the repository root)

Each mode lifts the whole corpus with the real pipeline, compiles it with the shipping optimisation (-O1, the flags of
compile_flags.CLANG), runs every snippet and compares the whole machine state: all 32 X registers, all 32 vector
registers, the flags where the case states them, and the case's whole 4 KiB data page.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness  # noqa: E402
from corpus import CASES  # noqa: E402
from harness import MODES, EV_NONE, EV_TRAP, EV_SVC, EV_CALL, EV_IMPORT, EV_UNIMPL  # noqa: E402

MASK = 0xffffffffffffffff
EV_NAMES = {EV_NONE: 'none', EV_TRAP: 'trap', EV_SVC: 'svc', EV_CALL: 'indirect call', EV_IMPORT: 'import',
            EV_UNIMPL: 'untranslated'}


def _vpair(val):
    return val if isinstance(val, tuple) else (val, 0)


def check(case, index, rec):
    """Every difference between the case's expectations and the record the runner wrote."""
    out = []
    x = rec[0:32]
    v = rec[32:96]
    nzcv, ev, evarg, page = rec[96], rec[98], rec[100], rec[101]

    for r in range(32):
        if r in case.expect_any:
            continue
        want = harness.resolve(case.expect_regs.get(r, case.regs.get(r, 0)), index) & MASK
        if x[r] != want:
            out.append('x%-2d = 0x%016x, expected 0x%016x' % (r, x[r], want))

    for r in range(32):
        lo, hi = _vpair(case.expect_v.get(r, case.vregs.get(r, 0)))
        want_lo, want_hi = lo & MASK, hi & MASK
        if v[2 * r] != want_lo or v[2 * r + 1] != want_hi:
            out.append('v%-2d = 0x%016x%016x, expected 0x%016x%016x' % (r, v[2 * r + 1], v[2 * r], want_hi, want_lo))

    if case.expect_nzcv is not None and nzcv != case.expect_nzcv:
        out.append('NZCV = %s, expected %s' % (format(nzcv, '04b'), format(case.expect_nzcv, '04b')))

    want_page = bytearray(4096)
    for off, data in case.mem.items():
        want_page[off:off + len(data)] = data
    for off, data in case.expect_mem.items():
        want_page[off:off + len(data)] = data
    if page != bytes(want_page):
        diffs = [i for i in range(4096) if page[i] != want_page[i]]
        first = diffs[0]
        out.append('data page: %d byte(s) differ, first at +0x%x: 0x%02x, expected 0x%02x'
                   % (len(diffs), first, page[first], want_page[first]))

    if ev != case.expect_ev:
        out.append('event = %s(%d), expected %s' % (EV_NAMES.get(ev, '?'), evarg, EV_NAMES.get(case.expect_ev, '?')))
    elif case.expect_evarg is not None and evarg != case.expect_evarg:
        out.append('event argument = 0x%x, expected 0x%x' % (evarg, case.expect_evarg))
    return out


class Semantics(unittest.TestCase):
    """One test per locals mode; a failure lists every case that disagrees with the corpus."""

    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.mkdtemp(prefix='nx-lift-test-')
        cls.runs = {}
        for mode in MODES:
            words, out = harness.lift_cases(CASES, mode)
            untranslated = [(c.name, bad) for c, _, bad in out]
            texts = [t for _, t, _ in out]
            exe = harness.compile_runner(CASES, texts, cls.workdir, mode)
            recs = harness.run_cases(CASES, exe, cls.workdir,
                                     [harness.setup_record(c, i) for i, c in enumerate(CASES)], words)
            cls.runs[mode] = (recs, untranslated)

    def _mode(self, mode):
        recs, untranslated = self.runs[mode]
        failures = []
        for i, case in enumerate(CASES):
            bad = untranslated[i][1]
            if bad and not case.allow_bad:
                failures.append('%s: the lifter could not translate %s' % (case.name, bad))
                continue
            diffs = check(case, i, recs[i])
            if diffs:
                failures.append('%s  (%s)\n    %s' % (case.name, case.why, '\n    '.join(diffs)))
        if failures:
            self.fail('%d of %d cases disagree in %r mode:\n\n%s'
                      % (len(failures), len(CASES), mode, '\n\n'.join(failures)))

    def test_eager(self):
        self._mode('eager')

    def test_exact(self):
        self._mode('exact')

    def test_lean(self):
        self._mode('lean')


if __name__ == '__main__':
    unittest.main()
