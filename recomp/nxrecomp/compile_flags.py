# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile flags of the lifted code: the one table for every build of it.

    cmake/NxRecomp.cmake (developer build, clang-cl)   CLANG_CL_OPT + CLANG_CL     (NX_OPT replaces CLANG_CL_OPT)
    cmake/NxRecomp.cmake with a non-MSVC compiler       CLANG
    game-build/release_b/pkp_build.py (player build)    ZIG_MINGW                   (zig c++, x86_64-windows-gnu)

CMake reads them through `python -m nxrecomp build-info`. The lists are not identical, and the differences are kept as they
were when the tables were merged (the player build and the developer build each produce working objects with their own):
  * optimisation: clang-cl /O1 is clang's -Os (size), the player build uses -O1;
  * the player build adds -fno-threadsafe-statics (clang-cl keeps the MSVC thread-safe local statics);
  * target: the developer build is the MSVC ABI (static CRT /MT from cmake/clang-cl.cmake), the player build mingw;
    -mcx16 and -mcpu=x86_64+cx16 both turn on cmpxchg16b on the x86-64 baseline (added only for x86-64 targets: see for_arch).
Changing ZIG_MINGW changes the players' build stamp and object cache keys (every unit compiles again).
"""

# what the generated code's meaning depends on: guest memory is untyped (no type-based alias analysis), and lifted FP must
# not be fused or reordered (smo-recomp's flags)
import platform

SEMANTIC = ['-fno-strict-aliasing', '-ffp-contract=off']
# The same two flags for the clang-cl build, which does not take the bare spellings: clang-cl 18 answers "unknown argument
# ignored in clang-cl" and compiles on, so the developer build ran with contraction on and no -fno-strict-aliasing. Nothing
# showed while the target had no FMA (there was nothing to contract into), but the flag has to be honoured: any build that
# does target FMA would fuse `a * b + c` into vfmadd and stop matching the player build's rounding. /clang: hands the flag
# to the driver unchanged (checked: `a * b + c` stays vmulss + vaddss, an explicit std::fma still becomes one vfmadd).
SEMANTIC_CL = ['/clang:-fno-strict-aliasing', '/clang:-ffp-contract=off']

# -mcx16 asks for the 128-bit cmpxchg the lifted code's exclusive-monitor emulation needs on x86-64 (docs/GAPS.md). It is an
# x86 option: on AArch64 a 128-bit __atomic_compare_exchange is a CASP instruction pair and clang rejects the flag, so it is
# added only when the lifted code is built for x86-64.
ARCH_FLAGS = {'x86_64': ['-mcx16'], 'amd64': ['-mcx16']}
# -march=x86-64-v3 was tried for the developer build and measured: no gain on the town scene (two builds run interleaved,
# A B B A, over the same six 10 s windows: 170.6 against 171.7 fps mean, while one binary's own two runs spanned
# 169.6..173.8), so it is not here. It would also have been an AVX2 minimum for whatever build took it. A single pair had
# shown +5.4 %; that was background build load, which is why the comparison has to be interleaved (work/fps-notes.md).
# -mcpu=apple-m1 was tried for the arm64 build and measured: 41.5 fps against 42.0 in the same scene, no gain, so it is not here.


def _arch_flags(arch):
    return ARCH_FLAGS.get((arch or '').lower(), [])


# -O1, not /O1: clang-cl maps /O1 to -Os, and the player build has always used -O1 (ZIG_MINGW). So the developer build was
# measuring an object the players never run. The two are inside the scene's run-to-run noise (about +-1 %: 171.3 against
# 172.8 in one pair, 171.6 against 173.4 in another), so this is so that a measurement here means something, not for the fps.
CLANG_CL_OPT = '-O1'
CLANG_CL = ['/w', '/EHs-c-', '/GR-'] + SEMANTIC_CL
# -O1, measured: -O2 on the lifted code is slower (34.9 fps against 42.0 in the same world scene on an M3, interval 28.7 against
# 23.9 ms). The lifted code is two hundred thousand small functions; -O2 makes them bigger and the instruction cache pays for it.
CLANG = ['-O1', '-w', '-fno-exceptions', '-fno-rtti'] + SEMANTIC


def for_arch(arch, kind):
    """The flags of one build of the lifted code: kind 'clang_cl' or 'clang', arch the target processor
    (CMAKE_SYSTEM_PROCESSOR, or the host's when a toolchain file leaves it empty)."""
    base = CLANG_CL if kind == 'clang_cl' else CLANG
    return base + _arch_flags(arch or platform.machine())
# -g0: zig c++ writes debug info unless told not to, and nothing reads it (the player's pkp has no PDB; crash reports name
# lifted functions from the module tables). Measured on one 3 MB unit of main: 6.3 s against 8.8 s to compile, the object
# 0.8 MB against 5.2 MB, the same instructions. It also keeps header line numbers out of the objects.
ZIG_DEBUG = ['-g0']
ZIG_MINGW = (['-target', 'x86_64-windows-gnu', '-mcpu=x86_64+cx16', '-std=c++20', '-O1', '-w', '-fno-exceptions', '-fno-rtti']
             + SEMANTIC + ['-fno-threadsafe-statics'] + ZIG_DEBUG)
# The same object code for a Linux package: the guest code's meaning does not change with the host, only the target triple does
# (x86_64-linux-gnu), and the flags stay identical to the mingw build's so a fix to one is a fix to both.
ZIG_LINUX = (['-target', 'x86_64-linux-gnu', '-mcpu=x86_64+cx16', '-std=c++20', '-O1', '-w', '-fno-exceptions', '-fno-rtti']
             + SEMANTIC + ['-fno-threadsafe-statics'] + ZIG_DEBUG)
# Linux AArch64: targets aarch64-linux-gnu, no -mcpu=x86_64+cx16 (AArch64 CASP handles 128-bit atomics natively).
ZIG_LINUX_ARM64 = (['-target', 'aarch64-linux-gnu', '-std=c++20', '-O1', '-w', '-fno-exceptions', '-fno-rtti']
                   + SEMANTIC + ['-fno-threadsafe-statics'] + ZIG_DEBUG)
