# Credits

Parent project: [pokopia-native](https://github.com/xFractals/pokopia-native) (MIT; its `ampere-lower/` is GPL-3.0-or-later), whose updates this project follows. Built on [nxrecomp](https://github.com/drakolordx7/nxrecomp). Uses code from [Ryujinx](https://github.com/Ryubing/Ryujinx), [Mesa](https://gitlab.freedesktop.org/mesa/mesa), [pokopia-native](https://github.com/xFractals/pokopia-native), smo-recomp, ScarletNX, ssbu-recomp, SuperMarioWonderRecomp, yuzu, Eden, libnx, [astc-encoder](https://github.com/ARM-software/astc-encoder), [bc7enc_rdo](https://github.com/richgel999/bc7enc_rdo), [libvpx](https://chromium.googlesource.com/webm/libvpx), AMD FidelityFX, NVIDIA Image Scaling, [SDL3](https://github.com/libsdl-org/SDL), [Tauri](https://tauri.app), [Zig](https://ziglang.org) and Python, plus [Kenney input prompts](https://kenney.nl/assets/input-prompts), DejaVu fonts, and in the launcher [Inter](https://rsms.me/inter/) (SIL OFL 1.1) and [Lucide](https://lucide.dev) icons (ISC). Design ideas (no code) from totk-pc-recomp (shadow storage for guest memory) and ultratears (memory pool copies).

The Linux launcher (`launcher/src-tauri/src/host.rs`) runs Proton the way [pokopia-recomp-linux](https://github.com/tinfoil-chewer/pokopia-recomp-linux) (GPL-3.0) does, inside the Steam Linux Runtime, and follows it for an AppImage's environment and the pinned Linux Zig.

The Joy-Con 2 mouse mode follows [Pikantine](https://github.com/SpiderNic96/Pikantine) (GPL-3.0), an emulator build of the same game version: its clicks are the Joy-Con's R and ZR on the npad.

Licences are in `runtime/THIRD_PARTY_NOTICES.md`, `recomp/THIRD_PARTY.md` and `tools/shaders/licenses/`.
