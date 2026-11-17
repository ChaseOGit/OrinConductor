// Linux desktop SDL3 frontend for OrinNative:
// - Creates SDL3 Vulkan window and feeds it to vk_android's nxhost_set_window()
// - Ensures X11 InputFocus on pure Xorg servers running without a window manager
// - Provides SDL_Vulkan_GetInstanceExtensions and SDL_Vulkan_CreateSurface to vka_dev / vka_present
// - Event-driven keyboard, gamepad (via pad_slots), and mouse/touch input with 2-frame tap pulse floor
//   so short synthetic taps (xdotool / XTest) and multi-reader games (Workload-A) never drop an edge
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#define SDL_MAIN_HANDLED
#include "dm.h"
#include "hle.h"
#include "front.h"
#include "dm_input.h"
#include "pad_slots.h"
#include "../gfx/vk_android/vk_android.h"
#include <SDL3/SDL.h>
#include <SDL3/SDL_vulkan.h>
#include <X11/Xlib.h>
#include <vulkan/vulkan_core.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static SDL_Window *g_win = NULL;
static pthread_mutex_t g_win_mu = PTHREAD_MUTEX_INITIALIZER;
static _Atomic int g_win_w = 1280, g_win_h = 720;
static _Atomic int g_guest_done = 0;

enum { TAP_HOLD_NS = 36000000ULL };   // ~2.1 frames at 60 Hz so multi-reader games all observe short taps

static bool g_kb_down[SDL_SCANCODE_COUNT];
static uint64_t g_kb_until[SDL_SCANCODE_COUNT];
static uint64_t g_kb_last_btn = UINT64_MAX;
static int32_t g_kb_last_axes[4];
static int g_input_log = -1;

typedef struct {
    SDL_JoystickID jid;
    SDL_Gamepad *gp;
    bool btn_down[SDL_GAMEPAD_BUTTON_COUNT];
    uint64_t btn_until[SDL_GAMEPAD_BUTTON_COUNT];
    uint64_t last_btn;
    int32_t last_axes[4];
} linux_pad_t;

static pad_slots g_pad_slots;
static linux_pad_t g_pads[DM_PLAYERS];

static const void *pad_owner(SDL_JoystickID jid) {
    return (const void *)(uintptr_t)((uint64_t)(uint32_t)jid + 0x100000000ULL);
}

static int want_input_log(void) {
    if (g_input_log < 0) {
        const char *e = getenv("DM_INPUT_LOG");
        g_input_log = (e && atoi(e)) ? 1 : 0;
    }
    return g_input_log;
}

static int x11_ignore_err(Display *d, XErrorEvent *e) {
    (void)d; (void)e;
    return 0;
}

static void ensure_x11_focus(SDL_Window *win, bool force) {
    if (!win) return;
    SDL_PropertiesID props = SDL_GetWindowProperties(win);
    Display *dpy = (Display *)SDL_GetPointerProperty(props, SDL_PROP_WINDOW_X11_DISPLAY_POINTER, NULL);
    Window xwin = (Window)SDL_GetNumberProperty(props, SDL_PROP_WINDOW_X11_WINDOW_NUMBER, 0);
    if (!dpy || !xwin) return;

    int (*old_handler)(Display *, XErrorEvent *) = XSetErrorHandler(x11_ignore_err);
    Window cur = None;
    int revert = 0;
    XGetInputFocus(dpy, &cur, &revert);
    // On pure Xorg without a window manager, cur stays PointerRoot or None so SDL3 never gets FocusIn
    // unless we explicitly assign InputFocus to our window. Under a real WM, cur is an application window,
    // so !force avoids stealing focus from other windows.
    if (force || cur == None || cur == PointerRoot) {
        XWindowAttributes attr;
        if (XGetWindowAttributes(dpy, xwin, &attr) && attr.map_state == IsViewable) {
            if (force) XRaiseWindow(dpy, xwin);
            XSetInputFocus(dpy, xwin, RevertToPointerRoot, CurrentTime);
            if (force) {
                int ww = atomic_load_explicit(&g_win_w, memory_order_relaxed);
                int wh = atomic_load_explicit(&g_win_h, memory_order_relaxed);
                XWarpPointer(dpy, None, xwin, 0, 0, 0, 0, ww > 0 ? ww / 2 : 640, wh > 0 ? wh / 2 : 360);
            }
            XFlush(dpy);
            static int logged_focus;
            if (!logged_focus++) {
                LOG("front end: X11 InputFocus set to window 0x%lx (was 0x%lx)", (unsigned long)xwin, (unsigned long)cur);
            }
        }
    }
    XSetErrorHandler(old_handler);
}

