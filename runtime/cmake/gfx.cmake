# SPDX-License-Identifier: GPL-3.0-or-later
# Graphics shell (S4b): SDL3 platform + input queue, Vulkan 1.3 device layer, NVN front end, ImGui overlay.
# Built as the static library nxrt_gfx (links nxrt).
include(${CMAKE_CURRENT_LIST_DIR}/gfx_deps.cmake)

# The offline tiling library of the tools repository is compiled in as source (tools/tiling, tegra_tiling.h): found at
# ../tools or, from a worktree under runtime-wt/<name>, at ../../tools.
set(NXRT_TOOLS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../tools" CACHE PATH "The tools repository (tools/tiling is used)")
if(NOT EXISTS "${NXRT_TOOLS_DIR}/tiling/src/tegra_tiling.cpp" AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/../../tools/tiling/src/tegra_tiling.cpp")
    get_filename_component(_nxrt_td "${CMAKE_CURRENT_SOURCE_DIR}/../../tools" ABSOLUTE)
    set(NXRT_TOOLS_DIR "${_nxrt_td}" CACHE PATH "The tools repository (tools/tiling is used)" FORCE)
endif()
if(NOT EXISTS "${NXRT_TOOLS_DIR}/tiling/src/tegra_tiling.cpp")
    message(FATAL_ERROR "tools/tiling not found: set NXRT_TOOLS_DIR to the tools repository (looked in ${NXRT_TOOLS_DIR})")
endif()
add_library(nxrt_tiling STATIC "${NXRT_TOOLS_DIR}/tiling/src/tegra_tiling.cpp")
target_include_directories(nxrt_tiling PUBLIC "${NXRT_TOOLS_DIR}/tiling/include")

# ASTC -> BC7 transcoder (tools/textures): the prebuilt static libraries of the tools repository (scripts/build.ps1 there) and the
# decoder DLLs that astc2bc7 loads at run time (copied next to every executable that links nxrt_gfx).
set(NXRT_A2B_DIR "${NXRT_TOOLS_DIR}/textures/build/release" CACHE PATH "Build folder of tools/textures (astc2bc7.lib, bc7e_ispc.lib, astcdec-*.dll)")
add_library(nxrt_astc2bc7 INTERFACE)
target_include_directories(nxrt_astc2bc7 INTERFACE "${NXRT_TOOLS_DIR}/textures/include")
if(APPLE)
    # tools/textures is an MSVC + ISPC build (tools/textures/CMakeLists.txt pins the Windows ISPC), so the offline
    # ASTC -> BC7 transcoder does not exist for macOS yet. Until it does the runtime hands the guest's ASTC blocks to
    # Metal as they are (docs/macos-port.md, M2), which needs no transcoder; nothing links the library.
    message(STATUS "nxrt: ASTC -> BC7 transcoder is Windows-only; macOS uses ASTC passthrough (src/os/os_posix_a2b.cpp, docs/macos-port.md D4)")
else()
    if(NOT EXISTS "${NXRT_A2B_DIR}/astc2bc7.lib" OR NOT EXISTS "${NXRT_A2B_DIR}/bc7e_ispc.lib")
        message(FATAL_ERROR "astc2bc7.lib not found in ${NXRT_A2B_DIR}: build tools/textures first (pwsh tools/textures/scripts/build.ps1) or set NXRT_A2B_DIR")
    endif()
    target_link_libraries(nxrt_astc2bc7 INTERFACE "${NXRT_A2B_DIR}/astc2bc7.lib" "${NXRT_A2B_DIR}/bc7e_ispc.lib")
endif()
file(GLOB NXRT_A2B_DLLS "${NXRT_A2B_DIR}/astcdec-*-shared.dll")

file(GLOB_RECURSE NXRT_GFX_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/gfx/*.cpp")
if(NOT NXRT_WINDOWS)
    # Windows-only pieces of the graphics shell: the DXGI/GDI HDR query and the DLSS/FSR3 temporal upscalers, whose
    # SDKs ship Windows binaries only. macOS gets its own HDR query (CGDisplay/Metal EDR) and MetalFX in M2.
    list(FILTER NXRT_GFX_SOURCES EXCLUDE REGEX "/src/gfx/extras/(win_display|temporal_dlss|temporal_ffx)\\.cpp$")
endif()
add_library(nxrt_gfx STATIC ${NXRT_GFX_SOURCES})
# The About page of the F1 menu shows the commit this build was made from (read when CMake configures; absent without git).
find_package(Git QUIET)
if(GIT_FOUND)
    execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${CMAKE_CURRENT_SOURCE_DIR}" rev-parse --short=10 HEAD
        OUTPUT_VARIABLE NXRT_BUILD_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET RESULT_VARIABLE _nxrt_git_rc)
    if(_nxrt_git_rc EQUAL 0 AND NXRT_BUILD_COMMIT)
        set_source_files_properties("${CMAKE_CURRENT_SOURCE_DIR}/src/gfx/play/play_ui_about.cpp" PROPERTIES COMPILE_DEFINITIONS "NXRT_BUILD_COMMIT=\"${NXRT_BUILD_COMMIT}\"")
    endif()
endif()
target_include_directories(nxrt_gfx PUBLIC include src/gfx/nvn/third_party PRIVATE src/gfx src/gfx/nvn)
target_link_libraries(nxrt_gfx PUBLIC nxrt nxrt_volk nxrt_vma nxrt_imgui nxrt_tiling nxrt_astc2bc7 nxrt_zstd SDL3::SDL3-static)
# The core calls into the graphics shell (src/core/switch2.cpp: the HDR layer luminance; src/testtools: the screenshot wait), so whatever
# links nxrt links nxrt_gfx too, as the game exe does. Static libraries may depend on each other.
target_link_libraries(nxrt INTERFACE nxrt_gfx)
target_compile_definitions(nxrt_gfx PUBLIC VMA_STATIC_VULKAN_FUNCTIONS=0 VMA_DYNAMIC_VULKAN_FUNCTIONS=1)

add_executable(nxrt_gfx_demo apps/gfx_demo.cpp)
target_link_libraries(nxrt_gfx_demo PRIVATE nxrt_gfx)
if(NXRT_WINDOWS)
    target_link_options(nxrt_gfx_demo PRIVATE /MAP /DEBUG /STACK:8388608)
    foreach(dll ${NXRT_A2B_DLLS})
        add_custom_command(TARGET nxrt_gfx_demo POST_BUILD COMMAND ${CMAKE_COMMAND} -E copy_if_different "${dll}" "$<TARGET_FILE_DIR:nxrt_gfx_demo>")
    endforeach()
else()
    # volk dlopen()s "libvulkan.1.dylib"; shipping MoltenVK under that name next to the executable makes the port
    # self-contained (no Vulkan SDK needed on a player's Mac). @executable_path is the search path.
    add_custom_command(TARGET nxrt_gfx_demo POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different "${NXRT_MOLTENVK_LIBRARY}" "$<TARGET_FILE_DIR:nxrt_gfx_demo>/libvulkan.1.dylib")
endif()

# The launcher (apps/launcher): plain Win32, no macOS equivalent.
if(NXRT_WINDOWS)
    include(${CMAKE_CURRENT_LIST_DIR}/launcher.cmake)
endif()
