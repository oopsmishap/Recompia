# HANDOVER — macOS port

Practical companion to [`docs/macos-port.md`](docs/macos-port.md). That file holds the design and its rationale
(D1–D12, §7 implementation status, the measured address space); this one holds what to type, what to expect, and
what breaks first.

- **Branch** `feat/macos-support` on `oopsmishap/Recompia`, tip `4be58a7`, forked from `Dittobyte/Recompia`
  `main` `5f98748`. No PR opened.
- **Nothing here has run on Apple Silicon yet.** Every macOS claim below is a compile result or a host-side
  behavioural test, never a GPU frame. Closing that gap is job one.

## Where it stands

| Milestone | State | Evidence it has |
|---|---|---|
| M0 build skeleton | **done** | 87 of 87 runtime sources pass a C++20 syntax check as a macOS build |
| M1 memory, kernel, timers | shim complete; address space proven, memory semantics not exercised by the game | 25/25 address-space assertions at the real module addresses; behavioural harness green |
| M2 graphics on MoltenVK | compiles; **never opened a window** | 34/34 of the graphics sources that need no fetched SDK header |
| M3 game bring-up | **not started** | — |
| M4 platform services (CoreAudio, `fs_volume` XPC) | not started | — |
| M5 fault-driven features, diagnostics | partial: the `SIGSEGV`/`SIGBUS` handler exists; `GetThreadContext`/`SuspendThread` on another thread is reported unsupported | — |
| M6 media, launcher, packaging | not started | — |

The syntax checks were `g++ 13.3 -std=c++20 -fsyntax-only` on Linux against the shim — **not** Apple clang. Treat
"compiles" as "no obvious portability error", not as "builds on your machine". The other 55 graphics sources have
never been parsed at all: they need volk, SDL3, ImGui and zstd headers, which only exist after the first configure.

The M1 harness measured the shim itself, not the game: 9,000/9,000 block/wake round trips, 80,000/80,000 rwlock
transitions, a 5 ms timer landing at 5.10 ms, a 30 ms sleep at 30.09 ms, `CREATE_SUSPENDED` + `ResumeThread`,
positioned `ReadFile` returning `ERROR_HANDLE_EOF` at EOF, and two section views sharing pages both ways.

## The Mac