int dm_linux_window_size(struct ANativeWindow *w, int *width, int *height) {
    (void)w;
    int ww = atomic_load_explicit(&g_win_w, memory_order_relaxed);
    int wh = atomic_load_explicit(&g_win_h, memory_order_relaxed);
    if (width) *width = ww > 0 ? ww : 1280;
    if (height) *height = wh > 0 ? wh : 720;
    return 0;
}

const char * const *dm_linux_vk_instance_exts(uint32_t *count) {
    Uint32 n = 0;
    const char * const *exts = SDL_Vulkan_GetInstanceExtensions(&n);
    if (count) *count = (uint32_t)n;
    return exts;
}

VkResult dm_linux_vk_create_surface(struct ANativeWindow *w, VkInstance inst, VkSurfaceKHR *out) {
    SDL_Window *win = (SDL_Window *)w;
    if (!win) win = g_win;
    if (!win || !out) return VK_ERROR_INITIALIZATION_FAILED;
    if (!SDL_Vulkan_CreateSurface(win, inst, NULL, out)) {
        LOG("[RED] SDL_Vulkan_CreateSurface failed: %s", SDL_GetError());
        return VK_ERROR_SURFACE_LOST_KHR;
    }
    int pw = 0, ph = 0;
    if (SDL_GetWindowSizeInPixels(win, &pw, &ph) && pw > 0 && ph > 0) {
        atomic_store_explicit(&g_win_w, pw, memory_order_relaxed);
        atomic_store_explicit(&g_win_h, ph, memory_order_relaxed);
    }
    return VK_SUCCESS;
}

static inline bool sc_active(SDL_Scancode sc, const bool *ks, int numkeys, uint64_t t_ns) {
    if ((int)sc < 0 || (int)sc >= SDL_SCANCODE_COUNT) return false;
    bool sdl_down = (ks && (int)sc < numkeys) ? ks[sc] : false;
    return g_kb_down[sc] || sdl_down || (t_ns < g_kb_until[sc]);
}

