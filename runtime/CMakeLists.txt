# SPDX-License-Identifier: GPL-3.0-or-later
# nxrt: game-independent runtime skeleton (OS/HLE layer) for the Metroid Prime Remastered static recompilation.
# Build: scripts\build.ps1   (clang-cl + Ninja, Release)  |  macOS: cmake --preset macos-arm64 (docs/macos-port.md)
cmake_minimum_required(VERSION 3.25)

if(WIN32)
    # Static MSVC runtime (/MT): players need no Visual C++ Redistributable. Set before project() so every target below, third-party code
    # included, uses it; the libraries this one links must agree (a mismatch is a link error).
    if(NOT DEFINED CMAKE_MSVC_RUNTIME_LIBRARY)
        set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    endif()
endif()
project(nxrt C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_C_STANDARD 11)

# Two native targets: Windows x64 (the shipped build) and Apple Silicon macOS (docs/macos-port.md). Everything else is unsupported.
if(WIN32)
    set(NXRT_WINDOWS TRUE)
elseif(APPLE)
    if(NOT CMAKE_SYSTEM_PROCESSOR MATCHES "^(arm64|aarch64)$")
        message(FATAL_ERROR "nxrt builds for Apple Silicon (arm64) only: the guest is AArch64 and the recompiler emits AArch64-derived C++ "
                            "with 64-bit atomics; an x86_64 macOS build would need the x86 translation path the project does not have")
    endif()
    set(NXRT_APPLE TRUE)
    if(NOT DEFINED CMAKE_OSX_DEPLOYMENT_TARGET)
        set(CMAKE_OSX_DEPLOYMENT_TARGET "13.0" CACHE STRING "macOS floor: MoltenVK 1.4 needs 12.0, C++20 std::format and the SDL3 macOS "
                                                           "backend are comfortable at 13")
    endif()
else()
    message(FATAL_ERROR "nxrt targets native Windows x64 and Apple Silicon macOS only")
endif()

