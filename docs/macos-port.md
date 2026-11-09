# macOS support: port plan

Status: **M0–M2 implemented at compile level; M3's lift and generated-code compile verified; the game executable,
the run, and M4–M6 outstanding.** §7 is the evidence log — what is in the tree and how it was checked.
[`../HANDOVER.md`](../HANDOVER.md) is the operational companion: prerequisites, the build, the first three smoke
tests, and the M3 recipe.
Target: a native `arm64` macOS build of `pkp` (Apple Silicon), running the same lifted game libraries the Windows
build links.

This document is written against `main` at `5f98748`. Every file/line reference below was read from that tree; the
counts are `wc -l` on the checked-out sources.

---

## 1. What the port actually is

`nxrecomp` emits plain C++20 against one ABI header (`recomp/include/nxrecomp/ctx.h`). That header is already
portable: 2,090 lines, one x86-only construct (a `PAUSE` hint, guarded by `__x86_64__`), all atomics through
`__atomic_*` builtins, all float behaviour through bit manipulation (`fpexact.h`, `neon.h` have no intrinsics at
all). The recompiler itself is 6,438 lines of Python and has no host-architecture dependency.

So the recompilation pipeline needs **no** port. The work is entirely in `runtime/` (113,090 lines in `src/`,
4,829 in `include/`) plus build and packaging.

Measured coupling to Win32 (`#include <windows.h>`/`<winsock2.h>` or a Win32-only SDK header):

| Area | Files | Lines | What the Win32 dependency is |
|---|---|---|---|
| `runtime/src` + `runtime/games` + `runtime/apps` + `game-build/src` | 80 | 38,520 | 101 distinct Win32 APIs |
| of which, graphics (`src/gfx`) | 21 | 12,000 | SDL3 already; the rest is DXGI HDR query, XAudio2, `Sleep`, `_mm_pause`, `__cpuid` |
| of which, `src/mem` | 2 | 1,435 | `VirtualAlloc`/sections/VEH page faults |
| of which, `src/diag` | 11 | 2,500 | SEH crash handler, `__rdtsc`, registry, DbgHelp |
| of which, `src/net` | 2 | 2,110 | Winsock |
| of which, `src/media` | 1 | 1,539 | Media Foundation |
| of which, `apps/launcher` | 7 | 4,352 | raw Win32 UI (`CreateWindowExW`, `wWinMain`) |

The single most important measurement: **the 101 Win32 APIs are almost all boring portability surface** —
`SRWLOCK`, `CONDITION_VARIABLE`, `CreateThread`, `WaitForSingleObject`, `CreateWaitableTimerExW`,
`QueryPerformanceCounter`, `CreateFileW`/`ReadFile`, `WideCharToMultiByte`, `GetLastError`. Only about ten
things need genuine design work. That is the shape of this plan.

---

## 2. Decisions

### D1 — Target `arm64` only, floor macOS 13 (Ventura)

The guest is AArch64; the host being AArch64 removes a whole class of mismatches and every `-mcx16` /
`cmpxchg16b` / `_mm_pause` / `__rdtsc` / `__cpuid` problem at once (clang lowers `__atomic_compare_exchange_n` on
`unsigned __int128` to `CASP` on Apple Silicon). An `x86_64` macOS build is only interesting as an optional
staging step (see M2a) and is not a deliverable.

Rationale for the floor: MoltenVK 1.4.2 raised its minimum to macOS 12.0, SDL3 requires 10.15+, and Metal 3
(macOS 13) is what the swapchain path wants. Recommend macOS 14+ for the test matrix.

### D2 — Vulkan stays the only graphics API. MoltenVK ≥ 1.4.2 supplies it.

`runtime/src/gfx/vk_device.cpp:365` hard-fails below Vulkan 1.3, and `:481` additionally requires
`dynamicRendering` and `synchronization2`. MoltenVK added Vulkan 1.3 in **v1.3.0** (2025-04-27), Vulkan 1.4 in
v1.4.0, and the current 1.4.x line also adds `VK_EXT_depth_clip_control` (which `vk_device.cpp:583` probes) and
`VK_EXT_external_memory_metal`. So the requirement is met. Writing a Metal backend instead would be a rewrite of
12,000 lines of NVN interception; rejected.

Concrete change: `runtime/cmake/gfx_deps.cmake:68` currently pins `VK_USE_PLATFORM_WIN32_KHR` on `nxrt_volk`. On
macOS it becomes `VK_USE_PLATFORM_METAL_EXT` (SDL3's `SDL_Vulkan_GetInstanceExtensions` reports
`VK_EXT_metal_surface`), and the MoltenVK ICD + `libvulkan.1.dylib` are bundled in the app.

### D3 — `[nvn] pool_copy = on` is mandatory on macOS, and `auto_copy()` must say so

`VK_EXT_external_memory_host` does not exist on MoltenVK (it is not in any MoltenVK release note; the Metal-shaped
equivalent is `VK_EXT_external_memory_metal`, which imports `MTLBuffer`, not raw host pointers).
`runtime/src/gfx/nvn/nvn_gpu_pool.cpp:296` already refuses to import in that case and the whole shadow-copy
implementation exists (`nvn_pool_copy.cpp`, 649 lines; `[nvn] pool_copy = auto|on|off`).

The bug to fix: `auto_copy()` at `runtime/src/gfx/nvn/nvn_pool_copy.cpp:71-88` turns the copy path on only for
Wine or an *integrated* GPU with ≤ 8 GiB device-local memory. MoltenVK reports
`VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU` with a unified heap the size of system RAM, so on a 24 GB Mac `auto`
picks **off** and every pool import fails. Add a platform term: on macOS, `auto` ⇒ `true`.

Cost of the copy path: every guest→GPU buffer range is a host-visible shadow, copied on first touch per serial and
written back before a serial is published (`nvn_gpu.cpp:212-214`). That is the same path Steam Deck / Wine users
already run, so it is exercised, not speculative.

### D4 — Pass ASTC through instead of transcoding to BC7

`runtime/src/gfx/nvn/nvn_texdata.cpp` decodes ASTC and re-encodes to `VK_FORMAT_BC7_*` via `tools/textures`
(`astc2bc7` + `bc7e.ispc`, MSVC `.lib`s, ISPC, plus `astcdec-*-shared.dll` loaded at run time —
`runtime/cmake/gfx.cmake:21-28`). Apple GPUs support ASTC natively, so on macOS the right move is
`VK_FORMAT_ASTC_*_BLOCK` straight from the tiled bytes.

This deletes three build dependencies (ISPC, bc7enc_rdo, the MSVC static libs), the runtime DLL loading, the
`bc7_cache` directory, and a per-texture CPU transcode. Keep the `astc2bc7` path compiled on Windows only.

### D5 — Guest threads are pthreads; guest stacks stay emulated