static void publish_keyboard(uint64_t t_ns) {
    int numkeys = 0;
    const bool *ks = SDL_GetKeyboardState(&numkeys);
    uint64_t kb_btn = 0;
    int32_t kb_lx = 0, kb_ly = 0, kb_rx = 0, kb_ry = 0;

    if (sc_active(SDL_SCANCODE_Z, ks, numkeys, t_ns) ||
        sc_active(SDL_SCANCODE_RETURN, ks, numkeys, t_ns) ||
        sc_active(SDL_SCANCODE_KP_ENTER, ks, numkeys, t_ns) ||
        sc_active(SDL_SCANCODE_SPACE, ks, numkeys, t_ns)) kb_btn |= DM_BTN_A;
    if (sc_active(SDL_SCANCODE_X, ks, numkeys, t_ns) ||
        sc_active(SDL_SCANCODE_BACKSPACE, ks, numkeys, t_ns) ||
        sc_active(SDL_SCANCODE_ESCAPE, ks, numkeys, t_ns)) kb_btn |= DM_BTN_B;
    if (sc_active(SDL_SCANCODE_C, ks, numkeys, t_ns)) kb_btn |= DM_BTN_X;
    if (sc_active(SDL_SCANCODE_V, ks, numkeys, t_ns)) kb_btn |= DM_BTN_Y;
    if (sc_active(SDL_SCANCODE_F, ks, numkeys, t_ns)) kb_btn |= DM_BTN_LS;
    if (sc_active(SDL_SCANCODE_G, ks, numkeys, t_ns)) kb_btn |= DM_BTN_RS;
    if (sc_active(SDL_SCANCODE_Q, ks, numkeys, t_ns)) kb_btn |= DM_BTN_L;
    if (sc_active(SDL_SCANCODE_E, ks, numkeys, t_ns)) kb_btn |= DM_BTN_R;
    if (sc_active(SDL_SCANCODE_1, ks, numkeys, t_ns)) kb_btn |= DM_BTN_ZL;
    if (sc_active(SDL_SCANCODE_3, ks, numkeys, t_ns)) kb_btn |= DM_BTN_ZR;
    if (sc_active(SDL_SCANCODE_TAB, ks, numkeys, t_ns) ||
        sc_active(SDL_SCANCODE_MINUS, ks, numkeys, t_ns)) kb_btn |= DM_BTN_MINUS;
    if (sc_active(SDL_SCANCODE_M, ks, numkeys, t_ns) ||
        sc_active(SDL_SCANCODE_EQUALS, ks, numkeys, t_ns)) kb_btn |= DM_BTN_PLUS;
    if (sc_active(SDL_SCANCODE_LEFT, ks, numkeys, t_ns))  kb_btn |= DM_BTN_LEFT;
    if (sc_active(SDL_SCANCODE_RIGHT, ks, numkeys, t_ns)) kb_btn |= DM_BTN_RIGHT;
    if (sc_active(SDL_SCANCODE_UP, ks, numkeys, t_ns))    kb_btn |= DM_BTN_UP;
    if (sc_active(SDL_SCANCODE_DOWN, ks, numkeys, t_ns))  kb_btn |= DM_BTN_DOWN;

    if (sc_active(SDL_SCANCODE_A, ks, numkeys, t_ns)) kb_lx -= DM_STICK_MAX;
    if (sc_active(SDL_SCANCODE_D, ks, numkeys, t_ns)) kb_lx += DM_STICK_MAX;
    if (sc_active(SDL_SCANCODE_W, ks, numkeys, t_ns)) kb_ly += DM_STICK_MAX;
    if (sc_active(SDL_SCANCODE_S, ks, numkeys, t_ns)) kb_ly -= DM_STICK_MAX;

    if (sc_active(SDL_SCANCODE_J, ks, numkeys, t_ns)) kb_rx -= DM_STICK_MAX;
    if (sc_active(SDL_SCANCODE_L, ks, numkeys, t_ns)) kb_rx += DM_STICK_MAX;
    if (sc_active(SDL_SCANCODE_I, ks, numkeys, t_ns)) kb_ry += DM_STICK_MAX;
    if (sc_active(SDL_SCANCODE_K, ks, numkeys, t_ns)) kb_ry -= DM_STICK_MAX;

    if (kb_btn != g_kb_last_btn ||
        kb_lx != g_kb_last_axes[0] || kb_ly != g_kb_last_axes[1] ||
        kb_rx != g_kb_last_axes[2] || kb_ry != g_kb_last_axes[3]) {
        static int first_kb_logged;
        if (kb_btn && (!first_kb_logged || want_input_log())) {
            first_kb_logged = 1;
            LOG("input: keyboard state btn=%#llx L=(%d,%d) R=(%d,%d)",
                (unsigned long long)kb_btn, kb_lx, kb_ly, kb_rx, kb_ry);
        }
        g_kb_last_btn = kb_btn;
        g_kb_last_axes[0] = kb_lx; g_kb_last_axes[1] = kb_ly;
        g_kb_last_axes[2] = kb_rx; g_kb_last_axes[3] = kb_ry;
        dm_input_set(kb_btn, kb_lx, kb_ly, kb_rx, kb_ry);
    }
}

static float pad_deadzone(void) {
    static float dz = -1.0f;
    if (dz < 0.0f) {
        const char *e = getenv("DM_PAD_DEADZONE");
        dz = e ? strtof(e, NULL) : 0.10f;
        if (!(dz >= 0.0f && dz <= 0.90f)) dz = 0.10f;
    }
    return dz;
}

