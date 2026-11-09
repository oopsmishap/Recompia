// SPDX-License-Identifier: GPL-3.0-or-later
// macOS display HDR query (win_display_posix.cpp), the counterpart of win_display.cpp. Same contract, same struct: the
// caller (src/gfx/vk_renderer.cpp) asks whether the monitor under the window has HDR on and what its luminance numbers
// are, and only logs the answer.
//
// Windows answers through the CCD database and DXGI_OUTPUT_DESC1. macOS has the same numbers through CoreGraphics:
// a display's EDR headroom is what the OS allows the layer to exceed SDR white by, and CGDisplayMode gives the pixel
// encoding. What is missing is the *user's* "SDR content brightness" slider, which Windows reports directly; on macOS
// that is not exposed, so sdr_white_nits stays at its default and the caller's tone mapping uses the headroom ratio.
#include "extras/win_display.h"

#if !defined(_WIN32)

#include <stdio.h>
#include <string.h>

#if defined(__APPLE__)
#include <CoreGraphics/CoreGraphics.h>
#endif

namespace nxrt::extras {

bool query_display_hdr(void* hwnd, DisplayHdrInfo* out) {
    if (!out) return false;
    *out = DisplayHdrInfo{};
#if defined(__APPLE__)
    (void)hwnd;
    // The main display is the one a single-window game is on; a second monitor's headroom would need the NSScreen the
    // window is on, which SDL exposes only as the window's display index.
    CGDirectDisplayID display = CGMainDisplayID();
    out->valid = true;

    if (CGDisplayIsBuiltin(display)) {
        snprintf(out->monitor, sizeof out->monitor, "builtin");
    } else {
        snprintf(out->monitor, sizeof out->monitor, "display-%u", static_cast<unsigned>(display));
    }

    // A wide-gamut mode with a pixel encoding that supports more than SDR white is what "HDR on" means on macOS; the
    // EDR headroom is then the factor the compositor allows above SDR white.
    CFStringRef name = CGDisplayName(display);
    if (name) {
        char buf[128] = {};
        CFStringGetCString(name, buf, sizeof buf, kCFStringEncodingUTF8);
        snprintf(out->how, sizeof out->how, "CoreGraphics: %s", buf);
        CFRelease(name);
    } else {
        snprintf(out->how, sizeof out->how, "CoreGraphics");
    }
    CGDisplayModeRef mode = CGDisplayCopyDisplayMode(display);
    if (mode) {
        const bool wide = CGDisplayModeUsesExtendedRange(mode);
        out->hdr_enabled = wide;
        if (wide) {
            // Extended range: the panel can exceed SDR white. Without the EDR headroom the compositor reports (which
            // needs the window's CAMetalLayer), 1000 nits is the peak Apple documents for an XDR panel and 80 nits the
            // SDR reference. vk_renderer.cpp only logs these.
            out->max_nits = 1000.0f;
            out->max_fullframe_nits = 1000.0f;
        }
        CGDisplayModeRelease(mode);
    }
    out->min_nits = 0.0f;
    out->sdr_white_nits = 80.0f;
    return true;
#else
    (void)hwnd;
    return false;
#endif
}

}  // namespace nxrt::extras

#endif  // !_WIN32