`recomp/include/nxrecomp/ctx.h:47` keeps `x[31]` (SP) **inside** `Ctx`, and `runtime/src/kernel/thread.cpp:115`
sets `t->ctx.x[31] = p.stack_top` — the guest stack at `mem::kStackBase` (`0x7C00000000`) is ordinary mapped
memory that the lifted code addresses explicitly. The host thread's own stack is only used by
`host_thread_main`.

Consequence: **no host stack switching, no `pthread_attr_setstack`, no ucontext trampoline.** `CreateThread` →
`pthread_create` with a default stack, and the `[threads] host_stack_mb = 64` knob becomes a no-op (or a
`pthread_attr_setstacksize`). This was the single biggest suspected blocker and it is not one.

### D6 — A POSIX shim under `runtime/src/os/`, not 80 rewrites

The 101 APIs collapse into ~40 shim functions. Implementing them once (header-only `nx_os.h` plus
`os_posix.cpp`) lets `os/sync.cpp` (595 lines of `SRWLOCK`/`CONDITION_VARIABLE`),
`kernel/kernel_sync.cpp`, `kernel/sched.cpp`, `kernel/precise_wait.cpp`, `diag/*`, `services/*` and
`net/*` compile with only an `#include` change.

The alternative — rewriting every call site — touches 38,520 lines and puts the working Windows build at risk on
every step. Rejected.

The shim provides, on POSIX: `SRWLOCK`, `CONDITION_VARIABLE`, `CRITICAL_SECTION`, `HANDLE` (a fd/`sem_t`/pointer
tagged union), `CreateEventW`, `CreateMutexW`, `WaitForSingleObject`, `SetEvent`/`ResetEvent`,
`CreateWaitableTimerExW`/`SetWaitableTimer`/`CancelWaitableTimer` (→ `kqueue` `EVFILT_TIMER` or
`timerfd`-equivalent via `dispatch_source`), `CreateThread`/`ResumeThread`/`ExitThread`, `Sleep`,
`QueryPerformanceCounter`/`Frequency` (→ `mach_absolute_time`), `GetTickCount64`, `GetLastError`/`SetLastError`
(→ `errno` thread-local), `CreateFileW`/`ReadFile`/`WriteFile`/`SetFilePointerEx`/`GetFileSizeEx` (→ `open`/`pread`,
UTF-16 → UTF-8 at the boundary), `WideCharToMultiByte`/`MultiByteToWideChar` (→
`CFStringConvert`/`iconv`-free hand-rolled UTF-8↔UTF-16), `GetModuleFileNameA` (→ `_NSGetExecutablePath`),
`LoadLibraryW`/`GetProcAddress`/`FreeLibrary` (→ `dlopen`/`dlsym`/`dlclose`), `GetSystemInfo` (→ `sysctl`),
`VirtualAlloc`-family (thin wrappers over `mem::`, see D7).

Everything the shim cannot honestly provide (`SetThreadAffinityMask`, `GetCurrentProcessorNumber`,
`SetThreadPriorityBoost`, `timeBeginPeriod`) gets a no-op or a documented degradation; see R3.

### D7 — `mem::` moves to `mmap` + `mach_vm`, and guest pages stop being 4 KiB on the host

`runtime/src/mem/memory.cpp` (768 lines) is `VirtualAlloc`-based end to end, plus one
`CreateFileMappingW`+`MapViewOfFile` section for the heap (`:479-500`). On macOS:

- `reserve_fixed` → `mmap(PROT_NONE, MAP_PRIVATE|MAP_ANON)` with the address as a *hint*, then verify the
  returned pointer equals the request and fail cleanly if not (`MAP_FIXED` clobbers silently, and
  `MAP_FIXED_NOREPLACE` does not exist). The layout constants in `runtime/include/nxrt/memory.h:14-28`
  (`kMainBase 0x7100000000` … `kKernelBase 0x7E00000000`) all sit inside the 47-bit arm64 user VA space and
  clear of the dyld shared cache, so they are reusable — verify empirically in M1.
- `commit`/`decommit` → `mprotect(PROT_READ|PROT_WRITE)` / `madvise(MADV_FREE_REUSABLE)`.
- `heap_create` → `mmap` a file-backed object (`shm_open` or a temp file) so `heap_alias`/`heap_private_view`
  can `mmap` the same object at several addresses. `heap_has_section()` stays true, which keeps
  `heap_private_view` (the fast GPU-import path, `memory.h:74`) working.
- **Page size.** `kPage = 0x1000` (`memory.h:29`) is guest semantics and must stay 4 KiB. The host page on
  Apple Silicon is **16 KiB**. Every host-facing call therefore needs a host-page rounding
  (`host_page = sysconf(_SC_PAGESIZE)`), and `heap_alias(offset)` with a 4 KiB offset must round down to 16 KiB —
  which changes the meaning of "the alias starts exactly here". Introduce `host_align()` and keep the guest page
  table at 4 KiB (it already is, `g_commit_bits`/`g_heap_cls` index by `kPage`).
- `Prot::RX`/`Prot::RWX` → `PROT_READ` / `PROT_READ|PROT_WRITE`. Guest code is **never executed** — the only
  caller is the ELF segment mapper at `runtime/src/loader/module.cpp:520`, and execution goes through the lifted
  C++. Keeping exec bits off keeps the whole build clear of `MAP_JIT`, `allow-jit`, and W^X entitlement questions.

### D8 — `SIGSEGV` for the two fault-driven features (revised: was "mach exception ports")

> **Revised during implementation.** This section first chose mach exception ports. A BSD signal handler is the
> better fit and is what `src/os/os_posix.cpp` now installs: it runs on the *faulting* thread and returning from it
> resumes the faulting instruction, which is exactly what `write_watch.cpp` needs (un-protect the page, return, the
> store retries). A mach port hands the fault to a helper thread, which then has to find the faulting thread, call
> `thread_get_state`, decide, `thread_set_state` and reply `KERN_SUCCESS` — more moving parts for the same result,
> and the `sigreturn` concern below turned out not to apply: no `sigreturn` is involved when the handler simply
> returns. The `CONTEXT` is built from `ucontext_t` (`uc_mcontext->__ss` on arm64), so a crash report carries the
> real pc/lr/sp/fp, and `RaiseException` runs the same handler list in-process. What still needs the mach work is
> `GetThreadContext`/`SuspendThread` on *another* thread (the diag sampler and the starvation guard), which the shim
> reports as unsupported rather than faking.

`runtime/src/mem/write_watch.cpp` (667 lines) turns guest pages read-only and services the resulting fault to
implement per-page write tracking (`on_fault` at `:269`, a vectored exception handler). `runtime/src/diag/crash.cpp`
(344 lines) is the crash reporter. Both need the same primitive: *see the fault, fix the state, resume the
thread*, and `AddVectoredExceptionHandler`/`RaiseException`/`SetUnhandledExceptionFilter` in the shim give it to
them over `sigaction(SIGSEGV | SIGBUS | SIGILL | SIGFPE | SIGTRAP)`.

