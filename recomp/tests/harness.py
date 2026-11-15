# SPDX-License-Identifier: GPL-3.0-or-later
"""Differential harness for the lifter: AArch64 assembly -> the lifter's C++ -> execute -> compare with the expected
machine state.  (recomp/tests/ is test-only: no build includes it and no workflow runs it.)

One run:
  1. assemble every case with clang (AArch64 cross), one object per case, and take its .text words;
  2. build one synthetic Image holding all the cases (each on its own page from CODE_BASE) and lift it with the real
     pipeline (Env, discovery state, the 'lean' summary pass and flag table, lift_function) in every locals mode the
     shipping build uses;
  3. compile the lifted text with a runner that maps a data page per case, sets a Ctx up from the case's setup, calls the
     lifted function, and writes back the final registers, flags, page and any runtime event;
  4. compare against the case's expectations: every register, the flags it states, the whole data page.

The expectations are literals in corpus.py, derived from the Arm ARM. They are never computed from the lifter.
"""
import os
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
RECOMP = os.path.dirname(HERE)
ROOT = os.path.dirname(RECOMP)
INCLUDE = os.path.join(RECOMP, 'include')

sys.path.insert(0, RECOMP)

CLANG = os.environ.get('NX_TEST_CLANG', r'C:\Program Files\LLVM\bin\clang.exe')
OBJCOPY = os.environ.get('NX_TEST_OBJCOPY', r'C:\Program Files\LLVM\bin\llvm-objcopy.exe')

CODE_BASE = 0x10000000      # guest address of case 0's code; case i starts at CODE_BASE + i * PAGE
PAGE = 0x1000
DATA_BASE = 0x20000000      # guest address of case i's data page (the runner maps it with VirtualAlloc)

MODES = ('eager', 'exact', 'lean')          # locals.py: None, 'exact', 'lean'

# The record the runner reads and writes per case: x0..x31, v0..v31 (as 64-bit halves), the resume address
# (a landing pad, else 0), NZCV, the event, and the data page.
REC = struct.Struct('<32Q 64Q Q B B B B I 4096s')

EV_NONE, EV_TRAP, EV_SVC, EV_CALL, EV_IMPORT, EV_UNIMPL = 0, 1, 2, 3, 4, 5


# ------------------------------------------------------------------------------------------------ the data page
class _DataPage:
    """Stands for the case's own data page address in a corpus value; DATA + n is that address plus n."""

    def __add__(self, off):
        return _DataAt(off)

    __radd__ = __add__

    def __repr__(self):
        return 'DATA'


DATA = _DataPage()


class _DataAt:
    def __init__(self, off):
        self.off = off

    def __repr__(self):
        return 'DATA+%d' % self.off


def resolve(v, index):
    """A corpus value with the DATA sentinels replaced by case `index`'s data page address."""
    if v is DATA:
        return DATA_BASE + index * PAGE
    if isinstance(v, _DataAt):
        return DATA_BASE + index * PAGE + v.off
    return v


# ------------------------------------------------------------------------------------------------ assembly

_asm_cache = {}


def assemble(asm):
    """The instruction words of one case's assembly text (clang cross-assembles it; the .text section is the code)."""
    if asm in _asm_cache:
        return _asm_cache[asm]
    with tempfile.TemporaryDirectory(prefix='nxt-asm-') as d:
        s, o, b = (os.path.join(d, n) for n in ('c.s', 'c.o', 'c.bin'))
        with open(s, 'w', encoding='utf-8') as f:
            f.write('.text\n' + asm.strip() + '\n')
        p = subprocess.run([CLANG, '--target=aarch64-none-elf', '-march=armv8.2-a', '-c', s, '-o', o],
                           capture_output=True)
        if p.returncode:
            raise RuntimeError('assembly failed for %r: %s' % (asm, p.stderr.decode('utf-8', 'replace')[-1500:]))
        p = subprocess.run([OBJCOPY, '-O', 'binary', '--only-section=.text', o, b], capture_output=True)
        if p.returncode:
            raise RuntimeError('objcopy failed: %s' % p.stderr.decode('utf-8', 'replace')[-1500:])
        with open(b, 'rb') as f:
            raw = f.read()
    if len(raw) % 4:
        raise RuntimeError('case code is not a whole number of words: %r' % (asm,))
    words = struct.unpack('<%dI' % (len(raw) // 4), raw)
    _asm_cache[asm] = words
    return words


def assemble_all(cases):
    """Assemble every case. A `calls` token becomes the displacement to the case it names, so a call may sit anywhere
    in the case: the assembler counts from the start of the case's own code and the call's own position comes off."""
    index = {c.name: i for i, c in enumerate(cases)}

    def text(i, c):
        # The assembler takes the number after `bl` as the displacement itself, so the call's own position in the case
        # has to come off the distance between the two cases' pages.
        lines = c.asm.splitlines()
        for tok, target in c.calls.items():
            for k, line in enumerate(lines):
                if '{%s}' % tok in line:
                    lines[k] = line.replace('{%s}' % tok, str((index[target] - i) * PAGE - 4 * k))
        return '\n'.join(lines)

    with ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4))) as ex:
        return list(ex.map(lambda ic: assemble(text(*ic)), enumerate(cases)))