static void apply_stick_deadzone(int32_t raw_x, int32_t raw_y, int32_t *out_x, int32_t *out_y) {
    float fx = (float)raw_x / 32767.0f;
    float fy = (float)raw_y / 32767.0f;
    float mag = hypotf(fx, fy);
    float dz = pad_deadzone();
    if (mag <= dz) {
        *out_x = 0;
        *out_y = 0;
        return;
    }
    float scale = (mag > 1.0f ? 1.0f : (mag - dz) / (1.0f - dz)) / mag;
    int32_t sx = (int32_t)lrintf(fx * scale * (float)DM_STICK_MAX);
    int32_t sy = (int32_t)lrintf(fy * scale * (float)DM_STICK_MAX);
    if (sx < -DM_STICK_MAX) sx = -DM_STICK_MAX; else if (sx > DM_STICK_MAX) sx = DM_STICK_MAX;
    if (sy < -DM_STICK_MAX) sy = -DM_STICK_MAX; else if (sy > DM_STICK_MAX) sy = DM_STICK_MAX;
    *out_x = sx;
    *out_y = sy;
}

static inline bool gp_btn_active(const linux_pad_t *lp, SDL_GamepadButton b, uint64_t t_ns) {
    if ((int)b < 0 || (int)b >= SDL_GAMEPAD_BUTTON_COUNT) return false;
    bool sdl_down = lp->gp ? SDL_GetGamepadButton(lp->gp, b) : false;
    return lp->btn_down[b] || sdl_down || (t_ns < lp->btn_until[b]);
}

static void publish_pad_slot(int slot, uint64_t t_ns) {
    if (slot < 0 || slot >= DM_PLAYERS) return;
    linux_pad_t *lp = &g_pads[slot];
    if (!lp->gp) return;

    static int swap_abxy = -1;
    if (swap_abxy < 0) {
        const char *e = getenv("DM_PAD_SWAP_ABXY");
        swap_abxy = (e && atoi(e)) ? 1 : 0;
    }

    uint64_t b = 0;
    if (swap_abxy) {
        if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_SOUTH, t_ns)) b |= DM_BTN_A;
        if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_EAST, t_ns))  b |= DM_BTN_B;
        if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_WEST, t_ns))  b |= DM_BTN_X;
        if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_NORTH, t_ns)) b |= DM_BTN_Y;
    } else {
        if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_EAST, t_ns))  b |= DM_BTN_A;
        if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_SOUTH, t_ns)) b |= DM_BTN_B;
        if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_NORTH, t_ns)) b |= DM_BTN_X;
        if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_WEST, t_ns))  b |= DM_BTN_Y;
    }
    if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_LEFT_STICK, t_ns))     b |= DM_BTN_LS;
    if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_RIGHT_STICK, t_ns))    b |= DM_BTN_RS;
    if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_LEFT_SHOULDER, t_ns))  b |= DM_BTN_L;
    if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER, t_ns)) b |= DM_BTN_R;
    if (SDL_GetGamepadAxis(lp->gp, SDL_GAMEPAD_AXIS_LEFT_TRIGGER) > 8000)  b |= DM_BTN_ZL;
    if (SDL_GetGamepadAxis(lp->gp, SDL_GAMEPAD_AXIS_RIGHT_TRIGGER) > 8000) b |= DM_BTN_ZR;
    if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_START, t_ns)) b |= DM_BTN_PLUS;
    if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_BACK, t_ns))  b |= DM_BTN_MINUS;
    if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_DPAD_LEFT, t_ns))  b |= DM_BTN_LEFT;
    if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_DPAD_UP, t_ns))    b |= DM_BTN_UP;
    if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_DPAD_RIGHT, t_ns)) b |= DM_BTN_RIGHT;
    if (gp_btn_active(lp, SDL_GAMEPAD_BUTTON_DPAD_DOWN, t_ns))  b |= DM_BTN_DOWN;

    int32_t raw_lx = SDL_GetGamepadAxis(lp->gp, SDL_GAMEPAD_AXIS_LEFTX);
    int32_t raw_ly = -(int32_t)SDL_GetGamepadAxis(lp->gp, SDL_GAMEPAD_AXIS_LEFTY);
    int32_t raw_rx = SDL_GetGamepadAxis(lp->gp, SDL_GAMEPAD_AXIS_RIGHTX);
    int32_t raw_ry = -(int32_t)SDL_GetGamepadAxis(lp->gp, SDL_GAMEPAD_AXIS_RIGHTY);
    int32_t lx = 0, ly = 0, rx = 0, ry = 0;
    apply_stick_deadzone(raw_lx, raw_ly, &lx, &ly);
    apply_stick_deadzone(raw_rx, raw_ry, &rx, &ry);

    if (b != lp->last_btn ||
        lx != lp->last_axes[0] || ly != lp->last_axes[1] ||
        rx != lp->last_axes[2] || ry != lp->last_axes[3]) {
        static int first_gp_logged;
        if (b && (!first_gp_logged || want_input_log())) {
            first_gp_logged = 1;
            LOG("input: gamepad player %d btn=%#llx L=(%d,%d) R=(%d,%d)",
                slot + 1, (unsigned long long)b, lx, ly, rx, ry);
        }
        lp->last_btn = b;
        lp->last_axes[0] = lx; lp->last_axes[1] = ly;
        lp->last_axes[2] = rx; lp->last_axes[3] = ry;
        dm_pad_set_player(slot, b, lx, ly, rx, ry);
    }
}