`write_watch` is load-bearing: `nvn_gpu_texture.cpp`, `nvn_gpu_pool.cpp`, `nvn_pool_copy.cpp`,
`services/fs_romfs.cpp`, `services/fs_volume.cpp` all consult it. Plan for it to be **deferred, not skipped**:
M5. Until then, a compile-time `NX_WW_POLL` fallback that arms nothing and reports "everything may have been
written" keeps the renderer correct at a cost in upload bandwidth — useful for bringing the game up in M3/M4
before the exception server exists.

### D9 — Audio through a CoreAudio `AudioSink`

`runtime/include/nxrt/audio_sink.h` is a clean 5-method interface and `runtime/src/services/audio_standin.h`
already provides a clock-only sink for tests. `audio_xaudio2.cpp` (611 lines) becomes `audio_coreaudio.cpp`
(AVAudioEngine or a raw `AudioUnit`/`AudioQueue` output unit with the same non-blocking `write()` contract, the
same underrun accounting, and the same device-loss → stand-in → retry ladder). Selectable via `[audio] sink`.

### D10 — Video through AVFoundation / VideoToolbox

`runtime/src/media/movie_mf.cpp` (1,539 lines) is Media Foundation; the `movie::` object layout it implements is
Nintendo's, not Microsoft's, so the protocol layer survives and only the demux/decode backend changes
(`AVAssetReader` + `VTDecompressionSession`, or the already-present libvpx path for VP8/9 —
`runtime/cmake/media_deps.cmake`).

### D11 — Launcher: use the Tauri one; leave `apps/launcher` to the end

`runtime/apps/launcher` is a raw Win32 UI (`wWinMain` at `main.cpp:803`, `CreateWindowExW` at `:748`, 4,352
lines). `launcher/` is Tauri 2 (12,626 lines of Rust, 5,206 of JS) and is already cross-platform. For macOS,
M1–M5 run from a hand-written `pkp.ini` and the CLI; the launcher comes last, and the cheapest correct answer is
to make the Tauri app drive the macOS flow rather than port 4,352 lines of Win32.

### D12 — Keep Windows green

Every milestone is additive: `#ifdef`s live in `runtime/src/os/` and the CMake presets, not scattered through the
call sites. `runtime/CMakeLists.txt:18` (`message(FATAL_ERROR "nxrt targets native Windows x64 only")`) becomes a
platform branch, not a deletion. The existing `.github/workflows/release.yml` stays untouched; a new
`macos.yml` builds the macOS artifact.

---

## 3. Milestones

Each milestone has a hard acceptance criterion. Nothing advances on "it compiles".

### M0 — Build skeleton (no game, no window)

Work:

1. `runtime/CMakeLists.txt` — replace the `if(NOT WIN32) FATAL_ERROR` with `if(WIN32) … elseif(APPLE) … else()
   FATAL_ERROR`. Guard the MSVC-runtime block, `NOMINMAX`/`_WIN32_WINNT` defines, `/W3` and `psapi` behind `WIN32`.
2. New `runtime/CMakePresets.json` preset `macos-arm64` (Ninja, `CMAKE_OSX_ARCHITECTURES=arm64`,
   `CMAKE_OSX_DEPLOYMENT_TARGET=13.0`, `-O2`, `-fno-strict-aliasing -ffp-contract=off`).
3. `runtime/src/os/nx_os.h` + `os_posix.cpp` — the shim from D6, growing as files are ported.
4. `runtime/cmake/gfx_deps.cmake` — `NXRT_DEPS_DIR` defaults to `$HOME/Library/Caches/nxrt/deps` (currently
   `$ENV{LOCALAPPDATA}`), `VK_USE_PLATFORM_METAL_EXT` on macOS, and MoltenVK as a `FetchContent` pin
   (`MoltenVK-macos.tar`, ≥ 1.4.2).
5. `recomp/cmake/NxRecomp.cmake` — drop `-mcx16` on non-x86 (`:99-102`: both the MSVC and the non-MSVC branch hard-code
   it; make it conditional on `CMAKE_SYSTEM_PROCESSOR` matching `x86_64|AMD64`). `game-build/release_b/pkp_build.py:65,529` needs a `-target arm64-apple-macos` variant.

Acceptance: `cmake --preset macos-arm64 && cmake --build` produces `libnxrt.a` and `libnxrt_gfx.a` on an M-series
Mac with the same warning set as Windows. No behaviour yet.

### M1 — Memory, kernel, timers (headless)

Work:

1. `runtime/src/mem/memory.cpp` — the D7 rewrite: `reserve_fixed`/`release_fixed`/`commit`/`decommit`/`protect`/
   `is_mapped`/`heap_*` over `mmap`/`mprotect`/`madvise`/`mach_vm`, with the 4 KiB guest / 16 KiB host split.
2. `runtime/src/kernel/svc_memory.cpp` (822 lines) — `VirtualAlloc`/`VirtualFree`/`VirtualQuery` call sites move to
   `mem::` (most already should be; the four `VirtualAlloc` uses are the private allocations).
3. `runtime/src/kernel/thread.cpp` — `CreateThread`→`pthread_create`, `DuplicateHandle`→`pthread_t`,
   `ExitThread`→`pthread_exit`, `CloseHandle` on the thread handle dropped.
4. `runtime/src/kernel/{time,precise_wait,busy_mutex,starvation_guard,sched}.cpp` — the shim's waitable timers and
   `mach_absolute_time`; `sched.cpp`'s affinity/priority calls (`:289-335`) degrade to `thread_policy_set`
   `THREAD_AFFINITY_POLICY` (a hint on macOS, and it cannot split performance/efficiency cores) or to nothing.
5. `runtime/src/os/sync.cpp`, `kernel/kernel_sync.cpp` — compile through the shim unchanged.
6. `runtime/src/diag/{log,registry,sampler,host_probe,watchdog,gauge}.cpp` — `GetLastError`→shim, registry reads
   (`host_device.cpp` SMBIOS at `:36-40`) → `sysctl hw.model` / `IOKit`.

Acceptance: the `nxrt` self-tests and `runtime/src/testtools` (`tt_runner`) build and run headless; the memory
unit tests (fixed reservations at `0x7100000000`, heap commit/decommit/alias, `vm_query`) pass. Guest threads
create/join and `svcSleepThread` hits its target within 1 ms.

### M2 — Graphics on MoltenVK

Work:

