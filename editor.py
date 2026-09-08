#!/usr/bin/env python3
"""Interactive rearrange / resize overlay for window-arrange.

GTK4 + gtk4-layer-shell overlay on the focused monitor:

  - Transparent chrome — desktop and live windows stay visible underneath
  - Drag a window body onto another → swap their cells (applied live)
  - Drag edges / corners → resize, pushing abutting neighbors; applied live
    so real windows move under the outline in real time
  - Enter / click Done → quit keeping the live layout
  - Esc / click Cancel → restore the snapshot taken at editor open
  - R → re-snapshot live window positions

Single-instance: Gtk.Application id + ALLOW_REPLACEMENT/REPLACE so a second
Super+B never stacks overlays (stacked editors pegged CPU previously).
The window-arrange launcher also kills any orphan editor.py before start.

Coordinates are Hyprland logical pixels (same space as layout.py).
"""

from __future__ import annotations

import atexit
import math
import os
import sys
from typing import Any

# gtk4-layer-shell must be loaded before libwayland when using Python GI.
# Re-exec once with LD_PRELOAD if needed (no-op when the launcher already set it).
def _ensure_layer_shell_preload() -> None:
    if os.environ.get("WINDOW_ARRANGE_LAYER_PRELOAD") == "1":
        return
    if "gtk4-layer-shell" in (os.environ.get("LD_PRELOAD") or ""):
        os.environ["WINDOW_ARRANGE_LAYER_PRELOAD"] = "1"
        return
    candidates = (
        "/usr/lib/libgtk4-layer-shell.so",
        "/usr/lib/libgtk4-layer-shell.so.0",
        "/usr/lib64/libgtk4-layer-shell.so",
        "/usr/lib64/libgtk4-layer-shell.so.0",
    )
    so = next((c for c in candidates if os.path.isfile(c)), None)
    if not so:
        return
    env = os.environ.copy()
    prev = env.get("LD_PRELOAD", "")
    env["LD_PRELOAD"] = so + (":" + prev if prev else "")
    env["WINDOW_ARRANGE_LAYER_PRELOAD"] = "1"
    os.execve(sys.executable, [sys.executable, *sys.argv], env)


_ensure_layer_shell_preload()

# Repo-local imports (also works when installed next to layout.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from apply import apply_plan, focused_monitor, snapshot_workspace_windows  # noqa: E402
from geometry import (  # noqa: E402
    DEFAULT_GAP,
    DEFAULT_MIN_H,
    DEFAULT_MIN_W,
    HANDLE_PX,
    clone_windows,
    cursor_for_handle,
    detect_row_bands,
    hit_test_handle,
    layout_is_valid,
    rearrange_drop,
    resize_handle,
    window_at,
)
from layout import logical_monitor_box, work_area  # noqa: E402

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402


def _pid_file_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime, "window-arrange", "editor.pid")


def _write_pid_file() -> None:
    path = _pid_file_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        return

    def _clear() -> None:
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    if fh.read().strip() == str(os.getpid()):
                        os.remove(path)
        except OSError:
            pass

    atexit.register(_clear)


def _claim_single_instance() -> None:
    """Drop any other editor.py processes before this overlay maps.

    Defense in depth with the bash launcher: if something started editor.py
    directly (or a previous instance ignored SIGTERM), refuse to stack.
    """
    me = os.getpid()
    here = os.path.abspath(__file__).encode()
    # Only our module paths — never a random project editor.py.
    markers = (
        here,
        b"/window-arrange/editor.py",
        b"/share/window-arrange/editor.py",
        b"/.local/bin/editor.py",
        b"/.local/share/window-arrange/editor.py",
    )
    victims: list[int] = []
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid == me:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    raw = fh.read().replace(b"\0", b" ")
            except OSError:
                continue
            if b"editor.py" not in raw:
                continue
            if any(m in raw for m in markers):
                victims.append(pid)
    except OSError:
        victims = []

    if victims:
        import time

        for pid in victims:
            try:
                os.kill(pid, 15)  # SIGTERM
            except OSError:
                pass

        deadline = time.time() + 0.6
        alive: list[int] = list(victims)
        while time.time() < deadline:
            still: list[int] = []
            for pid in alive:
                try:
                    os.kill(pid, 0)
                    still.append(pid)
                except OSError:
                    pass
            alive = still
            if not alive:
                break
            time.sleep(0.05)
        for pid in alive:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    _write_pid_file()