static void pad_add(SDL_JoystickID jid) {
    if (pad_slot_of(&g_pad_slots, pad_owner(jid)) >= 0) return;
    SDL_Gamepad *gp = SDL_OpenGamepad(jid);
    if (!gp) return;
    const char *name = SDL_GetGamepadName(gp);
    int slot = pad_slot_take(&g_pad_slots, pad_owner(jid), name);
    if (slot < 0) {
        LOG("[RED] input: gamepad %u (%s): all %d player slots are full", (unsigned)jid, name ? name : "unnamed", DM_PLAYERS);
        SDL_CloseGamepad(gp);
        return;
    }
    memset(&g_pads[slot], 0, sizeof g_pads[slot]);
    g_pads[slot].jid = jid;
    g_pads[slot].gp = gp;
    g_pads[slot].last_btn = UINT64_MAX;
    dm_pad_players(pad_slot_mask(&g_pad_slots));
    publish_pad_slot(slot, now_ns());
    LOG("input: gamepad %u (%s) connected as player %d", (unsigned)jid, name && *name ? name : "unnamed", slot + 1);
}

static void pad_remove(SDL_JoystickID jid) {
    int slot = pad_slot_free(&g_pad_slots, pad_owner(jid));
    if (slot < 0) return;
    if (g_pads[slot].gp) SDL_CloseGamepad(g_pads[slot].gp);
    memset(&g_pads[slot], 0, sizeof g_pads[slot]);
    dm_pad_set_player(slot, 0, 0, 0, 0, 0);
    dm_pad_players(pad_slot_mask(&g_pad_slots));
    LOG("input: gamepad %u (player %d) disconnected", (unsigned)jid, slot + 1);
}

static void init_connected_gamepads(void) {
    int npads = 0;
    SDL_JoystickID *jids = SDL_GetGamepads(&npads);
    for (int i = 0; i < npads; i++) pad_add(jids[i]);
    if (jids) SDL_free(jids);
}

static void *guest_thread_entry(void *arg) {
    void (*guest)(void) = (void (*)(void))arg;
    guest();
    atomic_store(&g_guest_done, 1);
    return NULL;
}