1. `runtime/src/gfx/platform.cpp` — `native_hwnd()` (`:446`) → `native_nsview()` reading
   `SDL_PROP_WINDOW_COCOA_WINDOW_POINTER`; `set_fullscreen` (`FullscreenMode::Exclusive`) maps to
   borderless-fullscreen on macOS (there is no exclusive mode outside a `CGDisplayCapture` hack, which is not
   worth it); `vulkan_instance_extensions`/`create_vulkan_surface` already delegate to SDL, so they work once
   `VK_USE_PLATFORM_METAL_EXT` is set.
2. `runtime/src/gfx/vk_device.cpp` — expect `external_memory_host == false`, `custom_border_color == false`,
   `push_descriptor == false` (all three are already probed and degrade: `:604`, `:662`, `:606`). Log the
   degradations once, at `INFO`. Add a `VK_MVK_moltenvk` config hook so `MVK_CONFIG_*` can be set from `pkp.ini`.
3. `runtime/src/gfx/nvn/nvn_pool_copy.cpp:71-88` — macOS ⇒ `auto_copy() == true` (D3).
4. `runtime/src/gfx/nvn/nvn_texdata.cpp` — the ASTC passthrough branch (D4), replacing the
   `encode_bc7_banded` path at `:367`.
5. `runtime/src/gfx/extras/win_display.cpp` — either an `IOKit`/`CGDisplay` HDR query or, for M2,
   `query_display_hdr` returning `valid = false` and HDR off. Do not pretend.
6. `runtime/src/gfx/extras/temporal_dlss.cpp`, `temporal_ffx.cpp` — compile out on macOS (Streamline and FFX are
   Windows/Linux; both already runtime-load and fall back). FSR1 (`fsr1_spatial.cpp`) and NIS
   (`nis_spatial.cpp`) are plain compute shaders and stay.
7. `runtime/src/gfx/frame_limiter.cpp:9,105` — `_mm_pause` → `__builtin_arm_yield()` / `asm volatile("yield")`.
8. `runtime/src/gfx/nvn/nvn_shaders.cpp:58-62` — `__cpuid`/`__cpuidex` (CPU feature detection for shader
   preload) → `sysctlbyname("hw.optional.arm.FEAT_*")` or a `#if defined(__aarch64__)` no-op.

Acceptance: `nxrt_gfx_demo` opens a window through SDL3 + MoltenVK, renders its scene, and shows the ImGui F1
overlay. Frame pacing is stable and the log shows the three feature degradations exactly once.

**M2a (optional, cheap de-risk).** Build `x86_64` and run under Rosetta 2. It keeps `cmpxchg16b`, `_mm_pause`,
`__rdtsc` and `__cpuid` working, so it isolates *graphics-stack* problems from *arm64* problems. Not a
deliverable, but a good way to unblock M2 if a MoltenVK issue is ambiguous.

### M3 — Game bring-up

Work:

1. `recomp/scripts/build_game.cmd` equivalent for macOS: lift the four NSOs (`recomp/games/pkp/config.py`
   modules: main `0x7100000000`, sdk `0x7500000000`, subsdk0 `0x7600000000`, rtld `0x7700000000`) with the local
   Python and compile the shards with Apple clang.
2. `game-build/CMakeLists.txt` — the imported `.lib` block becomes platform-conditional (`.a` on macOS); drop
   `/MAP /STACK:134217728 /HIGHENTROPYVA:NO` and `-clang:-fmacro-prefix-map` (use
   `-ffile-prefix-map` directly); the `DejaVuSans.ttf` and `astcdec-*.dll` copies go (D4).
3. `game-build/src/pkp_main.cpp` (542 lines) — the entry point is already `int main(int argc, char** argv)`
   (`:317`), so only the two `MessageBoxA` calls (`:113`, `:309`) need an `NSAlert`/`fprintf(stderr)` path, plus the
   `PKP_GUI_SUBSYSTEM` linker flag (`game-build/CMakeLists.txt:84-85`, `/SUBSYSTEM:WINDOWS /ENTRY:mainCRTStartup`)
   which has no macOS equivalent: on macOS the process is a normal executable inside the `.app`, and "no console
   window" comes from the bundle, not the linker.
4. `runtime/src/services/fs_romfs.cpp:537-564` and `fs_volume.cpp` — `CreateFileW`/`ReadFile` with
   `FILE_SHARE_*` and overlapped reads → `open(O_RDONLY)`/`pread` through the shim (the `mem::ww::OsWriteScope`
   around `:556` stays; it is what keeps the write tracker honest).
5. `runtime/src/services/{time,timezone,pl,acc,album,misc,common,logfault}.cpp` — `GetSystemTimePreciseAsFileTime`
   → `clock_gettime(CLOCK_REALTIME)`, `GetDynamicTimeZoneInformation` → `CFTimeZone`/`TZ`, `pl` power state →
   `IOPMrootDomain`, album/screenshot → `NSImage`/`CGImage` write.

Acceptance: the game boots past the title screen and reaches gameplay. This is the milestone that proves D3,
D4, D5 and D7 together; expect it to be where the surprises are.

### M4 — Platform services

Work:

1. `runtime/src/services/audio_xaudio2.cpp` → `audio_coreaudio.cpp` (D9), plus
   `runtime/src/services/audio_devices.cpp` (90 lines, device enumeration) → CoreAudio device list.
2. `runtime/games/pkp/pkp_kbm.cpp` (777 lines) — keyboard/mouse mapping; it uses `windows.h` for
   `GetKeyboardLayout`/`MapVirtualKey`/`ToUnicode`, all of which become SDL3 scancode/keycode data
   (SDL3 is already linked and already delivers the events via `src/gfx/input.cpp`).
3. `runtime/games/pkp/pkp_save_uuid.cpp` (242 lines) — filesystem only; through the shim.
4. `runtime/src/net/relay_server.cpp` (1,095) and `lobby.cpp` (1,015) — Winsock → BSD sockets. Mostly
   `closesocket`→`close`, `WSAGetLastError`→`errno`, `ioctlsocket(FIONBIO)`→`fcntl(O_NONBLOCK)`,
   `SIO_UDP_CONNRESET` dropped, `WSAStartup` dropped.
5. `runtime/src/timestep/timestep.cpp`, `runtime/src/testtools/*` — `QueryPerformanceCounter` → shim.

Acceptance: audio plays through the built-in speakers and a USB interface; keyboard/mouse and a Bluetooth
controller drive the game; Link Play connects to a `pokopia-native` peer; the test-tools runner replays a script.

### M5 — Fault-driven features and diagnostics

Work:

1. `runtime/src/os/mach_fault.cpp` — the exception-port server from D8, with a clean `fault_register(range,
   handler)` API that `write_watch` and `crash` both use.
2. `runtime/src/mem/write_watch.cpp` — replace `on_fault` (`:269`) with a registration on the server; replace
   `VirtualProtect` with `mprotect`; the `releasing`/`wsum` bookkeeping is architecture-independent and stays.