| Need | Why |
|---|---|
| Xcode command line tools (clang, SDK 13.0+) | `CMAKE_OSX_DEPLOYMENT_TARGET` is 13.0 |
| CMake ≥ 3.25, Ninja | the presets are Ninja-based |
| [LunarG Vulkan SDK for macOS](https://vulkan.lunarg.com/sdk/home) | supplies `libMoltenVK.dylib`. Must be **MoltenVK ≥ 1.3.0** — Vulkan 1.3 is a hard requirement (`runtime/src/gfx/vk_device.cpp:365` fails below it) plus `dynamicRendering` and `synchronization2` (`:481`) |
| Python 3.13 and `pip install -r recomp/requirements.txt` | `capstone==5.0.9` (instruction decode, tier 2), `lz4` (NSO segments), `pyelftools` (ELF). The lifter fails with a bare `ModuleNotFoundError` without them |

Everything else is fetched by pinned URL + SHA256 on the first configure into `~/Library/Caches/nxrt/deps`:
SDL3 3.4.18, Vulkan-Headers and volk `vulkan-sdk-1.4.363.0`, VMA v3.4.0, ImGui v1.92.9b, zstd v1.5.7
(`ZSTD_DISABLE_ASM`). Only MoltenVK comes from outside: `NXRT_MOLTENVK_DIR` defaults to `$VULKAN_SDK/lib`, and
configure stops with a `FATAL_ERROR` naming the SDK if it is absent. It is copied next to each executable as
`libvulkan.1.dylib`, which is the name volk `dlopen`s on Apple.

## Build and first run

```sh
git clone https://github.com/oopsmishap/Recompia.git
cd Recompia && git checkout feat/macos-support
cd runtime
cmake --preset macos-arm64
cmake --build --preset macos-arm64
```

Three smoke tests, in the order that isolates failures best:

```sh
# 1. MoltenVK loads and the device reaches Vulkan 1.3 (headless: no window, no display needed)
./build/macos-arm64/nxrt_gfx_demo --headless --frames 300

# 2. The swapchain, present modes and the ImGui overlay actually present
./build/macos-arm64/nxrt_gfx_demo --overlay --frames 600        # then press F1

# 3. A frame you can look at, and the exact pixels are diffable
./build/macos-arm64/nxrt_gfx_demo --headless --frames 120 --screenshot shot.png --shot-frame 60
```

`nxrt_gfx_demo` prints one result line and writes `nxrt_gfx_demo.log`. If it dies inside `vk_device.cpp`, read
the `[gfx]` log lines first: the device-feature block logs every extension it enabled and every one it wanted and
did not get.

MoltenVK offers no `VK_EXT_external_memory_host`, no `VK_EXT_custom_border_color`, no `VK_KHR_push_descriptor` and
no `VK_EXT_transform_feedback`. Three of the four are harmless: `features_.external_memory_host` being false
forces the pool copy path (`runtime/src/gfx/nvn/nvn_pool_copy.cpp:73` returns true unconditionally on `__APPLE__`);
`custom_border_color` is read at `runtime/src/gfx/nvn/nvn_gpu_texture.cpp:1273` only when set; `push_descriptor` is
recorded and logged but no render path reads it.

Transform feedback is the one to watch. It is not emulated, so a stream-out draw is **skipped**, not approximated:
`runtime/src/gfx/nvn/nvn_replay.cpp:3576` and `:4553` call `note_unsupported` and bump `draws_skipped`. The symptom
is a scene that renders with effects missing, not a crash — check the unsupported notes before suspecting the
tiling or the shaders.

## M3 — getting the game to run

This is the next milestone and the one that will find the real bugs. The plan's work list is
`docs/macos-port.md:277-302`; the concrete recipe:

**1. Get the five ExeFS files.** The reference dump `Pokémon Pokopia + Update 2.0.1.nup` is zstd-compressed and
the runtime refuses a compressed package (`runtime/src/loader/nup.cpp:118`). Do not fight it — use the folder form,
which `game-build/release_b/pkp_build.py` and `game-build/src/pkp_main.cpp:456-474` both accept. The five
decompressed files already exist on the Windows box at `H:/projects/_mishap_/.nxdump/exefs/` (37,495,940 bytes
total); copy them over rather than re-extracting 7 GB:

| File | Bytes | sha256 prefix |
|---|---|---|
| `main` | 29,405,084 | `8a8ce87b…` |
| `main.npdm` | 1,956 | `de024cc0…` |
| `sdk` | 7,327,611 | `7c12078e…` |
| `subsdk0` | 750,812 | `2e91ffc8…` |
| `rtld` | 10,477 | `166d3b75…` |

Full hashes in `game-build/release_b/dump_hashes.json`; check them, they are the difference between a port bug and
a bad dump. `main` carries build id `76a489f3bd3642fa5e18bd83d32c98bacc5f7d0e` at offset 0x40.

**2. Lift and compile with the game's CMake project.** Do not lift by hand. `recomp/games/pkp/CMakeLists.txt`
already declares all four modules with their bases, indexes and shard counts, and it passes the hook-site list
(`recomp/games/pkp/main.sites` — 278 lines, 144 of them `midhook`s, including the frame-update
`entryhook 0x7101fdcde4` at line 104) that a bare `nxrecomp lift` invocation silently drops — the game would build
and then run without the runtime's hooks.

```sh
cd Recompia
cmake -S recomp/games/pkp -B work/build/pkp -G Ninja \
      -DNX_ROOT="$PWD" \
      -DPKP_EXEFS=<exefs> \
      -DNX_PYTHON="$(command -v python3)"
cmake --build work/build/pkp --target pkp_modules -j "$(sysctl -n hw.perflevel0.logicalcpu)"
```

`-DNX_PYTHON` is the one flag you must not forget: it defaults to `recomp/../tools/.venv/Scripts/python.exe`, a
Windows path. Everything else has a usable default. No toolchain file is needed — unlike Windows, which goes
through `recomp/cmake/clang-cl.cmake`, Apple clang is the default — and `recomp/cmake/NxRecomp.cmake:102-111`
already gates `-mcx16` to x86-64 and carries a non-MSVC flag branch
(`-O1 -w -fno-exceptions -fno-rtti -fno-strict-aliasing -ffp-contract=off`).

Generated C++ goes to `work/lift/<module>`, build output to `work/build/pkp`. Targets: `pkp_main`, `pkp_sdk`,
`pkp_subsdk0`, `pkp_rtld` — one static library each, providing `nxmod_<name>` — and `pkp_modules` for all four.

**What to expect, measured on this tree.** All four modules lift in ~36 s of Python on 10 cores:

| module | functions | instructions | shards | not translated |
|---|---|---|---|---|
| `main` | 149,580 | 10,195,167 | 838 | **0** |
| `sdk` | 47,943 | 1,944,009 | 165 | **0** |
| `subsdk0` | 2,730 | 175,636 | 15 | **0** |
| `rtld` | 37 | 2,393 | 1 | **0** |

**12.3 M instructions, 200,290 functions, 0 not translated.** The tier-2 lifter covers every instruction shape this
game uses, which removes the largest M3 unknown. `main` alone is 663 MB of generated C++ across 839 files; one
680 KiB shard compiles in 1.9 s at `-O1`, so the whole build is minutes, not hours. Eight shards sampled across the
range plus `module_main.cpp` (35 MB) pass `g++ -std=c++20 -fsyntax-only` against `recomp/include` — the output is
ordinary C++20, not MSVC-flavoured. The CMake path itself was exercised on a Linux host with g++ 13.3 and GNU make:
configure, then `--target pkp_rtld`, lifts, compiles and archives `libpkp_rtld.a` in 1.4 s. The wiring and the flags
are therefore proven on a non-MSVC toolchain; only Apple clang and Ninja are untried.

There is no `pkp_link_check` target in this fork: it needs `recomp/games/mpr/link_check.cpp`, which the repository
does not carry. Unresolved symbols therefore surface only at the final link of the game executable.

**3. Port `game-build/CMakeLists.txt`.** Four changes, all mechanical:

- the imported `.lib` block (`pkp_main`, `pkp_sdk`, `pkp_subsdk0`, `pkp_rtld`) becomes `.a` — or better, build the
  lifted sources in-tree instead of importing;
- drop `/MAP /STACK:134217728 /HIGHENTROPYVA:NO` and `-clang:-fmacro-prefix-map` (use `-ffile-prefix-map`);
- drop `/SUBSYSTEM:WINDOWS /ENTRY:mainCRTStartup` and `PKP_GUI_SUBSYSTEM` — on macOS "no console window" is a
  bundle property, not a linker flag, and `game-build/src/pkp_main.cpp:317` is already `int main`;
- drop the `DejaVuSans.ttf` and `astcdec-*.dll` copies. There is no ASTC transcoder on macOS: Metal samples ASTC
  natively, so `nvn_texdata` takes `Mode::Passthrough` and `runtime/src/os/os_posix_a2b.cpp` only answers the
  format questions.

**4. Run it.** Acceptance for M3 is the game booting past the title screen into gameplay. Expect the first failure
in `runtime/src/services/fs_romfs.cpp:537-564` — that is the RomFS read path and the first thing the game touches.
The rest of the M3 service list (`time`, `timezone`, `pl`, `acc`, `album`, `misc`, `common`, `logfault`) is in
`docs/macos-port.md:277-302`.

## Traps already paid for

These cost hours during the port. Do not re-learn them.

- **Every condition-variable deadline is `CLOCK_REALTIME`.** Darwin has no `pthread_condattr_setclock`, and several
  objects are `PTHREAD_COND_INITIALIZER` statics. A monotonic deadline handed to a realtime condvar returns
  `ETIMEDOUT` immediately and turns every wait into a spin — it looks like a 100% CPU hang, not like a clock bug.
- **Timer teardown deadlocks if the worker is detached.** `CloseHandle` on a timer with a blocked detached worker
  waits forever on a destroyed mutex. Workers are joinable and stop on a flag.
- **A thread object must outlive its thread.** `ThreadObj` was freed under the running trampoline; it now carries
  two references and the trampoline releases one.
- **Two memory primitives are still unproven:** `MAP_FIXED` at `0x7100000000` and a 6 GiB
  `shm_open` + `ftruncate` (the `SEC_RESERVE` heap section). Both are expected to work; both are the plausible
  surprise on a Mac. If `CreateFileMapping` for the heap fails, look here first.
- **There is no JIT and no `PROT_EXEC`.** `Prot::RX`/`RWX` deliberately drop `PROT_EXEC`
  (`runtime/src/os/os_posix_mem.cpp`, decision D7): guest code is never executed by the host, only lifted C++. If
  you ever add something that runs guest bytes, that is the line to change — and it drags in `MAP_JIT` and
  entitlements with it.
- **`runtime/src/os/nx_os.h` must stay C11-compatible.** `runtime/third_party/pokopia-coop` is C. C++ overloads live
  in a `__cplusplus` section.
- **`windows.h` and friends resolve to the shim only on non-Windows.** `runtime/src/os/compat/` is added to the
  include path for the non-Windows target alone, so the Windows build still gets the real SDK headers. Do not
  "clean up" that conditional.

## What the Windows box could not verify, and why

Useful context if a Windows-side check ever disagrees with the Mac:

- clang-cl 18 cannot parse MSVC 14.50's STL (needs clang 19+), and MSVC `cl` rejects the `__attribute__` in
  `recomp/include/nxrecomp/ctx.h`. Windows-side syntax checking was therefore limited to three files
  (`audio_wav.cpp`, `symbols.cpp`, `stackwalk.cpp`).
- The `.nup` on `D:` is not readable from the Linux eval kernel; it was read through WSL at `/mnt/d`. The Windows
  `bash` tool mangles the `é` in the filename when it is passed as an argument.

## Map of the work

| Path | What is in it |
|---|---|
| `runtime/src/os/` | the shim: `nx_os.h` (380 names), `nx_os_internal.h` (the one object model), `os_posix.cpp` (2,150 lines: threads, sync, wait, time, UTF), `os_posix_file.cpp`, `os_posix_mem.cpp`, `os_posix_net.cpp`, `os_posix_misc.cpp`, `os_posix_a2b.cpp`, and `compat/` (19 headers that make `<windows.h>` resolve here) |
| `runtime/CMakeLists.txt`, `runtime/cmake/gfx_deps.cmake`, `runtime/cmake/gfx.cmake` | the platform branch, the pinned dependency fetcher, the MoltenVK lookup and the `libvulkan.1.dylib` copy |
| `runtime/CMakePresets.json` | `macos-arm64`, `macos-arm64-debug` |
| `runtime/src/gfx/extras/win_display_posix.cpp` | CoreGraphics HDR query (the Windows DXGI path is filtered out on Apple) |
| `recomp/` | the AArch64 → C++20 lifter; `recomp/games/pkp/config.py` is the game seam |
| `game-build/` | the Windows game executable and `release_b/pkp_build.py`, the model-(b) first-run build — the template to port in M3 |
| `docs/macos-port.md` | decisions D1–D12, milestones M0–M6, risk register, the Win32 → POSIX mapping table, and §7's evidence log |