# The generated-code ABI header (Ctx, call_indirect, ...) lives in the recompiler (recomp/include/nxrecomp/).
# Found next to the checkout (../recomp) or, for a git worktree under runtime-wt/<name>, two levels up (../../recomp).
set(NXRT_RECOMP_INCLUDE "${CMAKE_CURRENT_SOURCE_DIR}/../recomp/include" CACHE PATH "Directory that holds nxrecomp/")
if(NOT EXISTS "${NXRT_RECOMP_INCLUDE}/nxrecomp/ctx.h" AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/../../recomp/include/nxrecomp/ctx.h")
    get_filename_component(_nxrt_ri "${CMAKE_CURRENT_SOURCE_DIR}/../../recomp/include" ABSOLUTE)
    set(NXRT_RECOMP_INCLUDE "${_nxrt_ri}" CACHE PATH "Directory that holds nxrecomp/" FORCE)
endif()

if(NXRT_WINDOWS)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
        add_compile_options(/W3 -Wno-unused-parameter -Wno-missing-field-initializers -Wno-cast-function-type-mismatch
                            -Wno-cast-function-type-strict -Wno-language-extension-token)
    endif()
    add_compile_definitions(NOMINMAX WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS _WIN32_WINNT=0x0A00)
else()
    # The POSIX shim (src/os/nx_os.h) stands in for the Win32 API. -Wno-deprecated-declarations covers the shim's deliberate use of
    # pthread_* primitives; everything else stays at the compiler's default warnings.
    add_compile_options(-Wno-unused-parameter -Wno-missing-field-initializers -Wno-deprecated-declarations)
    add_compile_definitions(NXRT_POSIX=1)
endif()

option(NXRT_BUILD_GFX "Build the graphics shell (SDL3 + Vulkan + NVN front end, src/gfx, cmake/gfx.cmake)" ON)
# Game modules (games/<id>/, docs/GAME_BOUNDARY.md): the title-specific code a game exe links.
option(NXRT_BUILD_GAMES "Build the game modules in games/" ON)
set(NXRT_GAMES "pkp" CACHE STRING "ids of the game modules to build (games/<id>/), a list")

file(GLOB_RECURSE NXRT_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp")
list(FILTER NXRT_SOURCES EXCLUDE REGEX "/src/gfx/")   # the graphics shell is its own library (cmake/gfx.cmake)
if(NXRT_WINDOWS)
    list(FILTER NXRT_SOURCES EXCLUDE REGEX "/src/os/os_posix.*\\.cpp$")   # the shim itself: POSIX targets only
else()
    list(FILTER NXRT_SOURCES EXCLUDE REGEX "/src/os/os_win32\\.cpp$")
    # Subsystems built on a Windows-only SDK. Each has a POSIX counterpart on the milestone list in
    # docs/macos-port.md: Media Foundation movies (AVFoundation) and the XAudio2 sink (CoreAudio) are M3.
    list(FILTER NXRT_SOURCES EXCLUDE REGEX "/src/media/movie_mf\\.cpp$")
    list(FILTER NXRT_SOURCES EXCLUDE REGEX "/src/services/audio_xaudio2\\.cpp$")
    list(FILTER NXRT_SOURCES EXCLUDE REGEX "/src/services/audio_devices\\.cpp$")   # WASAPI device enumeration
    list(FILTER NXRT_SOURCES EXCLUDE REGEX "/src/services/album\\.cpp$")           # WIC screenshot writer
endif()
add_library(nxrt STATIC ${NXRT_SOURCES})
target_include_directories(nxrt PUBLIC include)
if(NOT NXRT_WINDOWS)
    # `<windows.h>` and the other Win32 SDK headers resolve to src/os/compat/, which forward to the shim. Only on the non-Windows target,
    # so the Windows build keeps using the real SDK headers byte for byte.
    target_include_directories(nxrt PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/src/os" "${CMAKE_CURRENT_SOURCE_DIR}/src/os/compat")
    # astc2bc7's interface header (tools/textures). The transcoder itself is an x86 ISPC build, so the POSIX build uses
    # src/os/os_posix_a2b.cpp: the same helpers, and an honest "unsupported" for the kernels Metal replaces (D4).
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/../tools/textures/include/astc2bc7.h")
        target_include_directories(nxrt PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../tools/textures/include")
    endif()
    target_link_libraries(nxrt PUBLIC "-framework CoreFoundation" "-framework IOKit" "-framework Security")
endif()
if(EXISTS "${NXRT_RECOMP_INCLUDE}")
    target_include_directories(nxrt PUBLIC "${NXRT_RECOMP_INCLUDE}")
endif()
if(NXRT_WINDOWS)
    target_link_libraries(nxrt PUBLIC psapi)
endif()
add_subdirectory(third_party/pokopia-coop)   # [net] protocol = native: pokopia-native's Link Play (MIT, C11)
target_link_libraries(nxrt PRIVATE pokopia_coop)
add_subdirectory(third_party/miniz)   # zlib streams (MIT): the Pokopia save repair, games/pkp/pkp_save_uuid.cpp
include(cmake/media_deps.cmake)   # libvpx for the Switch 2 video decoders (optional)

if(NXRT_BUILD_GFX)
    include(cmake/gfx.cmake)
endif()

if(NXRT_BUILD_GAMES)
    foreach(_game ${NXRT_GAMES})
        add_subdirectory(games/${_game})
    endforeach()
endif()
# The launcher's branding and factory settings: the data part of one game module and its launcher.rc (version info, manifest). Without that
# module the launcher builds unbranded, with no resources. The launcher is Win32 UI (cmake/launcher.cmake), so it is Windows only.
if(NXRT_WINDOWS AND TARGET mpr_launcher AND TARGET nxrt_game_${NXRT_LAUNCHER_GAME}_data)
    target_sources(mpr_launcher PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/games/${NXRT_LAUNCHER_GAME}/launcher.rc")
    target_link_libraries(mpr_launcher PRIVATE nxrt_game_${NXRT_LAUNCHER_GAME}_data)
    target_compile_definitions(mpr_launcher PRIVATE NXRT_LAUNCHER_GAME_REGISTER=nxrt_register_game_${NXRT_LAUNCHER_GAME}_data)
    target_link_options(mpr_launcher PRIVATE /MANIFEST:NO)   # launcher.rc carries the manifest
endif()
