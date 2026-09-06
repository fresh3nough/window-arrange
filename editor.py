#!/usr/bin/env python3
"""Interactive rearrange / resize overlay for window-arrange.

GTK4 + gtk4-layer-shell overlay on the focused monitor:

  - Drag a window body onto another → swap their cells
  - Drag edges / corners → resize, pushing abutting neighbors so gutters stay
  - Enter / click Apply → commit geometries via hyprctl
  - Esc / click Cancel → quit without changes
  - R → re-snapshot live window positions

Coordinates are Hyprland logical pixels (same space as layout.py).
"""

from __future__ import annotations

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
    hit_test_handle,
    resize_handle,
    swap_windows,
    window_at,
)
from layout import logical_monitor_box, work_area  # noqa: E402

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402


# Palette — readable on both light and dark wallpapers.
COL_DIM = (0.10, 0.12, 0.16, 0.55)
COL_TILE = (0.22, 0.45, 0.78, 0.42)
COL_TILE_BORDER = (0.55, 0.78, 1.0, 0.95)
COL_ACTIVE = (0.95, 0.70, 0.20, 0.50)
COL_ACTIVE_BORDER = (1.0, 0.85, 0.35, 1.0)
COL_SWAP = (0.30, 0.78, 0.45, 0.50)
COL_SWAP_BORDER = (0.45, 1.0, 0.60, 1.0)
COL_HANDLE = (1.0, 1.0, 1.0, 0.85)
COL_TEXT = (1.0, 1.0, 1.0, 0.95)
COL_HINT_BG = (0.08, 0.09, 0.12, 0.82)


def _gap_from_env_or_meta(windows: list[dict[str, Any]]) -> int:
    raw = os.environ.get("WINDOW_ARRANGE_GAP") or os.environ.get("GAP")
    if raw not in (None, ""):
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    # Prefer gap recorded on a prior arrange plan if present.
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