# ------------------------------------------------------------------------------------------------ lifting
TIER2_STATS = {}


class _Tier2Lifter(object):
    """Stand-in for nxrecomp.lift.Lifter with tier 1 skipped: every statement comes from the Capstone-driven emitters."""

    def __init__(self, entry, end, env):
        from nxrecomp.lift import Lifter
        self._inner = Lifter(entry, end, env)
        self._t2 = None
        self.targets = self._inner.targets
        self.has_br_fallback = False

    def lift(self, w, pc):
        from nxrecomp.lift import Fn
        from nxrecomp.lift.a64 import Unimpl
        from nxrecomp.lift.tier2 import Tier2
        if self._t2 is None:
            self._t2 = Tier2.shared()
        try:
            text = self._t2.lift(self._inner, w, pc)
            TIER2_STATS['t2'] = TIER2_STATS.get('t2', 0) + 1
        except Unimpl:
            text = Fn.lift(self._inner, w, pc)      # tier 2 never handles branches; tier 1 does
            TIER2_STATS['t1'] = TIER2_STATS.get('t1', 0) + 1
        self.targets = self._inner.targets
        self.has_br_fallback = self._inner.has_br_fallback
        return text


def build_env(cases, words, mode, spin=False):
    """The Env the real lift builds, over a synthetic image that holds every case on its own code page."""
    from nxrecomp.config import load_game
    from nxrecomp.env import Env
    from nxrecomp.image import Image

    mem = bytearray()
    ends = {}
    for i, w in enumerate(words):
        entry = CODE_BASE + i * PAGE
        if not w:
            raise RuntimeError('case %s assembled to nothing' % cases[i].name)
        if len(w) * 4 > PAGE:
            raise RuntimeError('case %s is longer than one page' % cases[i].name)
        mem += struct.pack('<%dI' % len(w), *w)
        mem += b'\0' * (PAGE - len(w) * 4)
        ends[entry] = entry + len(w) * 4
    img = Image('test', CODE_BASE, bytes(mem), (0, len(mem)), (0, 0))
    env = Env(img, load_game('pkp'))
    env.ends = ends
    env.starts = sorted(ends)
    env.pads = []
    for i, c in enumerate(cases):
        k = getattr(c, 'pad_index', None)
        if k is None:
            continue
        if not 0 <= k < len(words[i]):
            raise RuntimeError('case %s: pad index %r is not one of its %d instructions'
                               % (c.name, k, len(words[i])))
        env.pads.append(CODE_BASE + i * PAGE + 4 * k)
    env.pads.sort()
    env.spin = spin
    env.locals = None if mode == 'eager' else mode
    env.lean_held = frozenset(env.starts)   # every snippet is called from outside, so 'lean' must publish its flags
    from nxrecomp import jumptables
    jumptables.resolve(env)
    if mode == 'lean':
        jumptables.bound_dispatch(env)
    return env


def lift_cases(cases, mode, tier2_only=False, log=None, spin=False):
    """-> (the assembled words, [(case, text, untranslated)]). tier2_only: skip tier 1, so the statements come from the
    Capstone-driven emitters (lift/tier2/) — a second implementation of the same ISA, checked against the same corpus."""
    from nxrecomp.driver import flag_table
    from nxrecomp.emit_function import lift_function
    import nxrecomp.emit_function as ef

    words = assemble_all(cases)
    if tier2_only:
        TIER2_STATS.clear()
    env = build_env(cases, words, mode, spin)
    entries = sorted(env.ends)
    saved = ef.Lifter
    if tier2_only:
        ef.Lifter = _Tier2Lifter
    try:
        if mode == 'lean':
            # the summary pass, then the flag table, then the text pass: what lift_module does, in the same order
            env.locals_summaries, env.lean_flows = {}, {}
            for a in entries:
                lift_function(env, a, text=False)
            sums = env.locals_summaries
            flows = env.lean_flows
            env.flags_table = flag_table(sums, env, env.cfg, env.img, entries, log or (lambda *_: None))
            env.lean_flows = flows
        out = []
        for i, a in enumerate(entries):
            text, n, bad, _ = lift_function(env, a)
            out.append((cases[i], text, bad))
    finally:
        ef.Lifter = saved
    return words, out


