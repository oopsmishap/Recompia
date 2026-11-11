// SPDX-License-Identifier: GPL-3.0-or-later
// Joy-Con 2 mouse (Nmouse / Jcms): the virtual devices in the game's transfer memory, the sampling from the play layer's feed, and the
// play layer's side of the API (include/nxrt/jc2_mouse.h). See hid.cpp for the HID service.
#include "hid_internal.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cmath>
#include <cstring>

#include "nxrt/config.h"
#include "nxrt/log.h"
#include "nxrt/memory.h"
#include "nxrt/text.h"

namespace nxrt::services::hidsvc {

Jc2Feed& feed() {
    static Jc2Feed* f = new Jc2Feed();
    return *f;
}

std::atomic<int> g_jc2_mode{-1};          // -1: not read from the config yet
std::atomic<bool> g_jc2_request{false};
std::atomic<bool> g_jc2_listed{false};    // the game set up Nmouse and the device is in its handle table
std::atomic<bool> g_jc2_active{false};    // last sampled "on its surface"
std::atomic<uint64_t> g_jc2_space{0};     // jc2_mouse_set_pointer_space: w << 32 | h, 0 = the pointer range

uint64_t now_ns() {
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count());
}

// The feed is live when play mode sent one in the last 250 ms with the window focused and the mouse free.
bool jc2_feed_live() {
    Jc2Feed& f = feed();
    std::lock_guard<std::mutex> lk(f.mu);
    return f.last_ns && now_ns() - f.last_ns < 250000000ull && f.m.focused && !f.m.captured;
}

namespace {

// ---- Joy-Con 2 mouse ----------------------------------------------------------------------------------------------------------------
// Lists the virtual devices in the Nmouse block: a merged mouse (slot 0) and a Joy-Con 2 (R) mouse of player 1 (slot 1). Info first, the
// handle's valid byte last, so a reader never sees a valid handle without its info. Caller holds st().mu.
void nm_list_devices(HidState& s) {
    uint8_t* b = reinterpret_cast<uint8_t*>(s.nm_addr);
    struct Dev { uint64_t handle; uint32_t types; bool has_npad; uint32_t npad; };
    const Dev devs[2] = {{kNmHandleMerged, kNmTypeMerged, false, 0}, {kNmHandleRight, kNmTypeJoyRightJcms, true, 0}};
    for (uint32_t i = 0; i < 2; ++i) {
        uint8_t* info = b + kNmInfo + i * kNmInfoSize;
        w32(info, 0x0, devs[i].types);
        info[0x4] = 1;
        w32(info, 0x8, devs[i].npad);
        info[0xC] = devs[i].has_npad ? 1 : 0;
        info[0x10] = 1;   // pointer enabled
        w64(b, kNmHandles + i * 16, devs[i].handle);
        std::atomic_thread_fence(std::memory_order_release);
        b[kNmHandles + i * 16 + 8] = 1;
    }
    g_jc2_listed.store(true);
    NXLOG_I("hid", "Joy-Con 2 mouse: listed a merged mouse and a Joy-Con 2 (R) mouse for player 1 (mode %s, pointer range %dx%d)",
            nxrt::hid::jc2_mouse_mode_name(nxrt::hid::jc2_mouse_mode()), s.nm_w, s.nm_h);
}

// The server's view of the Nmouse block after InitializeNmouse: zeroed, every ring's buffer count set, devices listed unless the mode is
// off. Caller holds st().mu.
void nm_setup(HidState& s) {
    uint8_t* b = reinterpret_cast<uint8_t*>(s.nm_addr);
    std::memset(b, 0, kNmMinSize);
    for (uint32_t i = 0; i < kNmSlots; ++i) ring_init<NmouseInternal>(b, kNmRings + i * kNmRingSize);
    s.nm_x = s.nm_y = -1;
    if (nxrt::hid::jc2_mouse_mode() != nxrt::hid::Jc2MouseMode::Off) nm_list_devices(s);
}

}  // namespace

void jcms_setup(HidState& s) {
    uint8_t* b = reinterpret_cast<uint8_t*>(s.jcms_addr);
    std::memset(b, 0, kJcmsMinSize);
    for (uint32_t i = 0; i < 16; ++i) ring_init<JcmsState>(b, i * kJcmsRingSize);
}

