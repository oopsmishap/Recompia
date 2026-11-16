// SPDX-License-Identifier: GPL-3.0-or-later
// Pokémon Pokopia's test-tooling probes (nxrt/tt.h GameProbes, registered in pkp_core.cpp): the player's position, heading and action state,
// the stage id, the play time and the in-game time of day, read from guest memory at each frame boundary. No hooks: the frame source is
// `tick` (.weaver/play.sh). The script steps that need game functions (@warp @give @call ...) are not provided.
//
// Where the values live (main-relative offsets of the 2.0.1 update):
//   player manager  [main + 0x3561928] (26344 bytes, made in 0x71000ae380); +0x30 = the local player (+0x38 the second one). The follow
//                   camera (0x7100087840) and its reset (0x7100086000) read it the same way.
//   player          vtable slot 2 (0x71002e40d8) returns player + 0x20c0, the world position (3 floats, Y up); slot 3 (0x71002e4478) returns
//                   the heading at player + 0x2404 (radians); the local player's own overrides are below (kPlayerMode). [player + 0x18b8] + 0x984 is the u16 action state (0x71002779c8; the camera
//                   tests it against 136).
//   save data       [main + 0x3561930]: +0x13d373c = the time of day in seconds (float), +0x13d3740 year (u16), +0x13d3742 month,
//                   +0x13d3743 day (bytes); the play report (0x71001145fc) reads them so. +0x4a5418 = the play time in seconds (double,
//                   0x71011358e0 "PlayHour"). +0xc91f20 = m_stageId (byte, the save serialiser 0x71005d2c40). +0x6d7cec = m_tutorialHistory
//                   (1024 bytes, serialiser 0x71005ca320): `prog` counts its set bits.
#include <bit>
#include <chrono>
#include <cmath>
#include <cstring>

#include "nxrt/config.h"
#include "nxrt/hooks.h"
#include "nxrt/log.h"
#include "core/pkp_guest.h"
#include "core/pkp_layout.h"
#include "tt_internal.h"