# Palette — translucent outlines over the real desktop (no white wash).
COL_TILE_FILL = (0.18, 0.42, 0.78, 0.10)
COL_TILE_BORDER = (0.55, 0.82, 1.0, 0.85)
COL_ACTIVE_FILL = (0.95, 0.70, 0.20, 0.16)
COL_ACTIVE_BORDER = (1.0, 0.85, 0.35, 1.0)
COL_SWAP_FILL = (0.25, 0.80, 0.45, 0.18)
COL_SWAP_BORDER = (0.45, 1.0, 0.60, 1.0)
COL_HOVER_FILL = (0.30, 0.55, 0.90, 0.14)
COL_HANDLE = (1.0, 1.0, 1.0, 0.90)
COL_TEXT = (1.0, 1.0, 1.0, 0.95)
COL_TEXT_SHADOW = (0.0, 0.0, 0.0, 0.55)
COL_HINT_BG = (0.06, 0.07, 0.10, 0.78)
COL_GHOST = (1.0, 1.0, 1.0, 0.12)
COL_WORKAREA = (1.0, 1.0, 1.0, 0.14)

# Live-apply throttle while dragging (ms). Hyprland eval is ~5–20ms.
LIVE_APPLY_MS = 16


def _gap_from_env_or_meta(windows: list[dict[str, Any]]) -> int:
    raw = os.environ.get("WINDOW_ARRANGE_GAP") or os.environ.get("GAP")
    if raw not in (None, ""):
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    for w in windows:
        meta = w.get("meta") or {}
        if "gap" in meta:
            return int(meta["gap"])
    return DEFAULT_GAP


def _outer_from_env() -> int | None:
    raw = os.environ.get("WINDOW_ARRANGE_OUTER") or os.environ.get("OUTER")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _plan_from_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "address": w["address"],
            "class": w.get("class") or "",
            "role": w.get("role") or "grid",
            "mode": "float",
            "x": int(w["x"]),
            "y": int(w["y"]),
            "w": int(w["w"]),
            "h": int(w["h"]),
            "fullscreen": w.get("fullscreen") not in (0, False, None),
            "floating": bool(w.get("floating", True)),
            "pinned": bool(w.get("pinned")),
        }
        for w in windows
    ]


def _geom_signature(windows: list[dict[str, Any]]) -> tuple:
    return tuple(
        (w.get("address"), int(w["x"]), int(w["y"]), int(w["w"]), int(w["h"]))
        for w in windows
    )