// One Nmouse sample from the play layer's feed: the pointer in the pointer range, the range itself, the buttons, the wheel (120 per notch, inferred from the game clamping its sum at +-600), and "on its surface" while jc2_mouse_active().
// Caller holds st().mu.
void sample_nmouse(HidState& s) {
    // The blocks are the game's own memory, checked when it lent them. The game may free it without Finalize (the commands clear the
    // addresses too), so the mapping is checked again before every write.
    if (s.nm_addr && !mem::is_mapped(s.nm_addr, kNmMinSize)) {
        NXLOG_W("hid", "Joy-Con 2 mouse: the Nmouse block at %llx is no longer mapped; sampling it stopped", static_cast<unsigned long long>(s.nm_addr));
        s.nm_addr = s.nm_size = 0;
        g_jc2_listed.store(false);
        g_jc2_active.store(false);
    }
    if (s.jcms_addr && !mem::is_mapped(s.jcms_addr, kJcmsMinSize)) {
        NXLOG_W("hid", "Joy-Con 2 mouse: the Jcms block at %llx is no longer mapped; sampling it stopped", static_cast<unsigned long long>(s.jcms_addr));
        s.jcms_addr = s.jcms_size = 0;
    }
    if (!s.nm_addr || !g_jc2_listed.load()) return;
    const bool active = nxrt::hid::jc2_mouse_active();
    NmouseInternal ns{};
    const int32_t prev_x = s.nm_x, prev_y = s.nm_y;
    float wx = 0, wy = 0;
    float u = 0, v = 0;
    uint32_t buttons = 0;
    {
        Jc2Feed& f = feed();
        std::lock_guard<std::mutex> lk(f.mu);
        u = std::clamp(f.m.u, 0.f, 1.f);
        v = std::clamp(f.m.v, 0.f, 1.f);
        buttons = f.m.buttons;
        wx = f.wheel_x;
        wy = f.wheel_y;
        if (active) f.wheel_x = f.wheel_y = 0;
    }
    if (active) {
        const uint64_t space = g_jc2_space.load(std::memory_order_relaxed);
        const int32_t pw = space ? static_cast<int32_t>(space >> 32) : s.nm_w, ph = space ? static_cast<int32_t>(space & 0xffffffffu) : s.nm_h;
        const int32_t x = std::min(pw - 1, static_cast<int32_t>(u * static_cast<float>(pw)));
        const int32_t y = std::min(ph - 1, static_cast<int32_t>(v * static_cast<float>(ph)));
        ns.x = x;
        ns.y = y;
        s.nm_x = x;
        s.nm_y = y;
        ns.wheel_x = static_cast<int32_t>(std::lround(wx * 120.f));
        ns.wheel_y = static_cast<int32_t>(std::lround(wy * 120.f));
        ns.buttons = buttons;
        ns.attributes = kNmAttrConnected;
    } else {
        ns.x = std::max(0, s.nm_x);
        ns.y = std::max(0, s.nm_y);
    }
    ns.range_w = s.nm_w;
    ns.range_h = s.nm_h;
    if (active != g_jc2_active.exchange(active) && s.active_logs < 200) {
        s.active_logs++;
        NXLOG_I("hid", "Joy-Con 2 mouse %s (mode %s)", active ? "ON: the game sees the PC mouse" : "off", nxrt::hid::jc2_mouse_mode_name(nxrt::hid::jc2_mouse_mode()));
    }
    uint8_t* b = reinterpret_cast<uint8_t*>(s.nm_addr);
    for (uint32_t i = 0; i < 2; ++i) ring_push(ring_at<NmouseInternal>(b, kNmRings + i * kNmRingSize), ns);
    const bool moved = active && (ns.x != prev_x || ns.y != prev_y);
    if (active && s.nm_logged < 3 && (moved || ns.buttons || ns.wheel_x || ns.wheel_y)) {
        s.nm_logged++;
        NXLOG_I("hid", "Joy-Con 2 mouse sample %u: pos %d,%d range %dx%d buttons %x wheel %d,%d attr %x (block %llx)", s.nm_logged, ns.x, ns.y,
                ns.range_w, ns.range_h, ns.buttons, ns.wheel_x, ns.wheel_y, ns.attributes, static_cast<unsigned long long>(s.nm_addr));
    }
    if (s.jcms_addr) {
        // Player 1's Jcms states (index npad * 2 + type: 0 left, 1 right) keep counting so GetJcmsState has a current entry.
        for (uint32_t i = 0; i < 2; ++i) ring_push(ring_at<JcmsState>(reinterpret_cast<uint8_t*>(s.jcms_addr), i * kJcmsRingSize), JcmsState{});
    }
}

// A Joy-Con 2 (R) used as a mouse clicks with its own R (left button) and ZR (right button), which a game reads from the npad, not from the
// Nmouse buttons. As in Pikantine (github.com/SpiderNic96/Pikantine, GPL-3.0, src/hid_core/resources/npad/npad.cpp): while the mouse is on
// its surface, the fed left and right buttons are player 1's R and ZR. Play mode feeds buttons only while they belong to the device
// (play_events.cpp jc2_owns_clicks), and then keeps them from its bindings, so a click is never also the binding's button.
uint64_t jc2_pad_buttons() {
    if (!nxrt::hid::jc2_mouse_active()) return 0;
    uint32_t b = 0;
    {
        Jc2Feed& f = feed();
        std::lock_guard<std::mutex> lk(f.mu);
        b = f.m.buttons;
    }
    using namespace nxrt::hid;
    return (b & 1u ? btn(kBtnR) : 0) | (b & 2u ? btn(kBtnZR) : 0);
}