3. `runtime/src/diag/crash.cpp` — rewrite against the server: `thread_get_state` for registers, `dladdr` +
   `_Unwind_Backtrace` for stacks, `VM_REGION` walk for the memory map. `runtime/src/diag/symbols.cpp` (371
   lines) — DbgHelp/PDB → Mach-O `LC_SYMTAB` reading, or `atos`/`dSYM` on demand.
4. `runtime/src/diag/ftrace.cpp:113-245` — `__rdtsc` → `mach_absolute_time` (or `cntvct_el0`); the TSC/HPET
   calibration at `:115` disappears.
5. `runtime/src/eh/unwind.cpp:25` — `windows.h` is included only for `TerminateProcess`
   (`:558`, the uncaught-exception path) → `abort()`/`_exit()`. The unwinder itself is already guest-state-only.

Acceptance: the write tracker's unit tests pass; a deliberately crashing build produces a report with a symbolised
native stack; `[diag] ftrace` timings are sane.

### M6 — Media, launcher, packaging

Work:

1. `runtime/src/media/movie_mf.cpp` → `movie_avf.cpp` (D10).
2. `launcher/` — macOS build of the Tauri app: replace the `pkp.exe`/`vulkan-1.dll`/`.bat` assumptions, point the
   shader-cache pipeline at the bundled `spirv_harness`, keep the disk-space and Vulkan checks
   (`launcher/README.md` steps 1–3).
3. Packaging: `.app` bundle (`Contents/MacOS/pkp`, `Contents/Frameworks/{libvulkan.1.dylib,MoltenVK_icd.json,
   libSDL3.dylib}`, `Contents/Resources/{fonts,assets,shaders}`), `Info.plist` with
   `NSHighResolutionCapable`, `LSMinimumSystemVersion 13.0`, `CFBundleDocumentTypes` none.
4. Signing: `codesign --options runtime` + `notarytool`. Hardened runtime needs
   `com.apple.security.cs.disable-library-validation` **only if** MoltenVK/SDL are signed by a different Team ID —
   sign them in the same pass and the entitlement is unnecessary. No `allow-jit` and no
   `allow-unsigned-executable-memory` are needed, per D7.
5. `game-build/release_b/pkp_build.py` — a `--target arm64-apple-macos` path using the system clang (or a pinned
   LLVM tarball), same content-addressed `ObjCache`, same `@progress`/`@error` protocol.
6. CI: `.github/workflows/macos.yml` on `macos-14` (arm64), build + `nxrt_gfx_demo` smoke test + `.app` artifact.

Acceptance: a signed, notarized `.app` that a second Mac opens with a Gatekeeper double-click and plays the game
with its own dump.

---

## 4. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | The address layout (`kMainBase 0x7100000000` … `kKernelBase 0x7E00000000`, `memory.h:14-28`) collides with the dyld shared cache or the arm64 main-thread stack on some macOS version | Medium | High | M1 measures it on 13/14/15 and on both 16 GB and 32 GB machines; make the constants a runtime-searched window if needed (`mach_vm_region` walk), keeping the Windows values as the default |
| R2 | 16 KiB host pages break `heap_alias` at 4 KiB granularity, and something depends on alias identity | Medium | High | D7's `host_align()`; add an assertion that every alias request is host-aligned and that `heap_offset_of` still resolves; the NVN pool chunker already works in 64 KiB units (`kAllocGranularity`) |
| R3 | macOS gives no core pinning and no priority boost, so the console-core model in `kernel/sched.cpp` degrades; audio/render jitter follows | High | Medium | Accept: this is exactly what the Linux/Proton path already does. Add a `[threads] core_model = off` documented mode and measure frame-time variance against the Windows build |
| R4 | `pool_copy` (the only viable pool path, D3) is slower than import; on unified memory the copy is pure overhead | High | Medium | It is already the Wine path; measure. Long term, `VK_EXT_external_memory_metal` (MoltenVK 1.3+) can wrap an `MTLBuffer` — but the guest heap would have to *be* an `MTLBuffer`, which is a bigger change. Defer |
| R5 | MoltenVK gaps the NVN emulation leans on (`VK_EXT_custom_border_color` absent, no `VK_EXT_external_memory_host`, argument-buffer limits) | Medium | Medium | All are probed and degrade today; the visible cost is a few sampler border colours. Budget one pass of visual diffing against Windows (the repo already has `tools/test/imgdiff.py` and `tools/test/timelines/*.nxs`) |
| R6 | Mach exception ports interfere with the Swift/ObjC runtime or the debugger | Low | High | Isolate in one file behind `fault_register`; make it opt-out (`[diag] fault_server = off`) so a crash under lldb stays debuggable |
| R7 | Rosetta-free arm64 changes FP results enough to break the `recomp` differential oracle | Low | Medium | The oracle runs on Unicorn, not the host — it is unaffected. Only `fpexact.h`'s "host result when not NaN" shortcut (`:179`) depends on host FP; arm64 and x86-64 are both IEEE-754 with the same results here, but re-run the KA suite on arm64 as a check |
| R8 | Codesigning/notarization friction with a bundled Vulkan ICD and a runtime-loaded dylib | Medium | Medium | M6 explicitly; test `codesign --verify --deep --strict` early, and keep an unsigned local build for development |
| R9 | The Windows build regresses from shim-related header changes | Medium | High | D12: CI builds both on every push from M0 onward, and the shim is additive |

---

## 5. Ordering and cost

| Milestone | Depends on | Rough size |
|---|---|---|
| M0 build skeleton | — | small: 5 files, new preset, the shim header |
| M1 memory + kernel | M0 | large: `memory.cpp` (768) + `svc_memory.cpp` (822) + 6 kernel files |
| M2 graphics | M1 | medium: 8 files, mostly feature degradation; the ASTC branch is the only new logic |
| M3 game bring-up | M1, M2 | medium: build glue + 8 service files; the risk is in the debugging, not the writing |
| M4 services | M3 | large: audio (new, ~600), net (~2,100 through a shim), input (~800) |
| M5 faults + diag | M1 | large: the Mach server (new) + `write_watch` (667) + `crash` (344) + `symbols` (371) |
| M6 media + packaging | M4 | large: `movie_avf` (new, ~1,500) + Tauri + signing |

M1–M3 is the critical path; everything after M3 is a feature-completeness question rather than a "will it work".

### The first five commits

Ordered so each one is reviewable and leaves both platforms building.

1. `build: make runtime/CMakeLists.txt platform-aware` — `runtime/CMakeLists.txt:17-18` becomes a branch;
   `WIN32`-only defines, the MSVC runtime block, `/W3`, `psapi` and the `launcher.rc`/`/MANIFEST:NO` block
   (`:61-69`) move inside it. Add `runtime/CMakePresets.json`. No behaviour change on Windows.
2. `build: add runtime/src/os/nx_os.h, the POSIX shim header` — types and inline functions only, no call-site
   edits. `runtime/src/os/` exists but is not in any target yet.