# ------------------------------------------------------------------------------------------------ the runner

RUNNER_HEAD = r'''
// generated by recomp/tests/harness.py -- do not edit
#include <nxrecomp/cxx.h>
#include <cstdio>
#include <cstring>
#include <csetjmp>
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

struct Rec { uint64_t x[32]; uint64_t v[64]; uint64_t rp; uint8_t nzcv, trap, ev, spare; uint32_t evarg; uint8_t mem[4096]; };

static std::jmp_buf g_jmp;
static uint8_t g_ev;
static uint32_t g_evarg;

static void stop(uint8_t ev, uint32_t arg) { g_ev = ev; g_evarg = arg; std::longjmp(g_jmp, 1); }

extern "C" {
void call_indirect(Ctx *, uint64_t addr) { stop(3, (uint32_t)addr); }
void host_import(Ctx *, uint32_t id) { stop(4, id); }
void guest_trap(const char *what, uint64_t addr) { (void)what; stop(1, (uint32_t)addr); }
void guest_unimpl(uint64_t pc, uint32_t insn, const char *what) { (void)pc; (void)insn; (void)what; stop(5, 0); }
void guest_svc(Ctx *, uint32_t number) { stop(2, number); }
uint64_t guest_mrs(Ctx *, uint32_t) { return 0; }
void guest_msr(Ctx *, uint32_t, uint64_t) {}
void nx_site_inline(Ctx *, uint64_t) {}
int nx_site_midhook(Ctx *, uint64_t) { return 0; }
int nx_hook_entry(Ctx *, uint64_t) { return 0; }
void stx_diag(Ctx *, uint64_t, uint64_t, uint64_t, uint64_t, int) {}
void nx_catch_call(Ctx *c, void (*body)(Ctx *, uint64_t)) { body(c, 0); }
}
'''


def runner_source(cases, texts):
    """The TU: the lifted text of every case, a table of the functions, and a main that runs them all."""
    decls, table = [], []
    for i, c in enumerate(cases):
        a = CODE_BASE + i * PAGE
        decls.append('void f_%x(Ctx *);' % a)
        if getattr(c, 'pad_index', None) is not None:   # a two-entry function: resume through u_(c, pad)
            decls.append('void u_%x(Ctx *, uint64_t);' % a)
            table.append('  { "%s", f_%x, u_%x }' % (c.name, a, a))
        else:
            table.append('  { "%s", f_%x, 0 }' % (c.name, a))
    decls = '\n'.join(decls)
    table = ',\n'.join(table)
    return (RUNNER_HEAD + '\n' + decls + '\n\n' + '\n\n'.join(texts)
            + '\n\nstatic const struct { const char *name; void (*fn)(Ctx *); void (*ufn)(Ctx *, uint64_t); } kCases[] = {\n'
            + table + '\n};\n\n' + r'''
int main(int argc, char **argv) {
    if (argc != 4) { std::fprintf(stderr, "usage: runner setup.bin result.bin code.bin\n"); return 2; }
    FILE *in = std::fopen(argv[1], "rb"), *out = std::fopen(argv[2], "wb");
    if (!in || !out) { std::fprintf(stderr, "cannot open %s / %s\n", argv[1], argv[2]); return 2; }
    const int n = (int)(sizeof kCases / sizeof kCases[0]);
    // the guest's code pages: only literal loads read them, but they must be there
    void *code = VirtualAlloc((void *)(uintptr_t)0x10000000ull, (SIZE_T)n * 4096, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!code) { std::fprintf(stderr, "VirtualAlloc code failed (err %lu)\n", GetLastError()); return 3; }
    {
        FILE *cf = std::fopen(argv[3], "rb");
        if (!cf || std::fread(code, 4096, (SIZE_T)n, cf) != (SIZE_T)n) { std::fprintf(stderr, "code.bin short\n"); return 3; }
        std::fclose(cf);
    }
    // one block for every case's data page: VirtualAlloc rounds a base down to 64 KiB, so the block must be aligned
    unsigned char *data = (unsigned char *)VirtualAlloc((void *)(uintptr_t)0x20000000ull, (SIZE_T)n * 4096,
                                                        MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!data) { std::fprintf(stderr, "VirtualAlloc data failed (err %lu)\n", GetLastError()); return 3; }
    for (int i = 0; i < n; i++) {
        Rec r;
        if (std::fread(&r, sizeof r, 1, in) != 1) { std::fprintf(stderr, "setup %d short\n", i); return 2; }
        unsigned char *p = data + (SIZE_T)i * 4096;
        std::memcpy(p, r.mem, 4096);
        Ctx c;
        std::memset(&c, 0, sizeof c);
        std::memcpy(c.x, r.x, sizeof c.x);
        std::memcpy(c.v, r.v, sizeof c.v);
        c.N = (r.nzcv >> 3) & 1; c.Z = (r.nzcv >> 2) & 1; c.C = (r.nzcv >> 1) & 1; c.V = r.nzcv & 1;
        c.ex_addr = ~0ull;
        g_ev = 0; g_evarg = 0;
        if (setjmp(g_jmp) == 0) { if (kCases[i].ufn) kCases[i].ufn(&c, r.rp); else kCases[i].fn(&c); }
        Rec o;
        std::memset(&o, 0, sizeof o);
        std::memcpy(o.x, c.x, sizeof o.x);
        std::memcpy(o.v, c.v, sizeof o.v);
        o.rp = r.rp;
        o.nzcv = (uint8_t)((c.N & 1) << 3 | (c.Z & 1) << 2 | (c.C & 1) << 1 | (c.V & 1));
        o.trap = 0; o.ev = g_ev; o.evarg = g_evarg;
        std::memcpy(o.mem, p, 4096);
        std::fwrite(&o, sizeof o, 1, out);
    }
    std::fclose(in); std::fclose(out);
    return 0;
}
''')


