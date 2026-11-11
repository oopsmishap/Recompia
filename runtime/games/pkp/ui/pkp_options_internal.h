// SPDX-License-Identifier: GPL-3.0-or-later
// In-game Options (pkp_options.cpp and the files it is split into): what a row is, the row lists each tab shows, and the functions one of
// those files calls in another. Internal to those files.
//   pkp_options.cpp        the tabs, the slot state machine and the hooks into the game's Settings screen
//   pkp_options_rows.cpp   the rows of the PC settings tabs (Sound, Display, ... Controller) and of the Gameplay tab
//   pkp_options_time.cpp   the Time and Events tabs: their rows, the event list and the game clock
#pragma once
#include <cstddef>
#include <cstdint>

#include "core/pkp_settings.h"
#include "core/pkp_settings_table.h"
#include "nxrt/abi.h"
#include "play_types.h"

namespace nxrt::games::pkp::options_detail {

using S = play::Settings;

constexpr const char* kTag = "pkp";
constexpr int kSlots = 14;                    // free option ids (0x25..0x32) the tabs share
constexpr int kDirtySlot = kSlots - 1;        // never shown: its saved value differs from its cur while another tab holds changes (the game's own dirty scan sees only the vector)
constexpr int kMaxEvents = kDirtySlot;        // a tab has kDirtySlot rows

struct Row {
    const char* label;
    const char* desc;
    const char* names[4];                 // a choice row: its values
    uint8_t choices;                      // 2..4; 0 = a slider
    uint8_t lo, hi;                       // a slider's range (positions, step 1)
    const char* fmt;                      // a slider's text: fmt of base + position * step ...
    double base, step;
    const char* const* list;              // ... or list[position - lo]
    const char* zero;                     // ... and this at position lo, when set
    const char* (*text)(int, char*, size_t);   // ... or this
    int (*max)();                         // a slider whose last position is known at run time
    int (*get)(const S&);                 // a row of a play-mode setting: its position from S, and S changed to a position
    void (*set)(S&, int);
    bool restart, live;                   // restart: "Applies after a restart." in the description; live: previewed while it moves
    // A row of a [pkp] key instead (key_row below; get and set are null): Bool is Off/On, Int and Double are positions from the key's lo
    // in steps of `unit`. The screen saves it with settings::set_* when it commits (pkp_options.cpp).
    settings::Key key = settings::Key::Count;
    double unit = 0;
};

// clang-format off
constexpr Row choice(const char* label, const char* desc, const char* const (&n)[4], uint8_t count, int (*get)(const S&), void (*set)(S&, int), bool restart = false) {
    return Row{label, desc, {n[0], n[1], n[2], n[3]}, count, 0, 0, nullptr, 0, 0, nullptr, nullptr, nullptr, nullptr, get, set, restart, false};
}
constexpr Row number(const char* label, const char* desc, uint8_t lo, uint8_t hi, const char* fmt, double base, double step, int (*get)(const S&), void (*set)(S&, int), bool live = false) {
    return Row{label, desc, {}, 0, lo, hi, fmt, base, step, nullptr, nullptr, nullptr, nullptr, get, set, false, live};
}
constexpr Row autonum(const char* label, const char* desc, uint8_t hi, const char* fmt, double base, double step, int (*get)(const S&), void (*set)(S&, int), bool restart = false) {
    return Row{label, desc, {}, 0, 0, hi, fmt, base, step, nullptr, "Auto", nullptr, nullptr, get, set, restart, false};
}
constexpr Row names(const char* label, const char* desc, const char* const* list, uint8_t count, int (*get)(const S&), void (*set)(S&, int), bool restart = false) {
    return Row{label, desc, {}, 0, 0, static_cast<uint8_t>(count - 1), nullptr, 0, 0, list, nullptr, nullptr, nullptr, get, set, restart, false};
}
constexpr Row dynamic(const char* label, const char* desc, const char* (*text)(int, char*, size_t), int (*max)(), int (*get)(const S&), void (*set)(S&, int), bool restart = false) {
    return Row{label, desc, {}, 0, 0, 0, nullptr, 0, 0, nullptr, nullptr, text, max, get, set, restart, false};
}
// clang-format on

constexpr const char* kOffOn[4] = {"Off", "On"};

// A row of a [pkp] key (Row::key): its kind, range and restart marker come from the key's Spec (pkp_settings_table.h). Bool keys are an
// Off/On choice. Int and Double keys are a slider over the key's whole range, one position per `unit` (at most 255 positions); its text is
// fmt of value * scale, and `zero` at the key's lowest value when set. An Applies::AtStart key carries the restart marker; a Live key is in
// force as soon as the screen commits (features read it again every frame). String keys have no row kind yet. Used in constexpr row
// tables, so a key that does not fit fails to compile.
constexpr Row key_row(const char* label, const char* desc, settings::Key k, double unit = 1, const char* fmt = nullptr, double scale = 1, const char* zero = nullptr) {
    const settings::Spec& sp = settings::table::at(k);
    Row r{label, desc, {}, 0, 0, 0, nullptr, 0, 0, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, sp.applies == settings::Applies::AtStart, false};
    r.key = k;
    if (sp.type == settings::Type::Bool) {
        r.names[0] = kOffOn[0];
        r.names[1] = kOffOn[1];
        r.choices = 2;
        return r;
    }
    if (sp.type == settings::Type::String || !(sp.lo < sp.hi) || !(unit > 0) || !fmt) throw "key_row: an Int or Double key with a range, a unit and a format";
    const double positions = (sp.hi - sp.lo) / unit;
    if (positions > 255) throw "key_row: more than 255 positions";
    r.hi = static_cast<uint8_t>(positions + 0.5);
    r.unit = unit;
    r.fmt = fmt;
    r.base = sp.lo * scale;
    r.step = unit * scale;
    r.zero = zero;
    return r;
}

// The rows of one tab.
struct RowList {
    const Row* rows;
    int count;
};
template <size_t N>
constexpr RowList rows_of(const Row (&r)[N]) {
    static_assert(N <= kDirtySlot, "a tab has one slot per row and one slot is the dirty marker");
    return RowList{r, static_cast<int>(N)};
}

// ---- pkp_options_rows.cpp ----
extern const RowList kSoundRows, kDisplayRows, kPerformanceRows, kGraphicsRows, kPictureRows, kMouseRows, kControllerRows, kGameplayRows;
void refresh_audio_devices();                 // the Output Device row's list, read again when the screen opens

// ---- pkp_options.cpp: the Mouse Mode Key row (kMouseRows) ----
constexpr int kMmMid = 100;
const char* mouse_mode_text(int, char* out, size_t n);
int mouse_mode_max();

// ---- pkp_options_time.cpp ----
extern const RowList kTimeRows;
extern Row g_event_rows[kMaxEvents];          // the Events tab's rows, filled by load_events
int load_events();                            // reads the event file again and fills g_event_rows; the number of rows the tab shows
void apply_time_tab(const int (&cur)[kSlots]);                           // the Time tab's rows as the game clock (the screen commits)
void go_to_event(int row, int (&time_cur)[kSlots], int (&time_orig)[kSlots]);   // an Events row was pressed; the Time tab's rows follow
int hook_clock_text(::Ctx* c, uint64_t, void*);   // [tweaks] clock_format in the game's time-of-day text

}  // namespace nxrt::games::pkp::options_detail