3. `build: os_posix.cpp behind APPLE, port diag/log.cpp and os/sync.cpp` — the first two real consumers; proves
   the shim covers `SRWLOCK`/`CONDITION_VARIABLE`/`GetLastError`/file IO.
4. `mem: split guest page (4 KiB) from host page` — the `host_align()` change plus `mmap`-based
   `reserve_fixed`/`commit`/`decommit`/`protect` behind `APPLE`, with the Windows path untouched. Ship with a unit
   test that reserves `0x7100000000` and round-trips commit/decommit/alias.
5. `gfx: VK_USE_PLATFORM_METAL_EXT and a MoltenVK FetchContent pin` — `runtime/cmake/gfx_deps.cmake:68`, plus the
   macOS term in `auto_copy()`. First commit after which `nxrt_gfx_demo` has a chance of showing a window.

---

## 6. Appendix: Win32 → POSIX mapping

Counts are files that reference the API (of the 80 coupled files).

| Win32 | Uses | macOS |
|---|---|---|
| `GetCurrentProcess`, `GetCurrentProcessId` | 18, 5 | `getpid()` / no-op |
| `Sleep` | 15 | `nanosleep` |
| `TerminateProcess` | 13 | `_exit` / `abort` (shim) |
| `GetCurrentThreadId`, `GetCurrentThread` | 13, 11 | `pthread_mach_thread_np` / `pthread_self` |
| `GetProcAddress`, `LoadLibraryW`, `FreeLibrary` | 11, 2, 1 | `dlsym`, `dlopen`, `dlclose` |
| `GetLastError` | 11 | thread-local `errno` |
| `QueryPerformanceCounter`/`Frequency` | 10, 9 | `mach_absolute_time`, `mach_timebase_info` |
| `WideCharToMultiByte`, `MultiByteToWideChar` | 10, 6 | hand-rolled UTF-8↔UTF-16 (no ICU dependency) |
| `WaitForSingleObject`, `WaitForMultipleObjects` | 9, 1 | `sem_wait`/`pthread_cond_wait`/`kevent` |
| `GetModuleFileNameA/W` | 9, 4 | `_NSGetExecutablePath` |
| `SetThreadPriority`, `GetThreadPriority` | 9, 3 | `pthread_setschedparam` (best effort) |
| `VirtualQuery` | 7 | `mach_vm_region` |
| `AcquireSRWLock*`/`ReleaseSRWLock*` | 5+5+4+4 | `pthread_rwlock_*` |
| `CoInitializeEx`, `CoUninitialize`, `CoCreateInstance` | 5, 5, 3 | delete (COM is only used by Media Foundation and DXGI) |
| `CreateThread`, `ResumeThread`, `SuspendThread`, `ExitThread` | 4, 3, 2, 1 | `pthread_create`, no-op, `pthread_kill(SIGSTOP)` if needed, `pthread_exit` |
| `CreateWaitableTimerExW`, `SetWaitableTimer`, `CancelWaitableTimer` | 4, 3, 1 | `kqueue` `EVFILT_TIMER` or `dispatch_source` |
| `ReadFile`, `WriteFile`, `CreateFileW`, `SetFilePointerEx`, `GetFileSizeEx` | 4, 3, 3, 2, 2 | `pread`, `pwrite`, `open`, `lseek`, `fstat` |
| `GetTickCount64` | 4 | `clock_gettime(CLOCK_MONOTONIC)` |
| `VirtualAlloc`, `VirtualFree`, `VirtualProtect` | 3, 3, 3 | D7 |
| `timeBeginPeriod`/`timeEndPeriod` | 3, 1 | no-op (macOS has no timer resolution knob) |
| `SwitchToThread` | 3 | `sched_yield` |
| `CreateFileMappingW`, `MapViewOfFile`, `UnmapViewOfFile` | 1, 2, 1 | D7 (`mmap` of one file object at N addresses) |
| `SleepConditionVariableSRW`, `WakeConditionVariable`, `WakeAllConditionVariable`, `InitializeConditionVariable` | 2, 2, 1, 1 | `pthread_cond_t` |
| `SetThreadAffinityMask`, `GetCurrentProcessorNumber(Ex)` | 2, 2+1 | `thread_policy_set(THREAD_AFFINITY_POLICY)` hint, else no-op (R3) |
| `SetProcessInformation`, `SetPriorityClass`, `GetPriorityClass` | 1, 2, 1 | `setpriority` on the process, best effort |
| `RegGetValueA` | 2 | `sysctl` / `IOKit` (`host_device.cpp`) |
| `GetSystemInfo`, `GetLogicalProcessorInformationEx` | 1, 1 | `sysctlbyname("hw.ncpu")`, `hw.perflevel*` |
| `MessageBoxA/W` | 3 | `NSAlert` (a small ObjC++ file) or `fprintf(stderr)` |
| `CommandLineToArgvW`, `GetCommandLineW` | 2, 1 | `argv` from `main` |
| `MonitorFromWindow`, `GetMonitorInfoW`, `GetWindowRect`, `SetWindowPos`, `ShowWindow`, `SetForegroundWindow` | 1 each | SDL3 (`SDL_GetDisplayForWindow`, `SDL_SetWindowPosition`, `SDL_RaiseWindow`) |
| `LoadCursorW`, `GetDynamicTimeZoneInformation`, `GetStdHandle`, `AttachConsole` | 1 each | SDL3 cursor, `CFTimeZone`, stdout, dropped |
| `FindFirstFileExW`/`FindNextFileW`, `CreateDirectoryA`, `MoveFileExW`, `DeleteFile*`, `CopyFileA`, `GetFullPathName*` | 1–3 each | `opendir`, `mkdir`, `rename`, `unlink`, `copyfile`, `realpath` |

`winsock2`/`ws2tcpip` (3+3 files): `WSAGetLastError`→`errno`, `closesocket`→`close`, `ioctlsocket`→`fcntl`,
`WSAStartup`/`WSACleanup`→dropped, `SIO_UDP_CONNRESET`→dropped, `SOCKET`→`int`.

`dxgi1_6`/`wingdi` (1 file): dropped with `win_display.cpp`.
`xaudio2`/`mmreg`/`objbase` (1 file): dropped with `audio_xaudio2.cpp`.
`mfapi`/`mfidl`/`mfreadwrite`/`mferror` (1 file): dropped with `movie_mf.cpp`.
`dbghelp` (1 file): Mach-O symbol reader.
`intrin.h` (3 files), `immintrin`/`emmintrin` (2 files): `__rdtsc`→`mach_absolute_time`, `__cpuid`→`sysctl`,
`_mm_pause`→`yield`, `_mm_*` in `frame_limiter.cpp` → dropped.

## 7. Implementation status

What is in the tree now, and what the evidence for it is. "Compiles" means it was checked, not assumed.