namespace nxrt::tt::pkp {
namespace {

using nxrt::tt::detail::game_base;
using nxrt::tt::detail::read_mem;

using games::pkp::layout::kPlayerManager;
using games::pkp::layout::kManagerPlayer;
using games::pkp::layout::kPosition;
using games::pkp::layout::kHeading;
// The local player's class (vtable main+0x322fda8) overrides both getters (0x71002e4084, 0x71002e4420): while its mode word at +0x5a34 is 0 and
// the byte at +0x5a39 is 0 (vtable slots 504/512), the position and heading come from the save data instead (+0x7419f0, +0x741a10).
using games::pkp::layout::kPlayerMode;
using games::pkp::layout::kPlayerModeByte;
constexpr uint64_t kSaveAltPos = 0x7419f0;
using games::pkp::layout::kSaveHeading;
using games::pkp::layout::kAction;
using games::pkp::layout::kActionId;
using games::pkp::layout::kSaveRoot;
constexpr uint64_t kSaveTimeOfDay = 0x13d373c;
using games::pkp::layout::kSavePlayTime;
constexpr uint64_t kSaveStageId = 0xc91f20;
constexpr uint64_t kSaveTutorials = 0x6d7cec;   // m_tutorialHistory, 1024 bytes (serialiser 0x71005ca320); its set bits are `prog`

template <class T>
bool rd(uint64_t a, T* v) { return read_mem(a, v, sizeof(T)); }
uint64_t rd64(uint64_t a) { uint64_t v = 0; rd(a, &v); return v; }

bool read_state(GameState* out) {
    GameState g;
    const uint64_t base = game_base();
    const uint64_t mgr = rd64(base + kPlayerManager);
    const uint64_t player = mgr ? rd64(mgr + kManagerPlayer) : 0;
    const uint64_t save = rd64(base + kSaveRoot);
    if (player) {
        float p[3], yaw = 0;
        uint32_t mode = 1;
        uint8_t mode_byte = 1;
        const bool alt = save && rd(player + kPlayerMode, &mode) && rd(player + kPlayerModeByte, &mode_byte) && mode == 0 && mode_byte == 0;
        const uint64_t pos_at = alt ? save + kSaveAltPos : player + kPosition;
        const uint64_t yaw_at = alt ? save + kSaveHeading : player + kHeading;
        if (rd(pos_at, &p) && std::isfinite(p[0]) && std::isfinite(p[1]) && std::isfinite(p[2])) {
            std::memcpy(g.pos, p, sizeof p);
            if (rd(yaw_at, &yaw) && std::isfinite(yaw)) g.yaw = yaw;
            const uint64_t act = rd64(player + kAction);
            uint16_t st = 0;
            if (act && rd(act + kActionId, &st)) g.move_state = st;
            g.have_player = true;
        }
    }
    if (save) {
        float tod = 0;
        if (rd(save + kSaveTimeOfDay, &tod) && std::isfinite(tod) && tod >= 0 && tod < 86400.0f * 2) { g.tod = tod / 3600.0f; g.have_tod = true; }
        double play = 0;
        if (rd(save + kSavePlayTime, &play) && std::isfinite(play) && play >= 0) { g.igt = play; g.have_igt = true; }
        uint8_t stage = 0;
        if (rd(save + kSaveStageId, &stage)) { g.area[0] = stage; g.have_world = true; }
        uint64_t bits[128];
        if (rd(save + kSaveTutorials, &bits)) {
            int n = 0;
            for (uint64_t w : bits) n += std::popcount(w);
            g.prog = n;
            g.have_prog = true;
        }
    }
    static bool logged = false;
    if (!logged && player) {      // once: which class the player is (its vtable, main-relative) and where slot 2 points, to check the layout above
        logged = true;
        const uint64_t vt = rd64(player);
        NXLOG_I("tt", "pkp probe: player %llx vtable main+0x%llx slot2 main+0x%llx", static_cast<unsigned long long>(player),
                static_cast<unsigned long long>(vt - base), static_cast<unsigned long long>(rd64(vt + 16) - base));
    }
    *out = g;
    return g.have_player || g.have_tod || g.have_world;
}

// @tp: write the three floats where read_state reads them, so a teleport and a following @pos agree. The local player's getters read the
// save data instead of the player struct while its mode words are 0 (kPlayerMode above): that is where the position lives then, and it is
// what the game itself streams around.
bool set_position(float x, float y, float z) {
    const uint64_t base = game_base();
    const uint64_t mgr = rd64(base + kPlayerManager);
    const uint64_t player = mgr ? rd64(mgr + kManagerPlayer) : 0;
    if (!player) return false;
    const uint64_t save = rd64(base + kSaveRoot);
    uint32_t mode = 1;
    uint8_t mode_byte = 1;
    const bool alt = save && rd(player + kPlayerMode, &mode) && rd(player + kPlayerModeByte, &mode_byte) && mode == 0 && mode_byte == 0;
    const uint64_t at = alt ? save + kSaveAltPos : player + kPosition;
    float old[3] = {0, 0, 0};
    if (rd(at, old)) NXLOG_I("tt", "pkp probe: player at %.2f, %.2f, %.2f -> %.2f, %.2f, %.2f", old[0], old[1], old[2], x, y, z);
    const float p[3] = {x, y, z};
    if (!nxrt::tt::detail::write_mem(at, p, sizeof p)) return false;
    NXLOG_I("tt", "pkp probe: player position written at main+0x%llx (%s)", static_cast<unsigned long long>(at - base),
            alt ? "save data" : "player struct");
    return true;
}

// @stage: the save's stage id (m_stageId, a byte). write: store *id there; otherwise read it into *id. It is the stage the game's save names,
// and the one a scene load comes back to - writing it while a stage is loaded does not swap the scene by itself.
bool stage_id(uint8_t* id, bool write) {
    const uint64_t base = game_base();
    const uint64_t save = rd64(base + kSaveRoot);
    if (!save) return false;
    const uint64_t at = save + kSaveStageId;
    if (write) {
        uint8_t old = 0;
        if (rd(at, &old)) NXLOG_I("tt", "pkp probe: stage %u -> %u", old, *id);
        return nxrt::tt::detail::write_mem(at, id, 1);
    }
    return rd(at, id);
}

}  // namespace

// The title's per-frame state machine (0xf6fc28) is the one place the title object is handed to us. With [boot] boot_world
// set, take the A path the title itself takes: sub_7100F714AC(title) checks state 1 (+0xa4), tests for a save
// (sub_7100F71250) and moves the title to state 5 (has save -> load) or 2 (no save -> language picker). That is what
// pressing A does; calling it needs no input and no menu.
static uint64_t g_title = 0;   // the title object, from its constructor

int hook_title_ctor(::Ctx* c, uint64_t, void*) {
    g_title = c->x[0];
    NXLOG_I("tt", "title ctor: object %llx", static_cast<unsigned long long>(g_title));
    return 0;
}

int hook_frame_tick(::Ctx* c, uint64_t, void*) {
    static bool taken = false;
    if (taken || config_get_str("boot", "boot_world", "").empty()) return 0;
    // A boot that never gets there must say so: the title sitting there forever is the failure this feature exists to avoid,
    // and it would otherwise look like a slow load.
    static const std::chrono::steady_clock::time_point start = std::chrono::steady_clock::now();
    static bool warned = false;
    if (!warned && std::chrono::steady_clock::now() - start > std::chrono::seconds(120)) {
        warned = true;
        NXLOG_E("tt", "boot world: no A path after 120 s (title %llx); the boot did not reach the world",
                static_cast<unsigned long long>(g_title));
    }
    if (!g_title) return 0;
    if (!games::pkp::guest::ok(g_title + 0xa4, 4)) return 0;
    const uint32_t state = games::pkp::guest::rd<uint32_t>(g_title + 0xa4);
    const uint8_t ready = games::pkp::guest::rd<uint8_t>(g_title + 0xb8);
    if (state != 1 || ready == 0) return 0;   // sub_7100F714AC needs both: +0xa4 == 1 and +0xb8 != 0
    taken = true;
    NXLOG_I("tt", "boot world: taking the title's A path (title %llx, state 1 -> loading)", static_cast<unsigned long long>(g_title));
    games::pkp::guest::call(c, 0x7100f714ac, g_title);
    return 0;
}

void install_hooks() {
    const uint32_t a = nxrt::hook_add_entry(0x7100f6f124, &hook_title_ctor);    // the title object
    const uint32_t e = nxrt::hook_add_entry(0x7100bfc020, &hook_frame_tick);    // the UI thread: the A path reads thread-local save state
    const uint32_t f = nxrt::hook_add_entry(0x7100f6fc28, &hook_frame_tick);    // the title's own per-frame: the first title frame
    (void)e;
    (void)f;
    NXLOG_I("tt", "boot world hooks: title ctor id %u", a);
}

const GameProbes& pkp_game_probes() {
    static const GameProbes p = [] {
        GameProbes t;
        t.read_state = &read_state;
        t.install_hooks = &install_hooks;
        t.set_position = &set_position;
        t.stage_id = &stage_id;
        return t;
    }();
    return p;
}

}  // namespace nxrt::tt::pkp
