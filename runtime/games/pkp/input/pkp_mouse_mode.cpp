// SPDX-License-Identifier: GPL-3.0-or-later
// Pokémon Pokopia, play mode: the Joy-Con 2 mouse mode key and the gate of the game's own mouse mode (pkp_play.h play_frame; split out of
// pkp_play.cpp).
#include "input/pkp_play_internal.h"
#include "input/pkp_play.h"

#include <cstdint>

#include "nxrt/jc2_mouse.h"
#include "nxrt/log.h"
#include "nxrt/memory.h"

namespace nxrt::games::pkp::play_detail {

using layout::kPlayerManager;                 // qword: the player manager; + kManagerPlayer = the local player's character
using layout::kManagerPlayer;
constexpr uint64_t kMmScreen = 0x35aebf0;     // qword: + 0xf0 -> + 0x18 -> + 0x4340 / + 0x4344 = the screen size the pointer is unprojected with
using layout::kMouseModeOn;                   // character: the game's mouse mode is on (u8)
constexpr uint64_t kMmHitCell = 0xaa30;       // character: the pointer's hit cell (s16 x3); its valid flag is layout::kMouseHitValid
using layout::kMouseHitValid;
using layout::kMouseSwingPos;                 // character: the swing tools' position under the pointer (vec4) and its flag
using layout::kMouseSwingValid;
using layout::kPosition;                      // character: world position (vec4), heading (radians)
using layout::kHeading;

// The local player's character, 0 when there is none.
uint64_t mm_player(State& s) {
    const uint64_t mgr = s.base ? rd<uint64_t>(s.base + kPlayerManager) : 0;
    return mgr ? rd<uint64_t>(mgr + kManagerPlayer) : 0;
}

bool mm_local(uint64_t ch) {
    State& s = st();
    if (!s.mm_gate.load(std::memory_order_relaxed) || !ch) return false;
    const uint64_t mgr = rd<uint64_t>(s.base + kPlayerManager);
    return mgr && ch == rd<uint64_t>(mgr + kManagerPlayer);
}
int hook_mm_style(::Ctx* c, uint64_t, void*) {
    if (mm_local(c->x[19])) c->x[0] = 1;
    return 0;
}
int hook_mm_option(::Ctx* c, uint64_t, void*) {
    if (mm_local(c->x[19])) c->x[0] = 1;
    return 0;
}
int hook_mm_start(::Ctx* c, uint64_t, void*) {
    if (mm_local(c->x[19])) c->x[8] |= 1;
    return 0;
}

// The game's screen size for the pointer, 0 when it is not there yet.
void mm_pointer_space(State& s) {
    const uint64_t a = rd<uint64_t>(s.base + kMmScreen), b = a && mem::is_mapped(a + 0xf0, 8) ? rd<uint64_t>(a + 0xf0) : 0,
                   scr = b && mem::is_mapped(b + 0x18, 8) ? rd<uint64_t>(b + 0x18) : 0;
    int w = 0, h = 0;
    if (scr && mem::is_mapped(scr + 0x4340, 8)) w = rd<int32_t>(scr + 0x4340), h = rd<int32_t>(scr + 0x4344);
    hid::jc2_mouse_set_pointer_space(w, h);
}

// ---- Joy-Con 2 mouse mode ----
// The Mouse mode key (Act::MouseMode, middle button in play) flips the request for the Joy-Con 2 mouse: the game then sees the PC mouse on its
// surface and the camera lets go of the mouse (hook_cam_*: counts dropped while jc2_mouse_active(); play mode stops capturing and hides the
// OS cursor). The request ends when the key is pressed again, or when the game stops being in play (a menu, the pause screen, a loading
// screen).
// The game's own mouse mode (#367): its per-player mode update asks 0x71002fa6ac "is mouse mode allowed" before it runs the detector (30
// frames of the mouse on its surface) and keeps asking while the mode is on. That check answers no unless the pad style is two detached
// Joy-Cons (0x710112ac60 == 1) and the save's mouse option bit is set; the PC pad is a Pro Controller, so the key moved nothing in the game.
// While the request is on (or [input] jc2_mouse = on) and the game is in play, the mid hooks below make the check see two Joy-Cons and the
// option on for the local player, and the turn-on (0x71002fa8b0) see the option too: the game switches to Rock Smash, shows its pointer and
// aims every tool with it, as on a Switch 2 with a Joy-Con 2 on its side. Clicks are the Joy-Con's R and ZR (hid_jc2_mouse.cpp
// jc2_pad_buttons, after Pikantine): with those the game uses the move at the pointer. The bindings' ZR (left click), which parked this mode
// in Nov 2026, uses it at the block in front of the character. Its other gates (menus, Link Play roles, busy states, the unlock) stay the
// game's. When the request ends the check says no again and the game leaves mouse mode itself (restoring the ability).
// The game unprojects the pointer with its screen size ([[[0x71035aebf0] + 0xf0] + 0x18] + 0x4340 / + 0x4344), not with the pointer range,
// so play_frame gives the hid service that size for the pointer's x, y.
void mouse_mode_log(State& s, const char* what) {
    const uint64_t now = mono_ns();
    if (s.mm_log_ns != 0 && now - s.mm_log_ns < 250000000ull) return;      // rate limit: a stuck key must not flood the log
    s.mm_log_ns = now;
    NXLOG_I(kTag, "mouse mode %s (jc2_mouse %s, active %d, camera counts dropped %llu)", what, hid::jc2_mouse_mode_name(hid::jc2_mouse_mode()),
            hid::jc2_mouse_active() ? 1 : 0, static_cast<unsigned long long>(s.mm_dropped.load()));
}

void mouse_mode_set(State& s, bool want, const char* why) {
    if (want == s.mm_want) return;
    s.mm_want = want;
    hid::jc2_mouse_request(want);
    mouse_mode_log(s, why);
}

void play_frame(bool in_game, double, play::GameFrame*) {
    State& s = st();
    if (!s.host) return;
    const uint64_t n = s.host->action_activations(play::Act::MouseMode);
    const bool pressed = n != s.mm_seen;
    s.mm_seen = n;
    const bool gate = in_game && hid::jc2_mouse_wants_pointer();
    // Leaving mouse mode: the game turns it off itself (0x710031618c: allowed, and the mouse no longer on its surface -> 0x71002fa8b0 off,
    // the pointer hidden, the ability restored), but only while the check still says "allowed". So after the request ends the gate stays
    // open for the local player until the game's flag drops (the mouse is captured again, so it is off its surface). If the flag is still
    // set after 3 s in play (the mouse never left its surface), it is cleared here: the game then stays on its current tool.
    const uint64_t ch = mm_player(s);
    const bool flag = ch && rd<uint8_t>(ch + kMouseModeOn) != 0;
    bool leaving = false;
    if (!gate && flag) {
        const uint64_t now = mono_ns();
        if (!s.mm_exit_ns) s.mm_exit_ns = now;
        if (in_game && now - s.mm_exit_ns > 3000000000ull) {
            wr<uint8_t>(ch + kMouseModeOn, 0);
            NXLOG_W(kTag, "mouse mode: the game did not leave its mouse mode within 3 s; cleared its flag");
            s.mm_exit_ns = 0;
        } else {
            leaving = true;
        }
    } else {
        s.mm_exit_ns = 0;
    }
    const bool open = gate || leaving;
    if (open != s.mm_gate.exchange(open, std::memory_order_relaxed) && s.mm_gate_logs < 200) {
        s.mm_gate_logs++;
        NXLOG_I(kTag, "mouse mode: the game's Joy-Con 2 mouse mode is %s", gate ? "allowed (two Joy-Cons, mouse option on)" : leaving ? "allowed until the game leaves it" : "the game's own answer again");
    }
    if (gate) mm_pointer_space(s);
    // what the pointer hits, every 2 s while the game's mouse mode is on (at most 40 lines): screen size, pointer cell, swing position,
    // the character's cell and heading
    if (flag && s.mm_diag_logs < 40 && mono_ns() - s.mm_diag_ns > 2000000000ull) {
        s.mm_diag_ns = mono_ns();
        s.mm_diag_logs++;
        int w = 0, h = 0;
        const uint64_t a = rd<uint64_t>(s.base + kMmScreen), b = a && mem::is_mapped(a + 0xf0, 8) ? rd<uint64_t>(a + 0xf0) : 0,
                       scr = b && mem::is_mapped(b + 0x18, 8) ? rd<uint64_t>(b + 0x18) : 0;   // the checks of mm_pointer_space
        if (scr && mem::is_mapped(scr + 0x4340, 8)) w = rd<int32_t>(scr + 0x4340), h = rd<int32_t>(scr + 0x4344);
        NXLOG_I(kTag, "mouse mode: screen %dx%d, pointer cell %d,%d,%d (valid %u), swing pos %.0f,%.0f (valid %u), player %.0f,%.0f heading %.2f",
                w, h, rd<int16_t>(ch + kMmHitCell), rd<int16_t>(ch + kMmHitCell + 2), rd<int16_t>(ch + kMmHitCell + 4), rd<uint8_t>(ch + kMouseHitValid),
                static_cast<double>(rd<float>(ch + kMouseSwingPos)), static_cast<double>(rd<float>(ch + kMouseSwingPos + 8)), rd<uint8_t>(ch + kMouseSwingValid),
                static_cast<double>(rd<float>(ch + kPosition)), static_cast<double>(rd<float>(ch + kPosition + 8)), static_cast<double>(rd<float>(ch + kHeading)));
    }
    if (hid::jc2_mouse_mode() == hid::Jc2MouseMode::Off) {
        if (pressed) mouse_mode_log(s, "key ignored: [input] jc2_mouse = off");
        mouse_mode_set(s, false, "off (jc2_mouse = off)");
        return;
    }
    if (!in_game) {                                  // a menu, the pause screen or a loading screen: the camera side takes the mouse back
        mouse_mode_set(s, false, "off (the game left play)");
        return;
    }
    if (pressed) {
        if (hid::jc2_mouse_mode() == hid::Jc2MouseMode::On) mouse_mode_log(s, "key ignored: [input] jc2_mouse = on keeps the Joy-Con 2 mouse always");
        else mouse_mode_set(s, !s.mm_want, s.mm_want ? "off (key)" : "ON (key)");
    }
}

}  // namespace nxrt::games::pkp::play_detail