class ArrangeCanvas(Gtk.DrawingArea):
    """Full-monitor drawing surface that owns drag state."""

    def __init__(
        self,
        windows: list[dict[str, Any]],
        bounds: tuple[int, int, int, int],
        mon_origin: tuple[int, int],
        gap: int,
        on_apply,
        on_cancel,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.windows = clone_windows(windows)
        self.bounds = bounds  # global logical (x0,y0,x1,y1)
        self.mon_origin = mon_origin  # (mx, my) global logical
        self.gap = gap
        self.min_w = DEFAULT_MIN_W
        self.min_h = DEFAULT_MIN_H
        self.on_apply = on_apply
        self.on_cancel = on_cancel

        self._drag_index: int | None = None
        self._drag_handle: str | None = None
        self._drag_origin_windows: list[dict[str, Any]] | None = None
        self._drag_start_local: tuple[float, float] | None = None
        self._hover_handle: str | None = None
        self._hover_index: int | None = None
        self._swap_target: int | None = None
        self._ghost_xy: tuple[float, float] | None = None  # local pointer during body drag

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

    # --- input --------------------------------------------------------------

    def _on_key(self, _ctrl, keyval, _keycode, _state) -> bool:
        if keyval in (Gdk.KEY_Escape,):
            self.on_cancel()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.on_apply(self.windows)
            return True
        if keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self._resnap()
            return True
        return False

    def _resnap(self) -> None:
        try:
            self.windows = snapshot_workspace_windows()
        except Exception:
            pass
        self._drag_index = None
        self._swap_target = None
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
            # grab → grabbing while idle body hover still reads as move.
            if name == "grab":
                name = "grab"
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
        # Left click on empty chrome does nothing; focus for keys.
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
            # Preview swap target under cursor (not the dragged window).
            gx, gy = self._to_global(cx, cy)
            self.windows = clone_windows(self._drag_origin_windows)
            self._swap_target = window_at(
                self.windows, gx, gy, exclude=self._drag_index
            )
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
                    self.windows = swap_windows(
                        self._drag_origin_windows, self._drag_index, target
                    )
                else:
                    self.windows = clone_windows(self._drag_origin_windows)
            else:
                self.windows = clone_windows(self._drag_origin_windows)
        else:
            # Keep the last resize result already in self.windows.
            pass
        self._reset_drag()
        self.queue_draw()

    def _reset_drag(self) -> None:
        self._drag_index = None
        self._drag_handle = None
        self._drag_origin_windows = None
        self._drag_start_local = None
        self._swap_target = None
        self._ghost_xy = None
        self.set_cursor(Gdk.Cursor.new_from_name("default"))

    # --- drawing ------------------------------------------------------------

    def _on_draw(self, _area, cr, width: int, height: int) -> None:
        # Dim the desktop so tiles read clearly.
        cr.set_source_rgba(*COL_DIM)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # Work-area outline.
        x0, y0, x1, y1 = self.bounds
        mx, my = self.mon_origin
        cr.set_source_rgba(1, 1, 1, 0.18)
        cr.set_line_width(1.5)
        cr.rectangle(x0 - mx, y0 - my, x1 - x0, y1 - y0)
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
            # Center ghost under pointer relative to original grab offset if possible.
            if self._drag_start_local is not None:
                ox, oy = self._to_local_rect(src)[:2]
                grab_dx = self._drag_start_local[0] - ox
                grab_dy = self._drag_start_local[1] - oy
            else:
                grab_dx = src["w"] / 2
                grab_dy = src["h"] / 2
            rx = gx - grab_dx
            ry = gy - grab_dy
            cr.set_source_rgba(1, 1, 1, 0.20)
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
        is_swap = self._swap_target == index
        is_hover = self._hover_index == index and self._drag_index is None

        if is_swap:
            fill, border = COL_SWAP, COL_SWAP_BORDER
        elif is_drag and self._drag_handle != "body":
            fill, border = COL_ACTIVE, COL_ACTIVE_BORDER
        elif is_drag and self._drag_handle == "body":
            fill, border = (0.22, 0.45, 0.78, 0.22), (0.55, 0.78, 1.0, 0.45)
        elif is_hover:
            fill, border = (0.28, 0.52, 0.85, 0.50), COL_TILE_BORDER
        else:
            fill, border = COL_TILE, COL_TILE_BORDER

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
        # top
        cr.move_to(mid_x - tick / 2, ly + 5)
        cr.line_to(mid_x + tick / 2, ly + 5)
        # bottom
        cr.move_to(mid_x - tick / 2, ly + lh - 5)
        cr.line_to(mid_x + tick / 2, ly + lh - 5)
        # left
        cr.move_to(lx + 5, mid_y - tick / 2)
        cr.line_to(lx + 5, mid_y + tick / 2)
        # right
        cr.move_to(lx + lw - 5, mid_y - tick / 2)
        cr.line_to(lx + lw - 5, mid_y + tick / 2)
        cr.stroke()

        # Corner dots.
        for cx, cy in (
            (lx + 6, ly + 6),
            (lx + lw - 6, ly + 6),
            (lx + 6, ly + lh - 6),
            (lx + lw - 6, ly + lh - 6),
        ):
            cr.arc(cx, cy, 3.5, 0, 2 * math.pi)
            cr.fill()

        # Label.
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
        cr.set_source_rgba(*COL_TEXT)
        tx = lx + (lw - tw) / 2
        ty = ly + lh / 2 - th - 2
        cr.move_to(tx, ty)
        PangoCairo.show_layout(cr, layout)

        layout2 = PangoCairo.create_layout(cr)
        font2 = Pango.FontDescription.from_string("Sans 10")
        layout2.set_font_description(font2)
        layout2.set_text(sub, -1)
        tw2, th2 = layout2.get_pixel_size()
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.move_to(lx + (lw - tw2) / 2, ly + lh / 2 + 4)
        PangoCairo.show_layout(cr, layout2)

    def _draw_hint_bar(self, cr, width: int, height: int) -> None:
        text = (
            "Drag body to swap  ·  Drag edges/corners to resize neighbors  ·  "
            "Enter apply  ·  Esc cancel  ·  R refresh"
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

        # Apply / Cancel affordance as drawn buttons (click handled via keys primarily,
        # but we also hit-test these zones on click for discoverability).
        self._btn_apply = (bx + bw - 8 - 90, by - 44, 90, 34)
        self._btn_cancel = (bx + bw - 8 - 90 - 10 - 90, by - 44, 90, 34)
        self._draw_button(cr, *self._btn_cancel, "Cancel", False)
        self._draw_button(cr, *self._btn_apply, "Apply", True)

    def _draw_button(self, cr, x, y, w, h, label: str, primary: bool) -> None:
        if primary:
            cr.set_source_rgba(0.25, 0.55, 0.95, 0.95)
        else:
            cr.set_source_rgba(0.25, 0.27, 0.32, 0.95)
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
        super().__init__(application_id="org.omarchy.windowarrange.editor")
        self._windows = windows
        self._mon = mon
        self._gap = gap
        self._exit_code = 0

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

        LayerShell.init_for_window(win)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(win, "window-arrange-editor")
        LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(win, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)
        LayerShell.set_anchor(win, LayerShell.Edge.RIGHT, True)
        LayerShell.set_exclusive_zone(win, -1)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.EXCLUSIVE)
        # Pin to the focused monitor when the API exposes output names.
        try:
            name = self._mon.get("name")
            if name:
                LayerShell.set_monitor(win, self._monitor_by_name(name))
        except Exception:
            pass

        def do_apply(windows: list[dict[str, Any]]) -> None:
            plan = [
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
            result = apply_plan(plan)
            if result.get("error"):
                print(f"apply_error={result['error']}", file=sys.stderr)
                self._exit_code = 1
            else:
                print(f"applied={result['count']} ms={result['ms']:.0f}")
                self._exit_code = 0
            win.close()

        def do_cancel() -> None:
            print("cancelled")
            self._exit_code = 0
            win.close()

        canvas = ArrangeCanvas(
            windows=self._windows,
            bounds=bounds,
            mon_origin=(mx, my),
            gap=self._gap,
            on_apply=do_apply,
            on_cancel=do_cancel,
        )

        # Overlay Apply/Cancel click targets via a second gesture on canvas.
        click = Gtk.GestureClick.new()
        click.set_button(1)

        def on_btn_click(_g, _n, x, y):
            for attr, action in (
                ("_btn_apply", lambda: canvas.on_apply(canvas.windows)),
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
            # Gdk.Monitor connector / model varies; try a few attrs.
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
    mon = focused_monitor()
    if windows is None:
        windows = snapshot_workspace_windows()
    if not windows:
        print("window-arrange editor: no mapped windows on active workspace", file=sys.stderr)
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