void front_run(void (*guest)(void)) {
    const char *disp = getenv("DISPLAY");
    const char *wld = getenv("WAYLAND_DISPLAY");
    const char *headless = getenv("DM_HEADLESS");
    if ((!disp || !*disp) && (!wld || !*wld)) headless = "1";

    if (!headless || !atoi(headless)) {
        SDL_SetHint(SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS, "1");
        if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO | SDL_INIT_GAMEPAD | SDL_INIT_EVENTS)) {
            int w = 1280, h = 720;
            const char *ws = getenv("DM_WINDOW_SIZE");
            if (ws) sscanf(ws, "%dx%d", &w, &h);
            pthread_mutex_lock(&g_win_mu);
            SDL_PropertiesID props = SDL_CreateProperties();
            SDL_SetStringProperty(props, SDL_PROP_WINDOW_CREATE_TITLE_STRING, "OrinNative - Switch 2");
            SDL_SetNumberProperty(props, SDL_PROP_WINDOW_CREATE_X_NUMBER, 0);
            SDL_SetNumberProperty(props, SDL_PROP_WINDOW_CREATE_Y_NUMBER, 0);
            SDL_SetNumberProperty(props, SDL_PROP_WINDOW_CREATE_WIDTH_NUMBER, w);
            SDL_SetNumberProperty(props, SDL_PROP_WINDOW_CREATE_HEIGHT_NUMBER, h);
            SDL_SetBooleanProperty(props, SDL_PROP_WINDOW_CREATE_VULKAN_BOOLEAN, true);
            SDL_SetBooleanProperty(props, SDL_PROP_WINDOW_CREATE_RESIZABLE_BOOLEAN, true);
            SDL_SetBooleanProperty(props, SDL_PROP_WINDOW_CREATE_FOCUSABLE_BOOLEAN, true);
            g_win = SDL_CreateWindowWithProperties(props);
            SDL_DestroyProperties(props);
            if (g_win) {
                SDL_SetWindowPosition(g_win, 0, 0);
                SDL_ShowWindow(g_win);
                SDL_RaiseWindow(g_win);
                ensure_x11_focus(g_win, true);
            }
            pthread_mutex_unlock(&g_win_mu);
            if (g_win) {
                int pw = w, ph = h;
                if (SDL_GetWindowSizeInPixels(g_win, &pw, &ph) && pw > 0 && ph > 0) {
                    atomic_store(&g_win_w, pw);
                    atomic_store(&g_win_h, ph);
                }
                LOG("front end: SDL3 Vulkan window %p (%dx%d)", (void *)g_win, atomic_load(&g_win_w), atomic_load(&g_win_h));
                nxhost_set_window((struct ANativeWindow *)g_win);
                dm_focus_set(1);
                init_connected_gamepads();
            } else {
                LOG("[RED] front end: SDL_CreateWindow failed (%s), running headless", SDL_GetError());
            }
        } else {
            LOG("[RED] front end: SDL_Init failed (%s), running headless", SDL_GetError());
        }
    } else {
        LOG("front end: headless mode (no window)");
    }

    if (!g_win) {
        guest();
        return;
    }

    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 8 << 20);
    pthread_t th;
    if (pthread_create(&th, &attr, guest_thread_entry, (void *)guest)) {
        pthread_attr_destroy(&attr);
        die("front_run: cannot start guest thread");
    }
    pthread_attr_destroy(&attr);

    bool mouse_down = false;
    uint64_t last_focus_chk_ns = 0;
    while (!atomic_load(&g_guest_done)) {
        SDL_Event ev;
        while (SDL_PollEvent(&ev)) {
            uint64_t t_ns = now_ns();
            switch (ev.type) {
            case SDL_EVENT_QUIT:
                LOG("front end: SDL_EVENT_QUIT received, exiting");
                nxhost_set_window(NULL);
                _exit(0);
                break;
            case SDL_EVENT_WINDOW_SHOWN:
            case SDL_EVENT_WINDOW_EXPOSED:
                ensure_x11_focus(g_win, false);
                break;
            case SDL_EVENT_WINDOW_FOCUS_GAINED:
                dm_focus_set(1);
                break;
            case SDL_EVENT_WINDOW_MINIMIZED:
                memset(g_kb_down, 0, sizeof g_kb_down);
                memset(g_kb_until, 0, sizeof g_kb_until);
                publish_keyboard(t_ns);
                dm_focus_set(3);
                break;
            case SDL_EVENT_WINDOW_RESTORED:
                dm_focus_set(1);
                ensure_x11_focus(g_win, false);
                break;
            case SDL_EVENT_WINDOW_PIXEL_SIZE_CHANGED:
            case SDL_EVENT_WINDOW_RESIZED: {
                int pw = 0, ph = 0;
                if (SDL_GetWindowSizeInPixels(g_win, &pw, &ph) && pw > 0 && ph > 0) {
                    atomic_store(&g_win_w, pw);
                    atomic_store(&g_win_h, ph);
                }
                break;
            }
            case SDL_EVENT_KEY_DOWN:
                if ((int)ev.key.scancode >= 0 && (int)ev.key.scancode < SDL_SCANCODE_COUNT) {
                    g_kb_down[ev.key.scancode] = true;
                    if (!ev.key.repeat) g_kb_until[ev.key.scancode] = t_ns + TAP_HOLD_NS;
                    publish_keyboard(t_ns);
                }
                break;
            case SDL_EVENT_KEY_UP:
                if ((int)ev.key.scancode >= 0 && (int)ev.key.scancode < SDL_SCANCODE_COUNT) {
                    g_kb_down[ev.key.scancode] = false;
                    publish_keyboard(t_ns);
                }
                break;
            case SDL_EVENT_GAMEPAD_ADDED:
                pad_add(ev.gdevice.which);
                break;
            case SDL_EVENT_GAMEPAD_REMOVED:
                pad_remove(ev.gdevice.which);
                break;
            case SDL_EVENT_GAMEPAD_BUTTON_DOWN: {
                int slot = pad_slot_of(&g_pad_slots, pad_owner(ev.gbutton.which));
                if (slot >= 0 && ev.gbutton.button < SDL_GAMEPAD_BUTTON_COUNT) {
                    g_pads[slot].btn_down[ev.gbutton.button] = true;
                    g_pads[slot].btn_until[ev.gbutton.button] = t_ns + TAP_HOLD_NS;
                    publish_pad_slot(slot, t_ns);
                }
                break;
            }
            case SDL_EVENT_GAMEPAD_BUTTON_UP: {
                int slot = pad_slot_of(&g_pad_slots, pad_owner(ev.gbutton.which));
                if (slot >= 0 && ev.gbutton.button < SDL_GAMEPAD_BUTTON_COUNT) {
                    g_pads[slot].btn_down[ev.gbutton.button] = false;
                    publish_pad_slot(slot, t_ns);
                }
                break;
            }
            case SDL_EVENT_GAMEPAD_AXIS_MOTION: {
                int slot = pad_slot_of(&g_pad_slots, pad_owner(ev.gaxis.which));
                if (slot >= 0) publish_pad_slot(slot, t_ns);
                break;
            }
            case SDL_EVENT_MOUSE_BUTTON_DOWN:
                if (ev.button.button == SDL_BUTTON_LEFT) {
                    ensure_x11_focus(g_win, true);
                    mouse_down = true;
                    int ww = atomic_load(&g_win_w), wh = atomic_load(&g_win_h);
                    if (ww > 0 && wh > 0) {
                        dm_touch t = {0, ev.button.x / (float)ww, ev.button.y / (float)wh};
                        dm_touch_set(&t, 1);
                    }
                }
                break;
            case SDL_EVENT_MOUSE_MOTION:
                if (mouse_down) {
                    int ww = atomic_load(&g_win_w), wh = atomic_load(&g_win_h);
                    if (ww > 0 && wh > 0) {
                        dm_touch t = {0, ev.motion.x / (float)ww, ev.motion.y / (float)wh};
                        dm_touch_set(&t, 1);
                    }
                }
                break;
            case SDL_EVENT_MOUSE_BUTTON_UP:
                if (ev.button.button == SDL_BUTTON_LEFT) {
                    mouse_down = false;
                    dm_touch_set(NULL, 0);
                }
                break;
            default:
                break;
            }
        }

        uint64_t t_ns = now_ns();
        if (SDL_GetKeyboardFocus() != g_win && t_ns - last_focus_chk_ns > 500000000ULL) {
            last_focus_chk_ns = t_ns;
            ensure_x11_focus(g_win, false);
        }
        publish_keyboard(t_ns);
        for (int s = 0; s < DM_PLAYERS; s++) {
            if (g_pads[s].gp) publish_pad_slot(s, t_ns);
        }
        usleep(4000);
    }
    pthread_join(th, NULL);
}

void *front_metal_layer(void) { return g_win; }
int front_screen_max_fps(void) { return 60; }
int front_screen_variable_refresh(void) { return 0; }
void front_screen_match(int fps) { (void)fps; }
void front_progress(const char *title, long done, long total) {
    if (title) {
        static _Atomic int once;
        if (!atomic_exchange(&once, 1)) LOG("front progress: %s (%ld/%ld)", title, done, total);
    }
}
int front_text_input(const char *title, const char *sub, const char *guide, const char *initial, int max_chars, char *out, int out_cap) {
    (void)title; (void)sub; (void)guide; (void)max_chars;
    if (initial && *initial && out && out_cap > 0) {
        snprintf(out, (size_t)out_cap, "%s", initial);
        return 1;
    }
    return -1;
}