// The guest buffer behind a transfer memory handle, or 0 when it is unusable for `need` bytes.
uint64_t tm_addr(const ipc::Request& req, uint64_t size, uint64_t need, const char* what) {
    if (req.in_copy.empty()) {
        NXLOG_W("hid", "%s: no transfer memory handle", what);
        return 0;
    }
    auto tm = kernel::handle_as<kernel::KTransferMemory>(req.in_copy[0]);
    if (!tm || !tm->addr) {
        NXLOG_W("hid", "%s: handle %x is not a transfer memory", what, req.in_copy[0]);
        return 0;
    }
    const uint64_t n = std::min(size, tm->size);
    if (n < need || !mem::is_mapped(tm->addr, need)) {
        NXLOG_W("hid", "%s: transfer memory %llx size %llx (request %llx) is smaller than %llx or unmapped", what,
                static_cast<unsigned long long>(tm->addr), static_cast<unsigned long long>(tm->size), static_cast<unsigned long long>(size),
                static_cast<unsigned long long>(need));
        return 0;
    }
    return tm->addr;
}

// InitializeNmouse (3139) and its NmouseStateRange form (3130) share one layout: u8 pointer type @0, s32 width @4, s32 height @8,
// aruid @0x10, size @0x18 and the transfer memory handle (3139 adds a u32, always 1, at @0xC).
uint32_t nm_initialize(ipc::Request& req, const char* what) {
    const uint32_t type = req.arg<uint8_t>(0);
    const int32_t w = req.arg<int32_t>(4), h = req.arg<int32_t>(8);
    const uint64_t size = req.arg<uint64_t>(0x18);
    std::lock_guard<std::mutex> lk(st().mu);
    HidState& s = st();
    s.nm_w = w > 0 ? w : 1920;
    s.nm_h = h > 0 ? h : 1080;
    s.nm_addr = tm_addr(req, size, kNmMinSize, what);
    s.nm_size = s.nm_addr ? size : 0;
    NXLOG_I("hid", "%s(pointer type %u, range %dx%d, size %llx) -> block %llx", what, type, w, h, static_cast<unsigned long long>(size),
            static_cast<unsigned long long>(s.nm_addr));
    if (s.nm_addr) nm_setup(s);
    return 0;
}

}  // namespace nxrt::services::hidsvc

// ---- Joy-Con 2 mouse: the play layer's side (include/nxrt/jc2_mouse.h) -------------------------------------------------------------------
namespace nxrt::hid {

Jc2MouseMode jc2_mouse_mode() {
    int m = services::hidsvc::g_jc2_mode.load();
    if (m < 0) {
        std::string v = config_get_str("input", "jc2_mouse", "auto");
        v = nxrt::text::lower(std::move(v));
        m = v == "off" ? 0 : (v == "on" ? 2 : 1);
        if (v != "off" && v != "on" && v != "auto") NXLOG_W("hid", "[input] jc2_mouse = %s: not auto, on or off; using auto", v.c_str());
        services::hidsvc::g_jc2_mode.store(m);
    }
    return static_cast<Jc2MouseMode>(m);
}

void jc2_mouse_set_mode(Jc2MouseMode m) {
    services::hidsvc::g_jc2_mode.store(static_cast<int>(m));
}

const char* jc2_mouse_mode_name(Jc2MouseMode m) {
    return m == Jc2MouseMode::Off ? "off" : (m == Jc2MouseMode::On ? "on" : "auto");
}

void jc2_mouse_feed(const HostMouse& m) {
    services::hidsvc::Jc2Feed& f = services::hidsvc::feed();
    std::lock_guard<std::mutex> lk(f.mu);
    f.m = m;
    f.wheel_x += m.wheel_x;
    f.wheel_y += m.wheel_y;
    f.last_ns = services::hidsvc::now_ns();
}

void jc2_mouse_request(bool want) {
    services::hidsvc::g_jc2_request.store(want);
}

bool jc2_mouse_wants_pointer() {
    const Jc2MouseMode m = jc2_mouse_mode();
    if (m == Jc2MouseMode::Off) return false;
    if (m == Jc2MouseMode::On) return services::hidsvc::g_jc2_listed.load();
    return services::hidsvc::g_jc2_listed.load() && services::hidsvc::g_jc2_request.load();
}

void jc2_mouse_set_pointer_space(int w, int h) {
    services::hidsvc::g_jc2_space.store(w > 0 && h > 0 && w <= 16384 && h <= 16384 ? (static_cast<uint64_t>(w) << 32) | static_cast<uint32_t>(h) : 0,
                                std::memory_order_relaxed);
}

bool jc2_mouse_active() {
    return jc2_mouse_mode() != Jc2MouseMode::Off && services::hidsvc::g_jc2_listed.load() && services::hidsvc::jc2_feed_live();
}

}  // namespace nxrt::hid