### M0 — done

| Piece | Where |
|---|---|
| Platform branch (`WIN32` / `APPLE` / else `FATAL_ERROR`), arm64 guard, `CMAKE_OSX_DEPLOYMENT_TARGET` 13.0, MSVC-runtime and `NOMINMAX`/`psapi`/`/W3` behind `WIN32`, `os_posix*.cpp` excluded on Windows and the Windows-only SDK backends excluded on macOS | `runtime/CMakeLists.txt` |
| `macos-arm64` / `macos-arm64-debug` presets (Ninja, arm64, `Darwin`-conditioned) | `runtime/CMakePresets.json` |
| The shim: 380 Win32 names, ~200 implemented | `runtime/src/os/nx_os.h`, `nx_os_internal.h`, `os_posix.cpp`, `os_posix_file.cpp`, `os_posix_mem.cpp`, `os_posix_net.cpp`, `os_posix_misc.cpp` |
| `<windows.h>`, `<psapi.h>`, `<tlhelp32.h>`, `<dbghelp.h>`, `<bcrypt.h>`, `<intrin.h>`, `<emmintrin.h>`, `<immintrin.h>`, `<winsock2.h>`, `<ws2tcpip.h>`, `<mstcpip.h>`, `<iphlpapi.h>`, `<objbase.h>`, `<shlobj.h>`, `<shellapi.h>`, `<mmreg.h>`, `<avrt.h>`, `<mmsystem.h>` resolve to the shim on non-Windows only | `runtime/src/os/compat/` |
| `NXRT_DEPS_DIR` = `~/Library/Caches/nxrt/deps`, `VK_USE_PLATFORM_METAL_EXT`, MoltenVK located from `$VULKAN_SDK`/`NXRT_MOLTENVK_DIR` and copied next to each executable as `libvulkan.1.dylib` (volk's `dlopen` name on Apple) | `runtime/cmake/gfx_deps.cmake`, `runtime/cmake/gfx.cmake` |
| `-mcx16` only when `CMAKE_SYSTEM_PROCESSOR` is x86-64 | `recomp/cmake/NxRecomp.cmake` |

### M1 — compile-complete, behaviour verified for the shim, memory semantics not yet exercised

Every runtime source that is in the macOS build compiles against the shim: **87 files, 0 errors**, checked with
`g++ -std=c++20 -fsyntax-only` against the shim's include path. That covers `src/core`, `src/diag`, `src/eh`,
`src/kernel`, `src/mem`, `src/net`, `src/os`, `src/services` (minus the excluded backends) and `src/timestep`.

The shim's own semantics are verified by a behavioural test (not committed: a throwaway harness) that passes:

- the `block_on_wake` protocol — 3 waiters and 1 signaller, 9000 wakeups, no lost wakeup and no deadlock;
- SRWLOCK shared vs exclusive — 4 readers and 4 writers, 80000 increments, no lost update;
- auto-reset events, and a 5 ms `CreateWaitableTimerExW`/`SetWaitableTimer` deadline measured at 5.07 ms;
- `CreateThread(CREATE_SUSPENDED)` + `ResumeThread` + `WaitForSingleObject` on the thread handle;
- `VirtualAlloc(MEM_RESERVE)` at a fixed address, `MEM_COMMIT`, `VirtualProtect`, `MEM_DECOMMIT`, `MEM_RELEASE`,
  with `VirtualQuery` reporting the guest-granular state;
- a section with two views at fixed addresses sharing pages, and `VirtualFree(MEM_DECOMMIT)` refused on a view
  exactly as on Windows;
- positioned `ReadFile` (`OVERLAPPED` offset), `ERROR_HANDLE_EOF` past the end, `GetFileSizeEx`, `FindFirstFileExW`;
- UTF-8 ↔ UTF-32 round trip through `WideCharToMultiByte`/`MultiByteToWideChar`.

Design notes that came out of writing it:

- **SRWLOCK and CONDITION_VARIABLE are one pointer each.** `os/sync.cpp:110` relies on zero-initialised storage being
  a valid unlocked lock and `KThread::cv_storage` is one `void*`, while a Darwin `pthread_mutex_t` is 64 bytes whose
  zeroed state is not initialised. The pthread objects are allocated on first use, and `SleepConditionVariableSRW`
  releases the write lock atomically with the wait (the Win32 contract), which is what makes the kernel lock's
  `block_on_wake` correct.
- **Every condvar deadline is on `CLOCK_REALTIME`.** `pthread_condattr_setclock` does not exist on Darwin and several
  of the objects are `PTHREAD_COND_INITIALIZER` statics that cannot take an attribute. A `CLOCK_MONOTONIC` deadline
  handed to a realtime condvar returns `ETIMEDOUT` immediately and turns every wait into a spin.
- **A waitable timer's worker is joined, not detached.** `CloseHandle` on a timer stops the worker before the mutex
  and condition variable are destroyed; a detached worker was waiting on freed objects.
- **A thread object outlives its thread.** `CreateThread` takes two references, one for the caller's handle and one
  for the running thread, so a `CloseHandle` right after creation cannot free the object the trampoline is writing to.
- **`VirtualAlloc(MEM_COMMIT)` on a section view only applies the protection**; the pages are already mapped and read
  as zeros until touched, which is `SEC_RESERVE` semantics. `VirtualFree(MEM_DECOMMIT)` on a view is refused, as
  `memory.cpp:574` documents.
- **`PAGE_EXECUTE*` maps to read/read-write.** Guest code is never executed (D7), and an executable anonymous mapping
  on arm64 macOS needs `MAP_JIT` plus the allow-jit entitlement.
- **Protection changes are applied at host granularity.** `mprotect` needs 16 KiB alignment while the guest page is
  4 KiB, so a change covers the union of the guest pages inside the touched host pages. The guest-level state in the
  shim's region table stays 4 KiB accurate, which is what `VirtualQuery` and `write_watch.cpp` read.
- **Structured exceptions are BSD signals, not mach ports.** A `SIGSEGV` handler runs on the faulting thread and
  returning from it resumes the faulting instruction, which is what `write_watch.cpp` needs (un-protect, return, the
  store retries). A mach exception port would need `thread_set_state` to resume. The `CONTEXT` is built from
  `ucontext_t`.

Not yet exercised: the memory layout the game actually needs (module image at `0x7100000000`, a 6 GiB heap section,
pool aliases) has no test — that is M3 work, and it needs a real game dump.

### M2 — partially done

Of the graphics shell's 89 sources, the **34 that need no fetched SDK header compile against the shim with 0
errors**; the other 55 need `volk.h`, `SDL3/*`, `imgui.h` or `zstd.h`, which come from `NXRT_DEPS_DIR` and are not
downloaded here, so they are unchecked rather than assumed good.

- `pool_copy`: `auto_copy()` returns true on macOS (`nvn_pool_copy.cpp`), because MoltenVK implements no
  `VK_EXT_external_memory_host` (D3).
- `platform::native_hwnd()` returns the `NSView*` from `SDL_PROP_WINDOW_COCOA_WINDOW_POINTER`, which is what
  `VK_EXT_metal_surface` takes.
- The DXGI/GDI HDR query has a CoreGraphics counterpart (`src/gfx/extras/win_display_posix.cpp`); DLSS and FSR3 are
  compiled out (their SDKs are Windows binaries).
- `frame_limiter.cpp`/`nvn_shaders.cpp` intrinsics resolve through `compat/intrin.h` (`_mm_pause` → `yield`,
  `__rdtsc` → `mach_absolute_time`, `__cpuid` → zeroes, so `have_shani()` is false and the portable SHA-1 loop is
  used).
- ASTC passes through untouched when the device samples it (`nvn_texdata.cpp` already does this through
  `caps.astc`), which MoltenVK does on Apple Silicon (D4). The `astc2bc7` transcoder itself is ISPC and
  x86-ISA-specific (`tools/textures/CMakeLists.txt:15` pins `ispc-v1.32.0-windows`, and `bc7e.ispc` is emitted with
  `--arch=x86-64`), so the macOS build takes `src/os/os_posix_a2b.cpp` instead: the same size and format helpers
  (checked against `tools/textures/src/a2b.cpp`'s formulas), and `A2B_ERR_UNSUPPORTED` from the three entry points
  that need the missing kernels. No source file had to change: the four call sites already handle a failed
  transcode, and on Metal they are never reached. That keeps `nxrt_gfx` linkable on macOS.

### The address space a real dump needs (measured)

The layout in `runtime/include/nxrt/memory.h` was written from the lifted configuration, not from a game. It has now
been checked against an actual package (2.0.1, update 655360): the ExeFS NSO headers give

| module | base | image | bss | end | slot headroom |
|---|---|---|---|---|---|
| `main` | `0x7100000000` | `0x34cbec0` (55.4 MiB) | `0x854140` | `0x7103d20000` | `0xfc2e0000` |
| `sdk` | `0x7500000000` | `0xe08870` (14.0 MiB) | `0x1e7790` | `0x7500ff0000` | `0xff010000` |
| `subsdk0` | `0x7600000000` | `0x130010` (1.19 MiB) | `0xfff0` | `0x7600140000` | `0xffec0000` |
| `rtld` | `0x7700000000` | `0x10008` (64 KiB) | `0xfff8` | `0x7700020000` | `0xfffe0000` |

78.4 MiB across the four slots, and the documented layout holds without adjustment: `main` ends `0xfc2e0000` short of
`kRemoteBase`, the 12 GiB `kHeapMax` window (`0x7800000000`..`0x7b00000000`) ends 1 GiB before `kStackBase`
(`0x7c00000000`, 4 GiB), which ends 1 GiB before `kKernelBase` (`0x7e00000000`, 4 GiB, ending at `0x7f00000000` —
well inside the 47-bit user limit, which is the binding constraint on arm64 macOS). `kAliasBase` (`0x6000000000` +
8 GiB) and `kHleBase` (`0x7400000000` + 16 MiB) clear everything else.

Checked through the shim (`VirtualAlloc`/`VirtualProtect`/`VirtualQuery`/`CreateFileMapping`+`MapViewOfFileEx` at those
addresses, 25 assertions, all passing on a POSIX host): the four images reserve at their lifted bases, a second
reservation of the same range is refused, a text page commits RW, seals RX and reads back through the guest address,
`VirtualQuery` reports the commit granularity, a 6 GiB `SEC_RESERVE` section takes its primary view at `0x7800000000`,
a 64 KiB alias view at `0x6000000000` shares its pages both ways, and the remote/HLE/stack/kernel regions all place.
What remains untested is macOS's own `MAP_FIXED` behaviour at `0x7100000000` and a 6 GiB `shm_open`+`ftruncate`; both
are the first things to run on a Mac, and the second is the one plausible surprise.

**Input format for M3.** The runtime serves a `.nup` package directly (`src/loader/nup.cpp`) but only when its files
are *stored*: a zstd-compressed package is refused with "… is compressed in the .nup; the game reads packages directly
and needs stored files". The 2.0.1 package in hand is compressed (ExeFS: 5/5 files zstd; RomFS: 25/36), so M3 needs
either the launcher's unpack (`launcher/src-tauri/src/nup.rs` decompresses into `nup_dump/`, which is also what
`pkp_build.py --exefs <dump>/updates/<v>/exefs` expects) or a repack without compression. Its five ExeFS sha256s and
the main build id match `game-build/release_b/dump_hashes.json` exactly, and its RomFS is 36 files / 7,246,052,773
bytes — the manifest's figures to the byte — so it is the right dump; only its codec is wrong for the direct-read path.

### The lifter covers the whole game (measured)

The lift is the one part of M3 that can be checked without a Mac, and it came back clean. Lifting the five verified
ExeFS files through `recomp/games/pkp/CMakeLists.txt`'s module table (`--game pkp`, each module at its lifted base):

| module | functions | instructions | shards | not translated |
|---|---|---|---|---|
| `main` | 149,580 | 10,195,167 | 838 | **0** |
| `sdk` | 47,943 | 1,944,009 | 165 | **0** |
| `subsdk0` | 2,730 | 175,636 | 15 | **0** |
| `rtld` | 37 | 2,393 | 1 | **0** |

12,317,205 instructions and 200,290 functions with nothing left untranslated, in ~36 s of Python on 10 cores. The
tier-2 lifter therefore covers every instruction shape this game uses — the "some AArch64 shape falls through to the
tier-1 reference decoder or is skipped" risk does not apply to 2.0.1. `main` produces 663 MB of C++ across 839 files;
a 680 KiB shard compiles in 1.9 s at `-O1`, so the M3 build is minutes on a laptop. Eight shards sampled across the
range and `module_main.cpp` (35 MB) all pass `g++ -std=c++20 -fsyntax-only` against `recomp/include`, which is the
first evidence that the emitted code is portable C++20 rather than something only clang-cl accepts. The CMake path
was then run end to end on that Linux host (g++ 13.3, GNU make, no Ninja): `--target pkp_rtld` lifts, compiles and
archives `libpkp_rtld.a` in 1.4 s, so `NxRecomp.cmake`'s non-MSVC branch is exercised, not just written.

### Not started

M3 (game bring-up — the lift and the generated-code compile are verified above, the executable and the run are not),
M4 (platform services: CoreAudio sink, `fs_volume` XPC check), M5 (fault-driven features: the
write tracker and the sampler both need a real `GetThreadContext`, which needs `thread_get_state` on a mach thread
port, i.e. the mach exception work), M6 (AVFoundation movies, launcher, packaging).