class ArrangeCanvas(Gtk.DrawingArea):
    """Full-monitor drawing surface that owns drag state + live apply."""

    def __init__(
        self,
        windows: list[dict[str, Any]],
        bounds: tuple[int, int, int, int],
        mon_origin: tuple[int, int],
        gap: int,
        on_done,
        on_cancel,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.windows = clone_windows(windows)
        self.initial_windows = clone_windows(windows)
        self.bounds = bounds  # global logical (x0,y0,x1,y1)
        self.mon_origin = mon_origin  # (mx, my) global logical
        self.gap = gap
        self.min_w = DEFAULT_MIN_W
        self.min_h = DEFAULT_MIN_H
        self.on_done = on_done
        self.on_cancel = on_cancel

        self._drag_index: int | None = None
        self._drag_handle: str | None = None
        self._drag_origin_windows: list[dict[str, Any]] | None = None
        self._drag_start_local: tuple[float, float] | None = None
        self._hover_handle: str | None = None
        self._hover_index: int | None = None
        self._swap_target: int | None = None
        self._swap_band: list[int] = []  # all indices highlighted on cross-band drop
        self._ghost_xy: tuple[float, float] | None = None
        self._last_applied_sig: tuple | None = _geom_signature(self.windows)
        self._live_pending = False
        self._live_source: int | None = None
        self._applying = False

        # Transparent surface — no opaque default background.
        self.set_draw_func(self._on_draw)
        self.set_cursor(Gdk.Cursor.new_from_name("default"))

        click = Gtk.GestureClick.new()
        click.set_button(0)  # any button
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        self.add_controller(click)

        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        keys = Gtk.EventControllerKey.new()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self.set_focusable(True)
        self.set_can_focus(True)

    # --- coordinate helpers -------------------------------------------------

    def _to_global(self, local_x: float, local_y: float) -> tuple[int, int]:
        mx, my = self.mon_origin
        return int(round(local_x + mx)), int(round(local_y + my))

    def _to_local_rect(self, w: dict[str, Any]) -> tuple[float, float, float, float]:
        mx, my = self.mon_origin
        return w["x"] - mx, w["y"] - my, float(w["w"]), float(w["h"])

    # --- live apply ---------------------------------------------------------

    def _schedule_live_apply(self) -> None:
        """Coalesce hyprctl applies so drag stays smooth."""
        self._live_pending = True
        if self._live_source is not None:
            return

        def _tick() -> bool:
            if not self._live_pending:
                self._live_source = None
                return False
            self._live_pending = False
            self._flush_live_apply()
            # Keep the timer if another frame arrived during apply.
            if self._live_pending:
                return True
            self._live_source = None
            return False

        self._live_source = GLib.timeout_add(LIVE_APPLY_MS, _tick)

    def _flush_live_apply(self) -> None:
        if self._applying:
            self._live_pending = True
            return
        sig = _geom_signature(self.windows)
        if sig == self._last_applied_sig:
            return
        if not layout_is_valid(
            self.windows, self.bounds, min_w=self.min_w, min_h=self.min_h
        ):
            return
        self._applying = True
        try:
            result = apply_plan(_plan_from_windows(self.windows), refresh_clients=False)
            if not result.get("error"):
                self._last_applied_sig = sig
        finally:
            self._applying = False

    def _apply_now(self, windows: list[dict[str, Any]]) -> dict[str, Any]:
        result = apply_plan(_plan_from_windows(windows), refresh_clients=True)
        if not result.get("error"):
            self._last_applied_sig = _geom_signature(windows)
        return result

    # --- input --------------------------------------------------------------

    def _on_key(self, _ctrl, keyval, _keycode, _state) -> bool:
        if keyval in (Gdk.KEY_Escape,):
            self.on_cancel()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.on_done(self.windows)
            return True
        if keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self._resnap()
            return True
        return False

    def _resnap(self) -> None:
        try:
            self.windows = snapshot_workspace_windows()
            self.initial_windows = clone_windows(self.windows)
            self._last_applied_sig = _geom_signature(self.windows)
        except Exception:
            pass
        self._drag_index = None
        self._swap_target = None
        self._swap_band = []
        self.queue_draw()

    def _on_motion(self, _ctrl, x: float, y: float) -> None:
        if self._drag_index is not None:
            return
        gx, gy = self._to_global(x, y)
        hit = hit_test_handle(self.windows, gx, gy, handle_px=HANDLE_PX)
        if hit is None:
            self._hover_index = None
            self._hover_handle = None
            self.set_cursor(Gdk.Cursor.new_from_name("default"))
        else:
            self._hover_index, self._hover_handle = hit
            name = cursor_for_handle(self._hover_handle)
            cur = Gdk.Cursor.new_from_name(name) or Gdk.Cursor.new_from_name("default")
            self.set_cursor(cur)
        self.queue_draw()

    def _on_leave(self, _ctrl) -> None:
        if self._drag_index is None:
            self._hover_index = None
            self._hover_handle = None
            self.queue_draw()

    def _on_pressed(self, gesture: Gtk.GestureClick, _n, x, y) -> None:
        button = gesture.get_current_button()
        if button == 3:  # right-click cancel
            self.on_cancel()
            return
        self.grab_focus()

    def _on_released(self, gesture: Gtk.GestureClick, _n, x, y) -> None:
        pass

    def _on_drag_begin(self, _gesture, x: float, y: float) -> None:
        gx, gy = self._to_global(x, y)
        hit = hit_test_handle(self.windows, gx, gy, handle_px=HANDLE_PX)
        if hit is None:
            self._drag_index = None
            return
        idx, handle = hit
        self._drag_index = idx
        self._drag_handle = handle
        self._drag_origin_windows = clone_windows(self.windows)
        self._drag_start_local = (x, y)
        self._swap_target = None
        self._swap_band = []
        self._ghost_xy = (x, y)
        name = cursor_for_handle(handle)
        if name == "grab":
            name = "grabbing"
        cur = Gdk.Cursor.new_from_name(name) or Gdk.Cursor.new_from_name("default")
        self.set_cursor(cur)

    def _on_drag_update(self, _gesture, offset_x: float, offset_y: float) -> None:
        if self._drag_index is None or self._drag_origin_windows is None:
            return
        if self._drag_start_local is None:
            return
        sx, sy = self._drag_start_local
        cx, cy = sx + offset_x, sy + offset_y
        self._ghost_xy = (cx, cy)
        dx = int(round(offset_x))
        dy = int(round(offset_y))
        handle = self._drag_handle or "body"

        if handle == "body":
            gx, gy = self._to_global(cx, cy)
            self.windows = clone_windows(self._drag_origin_windows)
            target = window_at(self.windows, gx, gy, exclude=self._drag_index)
            self._swap_target = target
            self._swap_band = self._band_mates(self._drag_index, target)
            # Body drag only previews swap; apply happens on release.
        else:
            self.windows = resize_handle(
                self._drag_origin_windows,
                self._drag_index,
                handle,  # type: ignore[arg-type]
                dx,
                dy,
                gap=self.gap,
                min_w=self.min_w,
                min_h=self.min_h,
                bounds=self.bounds,
            )
            self._swap_target = None
            self._swap_band = []
            self._schedule_live_apply()
        self.queue_draw()

    def _on_drag_end(self, _gesture, offset_x: float, offset_y: float) -> None:
        if self._drag_index is None or self._drag_origin_windows is None:
            self._reset_drag()
            return
        handle = self._drag_handle or "body"
        if handle == "body":
            if self._drag_start_local is not None:
                cx = self._drag_start_local[0] + offset_x
                cy = self._drag_start_local[1] + offset_y
                gx, gy = self._to_global(cx, cy)
                target = window_at(
                    self._drag_origin_windows, gx, gy, exclude=self._drag_index
                )
                if target is not None:
                    self.windows = rearrange_drop(
                        self._drag_origin_windows,
                        self._drag_index,
                        target,
                        gap=self.gap,
                        min_w=self.min_w,
                        min_h=self.min_h,
                        bounds=self.bounds,
                    )
                    self._apply_now(self.windows)
                else:
                    self.windows = clone_windows(self._drag_origin_windows)
            else:
                self.windows = clone_windows(self._drag_origin_windows)
        else:
            # Final live apply so the last pixel lands.
            self._flush_live_apply()
        self._reset_drag()
        self.queue_draw()

    def _band_mates(self, src: int | None, dst: int | None) -> list[int]:
        """Indices to highlight on drop preview (whole destination band if cross-row)."""
        if src is None or dst is None:
            return []
        bands = detect_row_bands(self.windows)
        band_of = {}
        for bi, members in enumerate(bands):
            for i in members:
                band_of[i] = bi
        bs, bd = band_of.get(src, -1), band_of.get(dst, -1)
        if bs < 0 or bd < 0:
            return [dst]
        if bs == bd:
            return [dst]
        # Cross-band: highlight every window in the destination band (and
        # lightly the rest of the source band so the flip reads clearly).
        mates = list(bands[bd]) + [i for i in bands[bs] if i != src]
        return mates

    def _reset_drag(self) -> None:
        self._drag_index = None
        self._drag_handle = None
        self._drag_origin_windows = None
        self._drag_start_local = None
        self._swap_target = None
        self._swap_band = []
        self._ghost_xy = None
        self.set_cursor(Gdk.Cursor.new_from_name("default"))

    # --- drawing ------------------------------------------------------------

    def _on_draw(self, _area, cr, width: int, height: int) -> None:
        # Fully transparent backdrop — real windows show through.
        try:
            from cairo import OPERATOR_CLEAR, OPERATOR_OVER

            cr.set_operator(OPERATOR_CLEAR)
            cr.paint()
            cr.set_operator(OPERATOR_OVER)
        except Exception:
            # Fallback: paint nothing (rely on CSS transparent window bg).
            pass

        # Work-area outline only (no dim wash).
        x0, y0, x1, y1 = self.bounds
        mx, my = self.mon_origin
        cr.set_source_rgba(*COL_WORKAREA)
        cr.set_line_width(1.5)
        cr.rectangle(x0 - mx + 0.5, y0 - my + 0.5, x1 - x0 - 1, y1 - y0 - 1)
        cr.stroke()

        for i, w in enumerate(self.windows):
            self._draw_tile(cr, i, w)

        # Ghost of the dragged body.
        if (
            self._drag_index is not None
            and self._drag_handle == "body"
            and self._ghost_xy is not None
            and self._drag_origin_windows is not None
        ):
            src = self._drag_origin_windows[self._drag_index]
            gx, gy = self._ghost_xy
            if self._drag_start_local is not None:
                ox, oy = self._to_local_rect(src)[:2]
                grab_dx = self._drag_start_local[0] - ox
                grab_dy = self._drag_start_local[1] - oy
            else:
                grab_dx = src["w"] / 2
                grab_dy = src["h"] / 2
            rx = gx - grab_dx
            ry = gy - grab_dy
            cr.set_source_rgba(*COL_GHOST)
            self._round_rect(cr, rx, ry, src["w"], src["h"], 10)
            cr.fill()
            cr.set_source_rgba(*COL_ACTIVE_BORDER)
            cr.set_line_width(2)
            self._round_rect(cr, rx, ry, src["w"], src["h"], 10)
            cr.stroke()

        self._draw_hint_bar(cr, width, height)

    def _draw_tile(self, cr, index: int, w: dict[str, Any]) -> None:
        lx, ly, lw, lh = self._to_local_rect(w)
        is_drag = self._drag_index == index
        is_swap = index in self._swap_band or self._swap_target == index
        is_hover = self._hover_index == index and self._drag_index is None

        if is_swap:
            fill, border = COL_SWAP_FILL, COL_SWAP_BORDER
        elif is_drag and self._drag_handle != "body":
            fill, border = COL_ACTIVE_FILL, COL_ACTIVE_BORDER
        elif is_drag and self._drag_handle == "body":
            fill, border = (0.22, 0.45, 0.78, 0.06), (0.55, 0.78, 1.0, 0.40)
        elif is_hover:
            fill, border = COL_HOVER_FILL, COL_TILE_BORDER
        else:
            fill, border = COL_TILE_FILL, COL_TILE_BORDER

        cr.set_source_rgba(*fill)
        self._round_rect(cr, lx, ly, lw, lh, 10)
        cr.fill()
        cr.set_source_rgba(*border)
        cr.set_line_width(2.5 if (is_drag or is_swap or is_hover) else 1.5)
        self._round_rect(cr, lx, ly, lw, lh, 10)
        cr.stroke()

        # Edge/corner handle ticks.
        cr.set_source_rgba(*COL_HANDLE)
        cr.set_line_width(3)
        mid_x = lx + lw / 2
        mid_y = ly + lh / 2
        tick = 18
        cr.move_to(mid_x - tick / 2, ly + 5)
        cr.line_to(mid_x + tick / 2, ly + 5)
        cr.move_to(mid_x - tick / 2, ly + lh - 5)
        cr.line_to(mid_x + tick / 2, ly + lh - 5)
        cr.move_to(lx + 5, mid_y - tick / 2)
        cr.line_to(lx + 5, mid_y + tick / 2)
        cr.move_to(lx + lw - 5, mid_y - tick / 2)
        cr.line_to(lx + lw - 5, mid_y + tick / 2)
        cr.stroke()

        for cx, cy in (
            (lx + 6, ly + 6),
            (lx + lw - 6, ly + 6),
            (lx + 6, ly + lh - 6),
            (lx + lw - 6, ly + lh - 6),
        ):
            cr.arc(cx, cy, 3.5, 0, 2 * math.pi)
            cr.fill()

        label = (w.get("class") or w.get("title") or "window").strip() or "window"
        if len(label) > 28:
            label = label[:27] + "…"
        size_s = f"{int(w['w'])}×{int(w['h'])}"
        self._draw_centered_text(cr, lx, ly, lw, lh, label, size_s)

    def _draw_centered_text(self, cr, lx, ly, lw, lh, title: str, sub: str) -> None:
        layout = PangoCairo.create_layout(cr)
        font = Pango.FontDescription.from_string("Sans Bold 13")
        layout.set_font_description(font)
        layout.set_text(title, -1)
        layout.set_width(int(max(20, lw - 24) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        tw, th = layout.get_pixel_size()
        tx = lx + (lw - tw) / 2
        ty = ly + lh / 2 - th - 2
        # Soft shadow so labels stay readable on light or dark windows.
        cr.set_source_rgba(*COL_TEXT_SHADOW)
        cr.move_to(tx + 1, ty + 1)
        PangoCairo.show_layout(cr, layout)
        cr.set_source_rgba(*COL_TEXT)
        cr.move_to(tx, ty)
        PangoCairo.show_layout(cr, layout)

        layout2 = PangoCairo.create_layout(cr)
        font2 = Pango.FontDescription.from_string("Sans 10")
        layout2.set_font_description(font2)
        layout2.set_text(sub, -1)
        tw2, th2 = layout2.get_pixel_size()
        cr.set_source_rgba(*COL_TEXT_SHADOW)
        cr.move_to(lx + (lw - tw2) / 2 + 1, ly + lh / 2 + 5)
        PangoCairo.show_layout(cr, layout2)
        cr.set_source_rgba(1, 1, 1, 0.85)
        cr.move_to(lx + (lw - tw2) / 2, ly + lh / 2 + 4)
        PangoCairo.show_layout(cr, layout2)

    def _draw_hint_bar(self, cr, width: int, height: int) -> None:
        text = (
            "Drag body to swap rows/cells  ·  Edges resize live  ·  "
            "Enter done  ·  Esc restore  ·  R refresh"
        )
        pad_x, pad_y = 18, 10
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string("Sans 11"))
        layout.set_text(text, -1)
        tw, th = layout.get_pixel_size()
        bw = tw + pad_x * 2
        bh = th + pad_y * 2
        bx = (width - bw) / 2
        by = height - bh - 28
        cr.set_source_rgba(*COL_HINT_BG)
        self._round_rect(cr, bx, by, bw, bh, 8)
        cr.fill()
        cr.set_source_rgba(*COL_TEXT)
        cr.move_to(bx + pad_x, by + pad_y)
        PangoCairo.show_layout(cr, layout)

        self._btn_done = (bx + bw - 8 - 90, by - 44, 90, 34)
        self._btn_cancel = (bx + bw - 8 - 90 - 10 - 90, by - 44, 90, 34)
        self._draw_button(cr, *self._btn_cancel, "Cancel", False)
        self._draw_button(cr, *self._btn_done, "Done", True)

    def _draw_button(self, cr, x, y, w, h, label: str, primary: bool) -> None:
        if primary:
            cr.set_source_rgba(0.20, 0.50, 0.90, 0.92)
        else:
            cr.set_source_rgba(0.18, 0.20, 0.24, 0.88)
        self._round_rect(cr, x, y, w, h, 7)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.95)
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 11"))
        layout.set_text(label, -1)
        tw, th = layout.get_pixel_size()
        cr.move_to(x + (w - tw) / 2, y + (h - th) / 2)
        PangoCairo.show_layout(cr, layout)

    @staticmethod
    def _round_rect(cr, x, y, w, h, r) -> None:
        r = min(r, w / 2, h / 2)
        cr.new_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()


class EditorApp(Gtk.Application):
    def __init__(self, windows: list[dict[str, Any]], mon: dict[str, Any], gap: int):
        # ALLOW_REPLACEMENT + REPLACE: a second Super+B takes over the bus name
        # instead of stacking another CPU-heavy overlay.
        super().__init__(
            application_id="org.omarchy.windowarrange.editor",
            flags=Gio.ApplicationFlags.ALLOW_REPLACEMENT | Gio.ApplicationFlags.REPLACE,
        )
        self._windows = windows
        self._mon = mon
        self._gap = gap
        self._exit_code = 0
        self._canvas: ArrangeCanvas | None = None
        try:
            self.register()
        except Exception:
            pass

    def do_activate(self) -> None:  # noqa: N802 — GObject override
        mx, my, lw, lh, _scale = logical_monitor_box(self._mon)
        outer = _outer_from_env()
        if outer is None:
            outer = 16
        x0, y0, x1, y1, _aw, _ah = work_area(self._mon, outer)
        bounds = (x0, y0, x1, y1)

        win = Gtk.ApplicationWindow(application=self)
        win.set_default_size(lw, lh)
        win.set_title("window-arrange editor")
        # Ask GTK for a transparent background (no white flash on map).
        try:
            win.set_css_classes(["wa-editor"])
            css = Gtk.CssProvider()
            css.load_from_data(
                b"""
                window.wa-editor, window.wa-editor > * {
                    background-color: transparent;
                    background-image: none;
                }
                """
            )
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        except Exception:
            pass

        LayerShell.init_for_window(win)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(win, "window-arrange-editor")
        LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(win, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)
        LayerShell.set_anchor(win, LayerShell.Edge.RIGHT, True)
        LayerShell.set_exclusive_zone(win, -1)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.EXCLUSIVE)
        try:
            name = self._mon.get("name")
            if name:
                LayerShell.set_monitor(win, self._monitor_by_name(name))
        except Exception:
            pass

        def do_done(windows: list[dict[str, Any]]) -> None:
            # Layout is already live-applied during drag; one final commit.
            if self._canvas is not None:
                result = self._canvas._apply_now(windows)
            else:
                result = apply_plan(_plan_from_windows(windows))
            if result.get("error"):
                print(f"apply_error={result['error']}", file=sys.stderr)
                self._exit_code = 1
            else:
                print(f"applied={result.get('count', len(windows))} ms={result.get('ms', 0):.0f}")
                self._exit_code = 0
            win.close()

        def do_cancel() -> None:
            # Restore geometries from editor open.
            if self._canvas is not None:
                result = self._canvas._apply_now(self._canvas.initial_windows)
                if result.get("error"):
                    print(f"restore_error={result['error']}", file=sys.stderr)
            print("cancelled")
            self._exit_code = 0
            win.close()

        canvas = ArrangeCanvas(
            windows=self._windows,
            bounds=bounds,
            mon_origin=(mx, my),
            gap=self._gap,
            on_done=do_done,
            on_cancel=do_cancel,
        )
        self._canvas = canvas

        click = Gtk.GestureClick.new()
        click.set_button(1)

        def on_btn_click(_g, _n, x, y):
            for attr, action in (
                ("_btn_done", lambda: canvas.on_done(canvas.windows)),
                ("_btn_cancel", canvas.on_cancel),
            ):
                btn = getattr(canvas, attr, None)
                if not btn:
                    continue
                bx, by, bw, bh = btn
                if bx <= x <= bx + bw and by <= y <= by + bh:
                    action()
                    return

        click.connect("pressed", on_btn_click)
        canvas.add_controller(click)

        win.set_child(canvas)
        win.connect("close-request", lambda *_: False)
        win.present()
        GLib.idle_add(canvas.grab_focus)
        smoke_ms = os.environ.get("WINDOW_ARRANGE_EDIT_SMOKE_MS")
        if smoke_ms:
            try:
                ms = max(50, int(smoke_ms))
            except ValueError:
                ms = 0
            if ms:

                def _smoke_quit() -> bool:
                    print("smoke_cancel")
                    win.close()
                    self.quit()
                    return False

                GLib.timeout_add(ms, _smoke_quit)

    def _monitor_by_name(self, name: str):
        display = Gdk.Display.get_default()
        if display is None:
            return None
        mons = display.get_monitors()
        for i in range(mons.get_n_items()):
            m = mons.get_item(i)
            for attr in ("get_connector", "get_manufacturer"):
                fn = getattr(m, attr, None)
                if callable(fn):
                    try:
                        val = fn()
                        if val and name in str(val):
                            return m
                    except Exception:
                        pass
            desc = getattr(m, "get_description", None)
            if callable(desc):
                try:
                    if name in str(desc()):
                        return m
                except Exception:
                    pass
        return mons.get_item(0) if mons.get_n_items() else None


def run_editor(
    windows: list[dict[str, Any]] | None = None,
    *,
    gap: int | None = None,
) -> int:
    """Launch the overlay. Returns process exit code."""
    # Kill any stacked/orphan overlays before mapping a new one.
    _claim_single_instance()

    mon = focused_monitor()
    if windows is None:
        windows = snapshot_workspace_windows()
    if not windows:
        print(
            "window-arrange editor: no mapped windows on active workspace",
            file=sys.stderr,
        )
        return 0
    gap_i = int(gap if gap is not None else _gap_from_env_or_meta(windows))
    app = EditorApp(windows, mon, gap_i)
    app.run(None)
    return int(getattr(app, "_exit_code", 0))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    return run_editor()


if __name__ == "__main__":
    raise SystemExit(main())