# ------------------------------------------------------------------------------------------------ compiling and running

def compile_runner(cases, texts, workdir, tag='run'):
    """Compile the runner and the lifted text; -> the executable path."""
    src = os.path.join(workdir, tag + '.cpp')
    exe = os.path.join(workdir, tag + '.exe')
    with open(src, 'w', encoding='utf-8', newline='\n') as f:
        f.write(runner_source(cases, texts))
    flags = ['-std=c++20', '-O1', '-w', '-fno-exceptions', '-fno-rtti', '-fno-strict-aliasing', '-ffp-contract=off',
             '-mcx16', '-D_ALLOW_COMPILER_AND_STL_VERSION_MISMATCH', '-I', INCLUDE, src, '-o', exe]
    # (-fsanitize=undefined is not usable here: clang 18 has no UBSan runtime for the MSVC ABI, and the link fails.)
    p = subprocess.run([CLANG] + flags, capture_output=True)
    if p.returncode:
        raise RuntimeError('compiling the lifted code failed:\n'
                           + p.stderr.decode('utf-8', 'replace')[-6000:])
    return exe


def run_cases(cases, exe, workdir, setups, words):
    """Run every case in one process; -> the list of result records."""
    setup_path = os.path.join(workdir, 'setup.bin')
    result_path = os.path.join(workdir, 'result.bin')
    code_path = os.path.join(workdir, 'code.bin')
    with open(setup_path, 'wb') as f:
        for s in setups:
            f.write(s)
    with open(code_path, 'wb') as f:
        for w in words:
            f.write(struct.pack('<%dI' % len(w), *w))
            f.write(b'\0' * (PAGE - len(w) * 4))
    p = subprocess.run([exe, setup_path, result_path, code_path], capture_output=True)
    if p.returncode:
        raise RuntimeError('the runner failed (%d): %s' % (p.returncode, p.stderr.decode('utf-8', 'replace')[-3000:]))
    with open(result_path, 'rb') as f:
        raw = f.read()
    if len(raw) != REC.size * len(cases):
        raise RuntimeError('the runner wrote %d bytes, expected %d' % (len(raw), REC.size * len(cases)))
    return [REC.unpack_from(raw, i * REC.size) for i in range(len(cases))]


def setup_record(case, index):
    """The runner's input record for one case: its registers, vectors, flags and data page."""
    x = [0] * 32
    for n, v in case.regs.items():
        x[n] = resolve(v, index) & 0xffffffffffffffff
    v = [0] * 64
    for n, val in case.vregs.items():
        lo, hi = val if isinstance(val, tuple) else (val, 0)
        v[2 * n], v[2 * n + 1] = lo & 0xffffffffffffffff, hi & 0xffffffffffffffff
    mem = bytearray(4096)
    for off, data in case.mem.items():
        mem[off:off + len(data)] = data
    rp = case.rp
    if rp == 'pad':                     # the case's own landing pad address
        rp = CODE_BASE + index * PAGE + 4 * case.pad_index
    return REC.pack(*x, *v, rp or 0, case.nzcv or 0, 0, 0, 0, 0, bytes(mem))
