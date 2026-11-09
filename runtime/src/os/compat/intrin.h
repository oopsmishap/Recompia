// SPDX-License-Identifier: GPL-3.0-or-later
// <intrin.h> for the POSIX build: the handful of intrinsics the runtime uses.
//
// YieldProcessor/_mm_pause are the spin hint (kernel/busy_mutex.cpp, kernel/precise_wait.cpp): `yield` on arm64.
// __rdtsc is the cycle counter diag/ftrace.cpp stamps events with: mach_absolute_time, which is the same kind of
// thing (a monotonic tick counter, not a cycle count). __cpuid has no arm64 counterpart and is defined to zero the
// registers, so a caller that probes for an x86 feature sees "no" -- which is the correct answer here.
#pragma once
#include "nx_os.h"

#if defined(__aarch64__) || defined(__arm64__)
static inline void nxos_spin_hint(void) { __asm__ __volatile__("yield" ::: "memory"); }
#else
static inline void nxos_spin_hint(void) { __asm__ __volatile__("pause" ::: "memory"); }
#endif

static inline void YieldProcessor(void) { nxos_spin_hint(); }
static inline void _mm_pause(void) { nxos_spin_hint(); }
static inline void _ReadWriteBarrier(void) { __atomic_signal_fence(__ATOMIC_SEQ_CST); }
static inline void _mm_mfence(void) { __atomic_thread_fence(__ATOMIC_SEQ_CST); }
static inline void _mm_sfence(void) { __atomic_signal_fence(__ATOMIC_SEQ_CST); }
static inline void _mm_lfence(void) { __atomic_signal_fence(__ATOMIC_SEQ_CST); }

static inline unsigned long long __rdtsc(void) {
#if defined(__APPLE__)
    extern unsigned long long mach_absolute_time(void);
    return mach_absolute_time();
#else
    unsigned long long t;
    __asm__ __volatile__("mrs %0, cntvct_el0" : "=r"(t));
    return t;
#endif
}

static inline void __cpuid(int out[4], int leaf) {
    (void)leaf;
    out[0] = out[1] = out[2] = out[3] = 0;
}

static inline void __cpuidex(int out[4], int leaf, int sub) {
    (void)leaf;
    (void)sub;
    out[0] = out[1] = out[2] = out[3] = 0;
}

static inline unsigned char _BitScanForward(unsigned long* index, unsigned long mask) {
    if (!mask) return 0;
    *index = static_cast<unsigned long>(__builtin_ctzl(mask));
    return 1;
}

static inline unsigned char _BitScanReverse(unsigned long* index, unsigned long mask) {
    if (!mask) return 0;
    *index = static_cast<unsigned long>(63 - __builtin_clzl(mask));
    return 1;
}
