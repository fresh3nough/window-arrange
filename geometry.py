#!/usr/bin/env python3
"""Interactive geometry helpers for window-arrange.

Pure functions (no hyprctl) so rearrange + neighbor-aware resize can be unit
tested. Windows are axis-aligned rects in logical pixels: {id, x, y, w, h, ...}.

Resize keeps the gutter between neighbors: dragging an edge grows one window
and shrinks every abutting neighbor on that edge in the same motion (i3-style
split adjust). Corners combine the two axes.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Literal

Edge = Literal["left", "right", "top", "bottom"]
Handle = Edge | Literal[
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
    "body",
]

DEFAULT_MIN_W = 200
DEFAULT_MIN_H = 120
DEFAULT_GAP = 12
# How close two edges must be (beyond the nominal gap) to count as abutting.
EDGE_SLOP = 6
# Pixel hit target for edge/corner grabs in the overlay.
HANDLE_PX = 10
CORNER_PX = 14


def _as_rect(w: dict[str, Any]) -> tuple[int, int, int, int]:
    return int(w["x"]), int(w["y"]), int(w["w"]), int(w["h"])


def clone_windows(windows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [deepcopy(dict(w)) for w in windows]


def rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def interval_overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    """Length of overlap between [a0, a1) and [b0, b1)."""
    return max(0, min(a1, b1) - max(a0, b0))


def _vert_overlap(a: dict[str, Any], b: dict[str, Any]) -> int:
    return interval_overlap(a["y"], a["y"] + a["h"], b["y"], b["y"] + b["h"])


def _horiz_overlap(a: dict[str, Any], b: dict[str, Any]) -> int:
    return interval_overlap(a["x"], a["x"] + a["w"], b["x"], b["x"] + b["w"])


def find_neighbors(
    windows: list[dict[str, Any]],
    index: int,
    edge: Edge,
    gap: int = DEFAULT_GAP,
    slop: int = EDGE_SLOP,
) -> list[int]:
    """Indices of windows that abut `windows[index]` on `edge`, across `gap`."""
    src = windows[index]
    sx, sy, sw, sh = _as_rect(src)
    gap = max(0, int(gap))
    hit: list[int] = []
    for j, other in enumerate(windows):
        if j == index:
            continue
        ox, oy, ow, oh = _as_rect(other)
        if edge == "right":
            # other's left near src.right + gap
            if abs(ox - (sx + sw + gap)) <= slop and _vert_overlap(src, other) > 0:
                hit.append(j)
        elif edge == "left":
            if abs((ox + ow + gap) - sx) <= slop and _vert_overlap(src, other) > 0:
                hit.append(j)
        elif edge == "bottom":
            if abs(oy - (sy + sh + gap)) <= slop and _horiz_overlap(src, other) > 0:
                hit.append(j)
        elif edge == "top":
            if abs((oy + oh + gap) - sy) <= slop and _horiz_overlap(src, other) > 0:
                hit.append(j)
    return hit


def _clamp_delta_grow(
    primary_size: int,
    neighbor_sizes: list[int],
    delta: int,
    min_size: int,
) -> int:
    """Clamp delta so primary and every neighbor stay >= min_size.

    Positive delta grows primary and shrinks neighbors (right/bottom edges).
    Negative delta shrinks primary and grows neighbors.
    """
    if delta == 0:
        return 0
    # Primary lower bound
    if delta < 0:
        delta = max(delta, min_size - primary_size)
    # Neighbors shrink when delta > 0
    if delta > 0 and neighbor_sizes:
        max_shrink = min(s - min_size for s in neighbor_sizes)
        delta = min(delta, max(0, max_shrink))
    # Neighbors grow when delta < 0 — no upper bound from neighbors.
    # Primary grows when delta > 0 — no upper bound here (bounds box handles it).
    return delta


def _clamp_delta_shrink_primary_grows_on_neg(
    primary_size: int,
    neighbor_sizes: list[int],
    delta: int,
    min_size: int,
) -> int:
    """For left/top edges: positive delta moves the edge right/down.

    left edge +dx: primary x+=dx, w-=dx; neighbors grow on their right.
    So positive dx shrinks primary; negative dx grows primary / shrinks neighbors.
    """
    if delta == 0:
        return 0
    # Shrinking primary (delta > 0)
    if delta > 0:
        delta = min(delta, max(0, primary_size - min_size))
    # Shrinking neighbors (delta < 0)
    if delta < 0 and neighbor_sizes:
        max_shrink = min(s - min_size for s in neighbor_sizes)
        delta = max(delta, -max(0, max_shrink))
    return delta


def resize_edge(
    windows: list[dict[str, Any]],
    index: int,
    edge: Edge,
    delta: int,
    *,
    gap: int = DEFAULT_GAP,
    min_w: int = DEFAULT_MIN_W,
    min_h: int = DEFAULT_MIN_H,
    bounds: tuple[int, int, int, int] | None = None,
) -> list[dict[str, Any]]:
    """Return a new window list after dragging `edge` of `index` by `delta` px.

    Neighbors that abut across `gap` share the delta so the gutter stays put
    and no gap opens between tiles. `bounds` is (x0, y0, x1, y1) exclusive-ish
    work-area limits; when set, motion that would push a rect outside is cut.
    """
    if delta == 0 or index < 0 or index >= len(windows):
        return clone_windows(windows)

    out = clone_windows(windows)
    src = out[index]
    neighbors = find_neighbors(out, index, edge, gap=gap)

    if edge in ("left", "right"):
        min_size = min_w
        sizes = [out[j]["w"] for j in neighbors]
        if edge == "right":
            delta = _clamp_delta_grow(src["w"], sizes, delta, min_size)
        else:
            delta = _clamp_delta_shrink_primary_grows_on_neg(src["w"], sizes, delta, min_size)
    else:
        min_size = min_h
        sizes = [out[j]["h"] for j in neighbors]
        if edge == "bottom":
            delta = _clamp_delta_grow(src["h"], sizes, delta, min_size)
        else:
            delta = _clamp_delta_shrink_primary_grows_on_neg(src["h"], sizes, delta, min_size)

    if delta == 0:
        return out

    if edge == "right":
        src["w"] = int(src["w"] + delta)
        for j in neighbors:
            n = out[j]
            n["x"] = int(n["x"] + delta)
            n["w"] = int(n["w"] - delta)
    elif edge == "left":
        src["x"] = int(src["x"] + delta)
        src["w"] = int(src["w"] - delta)
        for j in neighbors:
            n = out[j]
            n["w"] = int(n["w"] + delta)
    elif edge == "bottom":
        src["h"] = int(src["h"] + delta)
        for j in neighbors:
            n = out[j]
            n["y"] = int(n["y"] + delta)
            n["h"] = int(n["h"] - delta)
    elif edge == "top":
        src["y"] = int(src["y"] + delta)
        src["h"] = int(src["h"] - delta)
        for j in neighbors:
            n = out[j]
            n["h"] = int(n["h"] + delta)

    if bounds is not None:
        out = _clamp_all_to_bounds(out, bounds, min_w=min_w, min_h=min_h)
    return out


def resize_handle(
    windows: list[dict[str, Any]],
    index: int,
    handle: Handle,
    dx: int,
    dy: int,
    *,
    gap: int = DEFAULT_GAP,
    min_w: int = DEFAULT_MIN_W,
    min_h: int = DEFAULT_MIN_H,
    bounds: tuple[int, int, int, int] | None = None,
) -> list[dict[str, Any]]:
    """Apply a handle drag. Corners adjust both axes; edges one axis; body no-op."""
    out = clone_windows(windows)
    if handle == "body":
        return out

    h_edge: Edge | None = None
    v_edge: Edge | None = None
    if handle in ("left", "top-left", "bottom-left"):
        h_edge = "left"
    elif handle in ("right", "top-right", "bottom-right"):
        h_edge = "right"
    if handle in ("top", "top-left", "top-right"):
        v_edge = "top"
    elif handle in ("bottom", "bottom-left", "bottom-right"):
        v_edge = "bottom"

    if h_edge is not None and dx:
        out = resize_edge(
            out, index, h_edge, dx, gap=gap, min_w=min_w, min_h=min_h, bounds=None
        )
    if v_edge is not None and dy:
        out = resize_edge(
            out, index, v_edge, dy, gap=gap, min_w=min_w, min_h=min_h, bounds=None
        )
    if bounds is not None:
        out = _clamp_all_to_bounds(out, bounds, min_w=min_w, min_h=min_h)
    return out


def _clamp_all_to_bounds(
    windows: list[dict[str, Any]],
    bounds: tuple[int, int, int, int],
    *,
    min_w: int,
    min_h: int,
) -> list[dict[str, Any]]:
    x0, y0, x1, y1 = bounds
    out = clone_windows(windows)
    for w in out:
        w["w"] = max(min_w, min(int(w["w"]), max(min_w, x1 - x0)))
        w["h"] = max(min_h, min(int(w["h"]), max(min_h, y1 - y0)))
        w["x"] = min(max(int(w["x"]), x0), max(x0, x1 - w["w"]))
        w["y"] = min(max(int(w["y"]), y0), max(y0, y1 - w["h"]))
        if w["x"] + w["w"] > x1:
            w["w"] = max(min_w, x1 - w["x"])
        if w["y"] + w["h"] > y1:
            w["h"] = max(min_h, y1 - w["y"])
    return out


def swap_windows(
    windows: list[dict[str, Any]],
    a: int,
    b: int,
) -> list[dict[str, Any]]:
    """Exchange geometry (x, y, w, h) between two windows; identities stay put."""
    out = clone_windows(windows)
    if a == b or a < 0 or b < 0 or a >= len(out) or b >= len(out):
        return out
    for key in ("x", "y", "w", "h"):
        out[a][key], out[b][key] = out[b][key], out[a][key]
    return out


def hit_test_handle(
    windows: list[dict[str, Any]],
    px: int,
    py: int,
    *,
    handle_px: int = HANDLE_PX,
    corner_px: int = CORNER_PX,
) -> tuple[int, Handle] | None:
    """Return (index, handle) under point, preferring corners then edges then body.

    Topmost (highest index) window wins when rects overlap — unusual for a grid
    but keeps the editor predictable if the user stacked floats beforehand.
    """
    # Iterate front-to-back: last drawn / highest z. We don't track z; use reverse order.
    for i in range(len(windows) - 1, -1, -1):
        w = windows[i]
        x, y, ww, hh = _as_rect(w)
        if not (x <= px < x + ww and y <= py < y + hh):
            continue
        left = px - x <= corner_px
        right = (x + ww) - px <= corner_px
        top = py - y <= corner_px
        bottom = (y + hh) - py <= corner_px
        # Corners first (use corner_px).
        if top and left:
            return i, "top-left"
        if top and right:
            return i, "top-right"
        if bottom and left:
            return i, "bottom-left"
        if bottom and right:
            return i, "bottom-right"
        # Edges (thinner strip is fine once corners ruled out).
        if px - x <= handle_px:
            return i, "left"
        if (x + ww) - px <= handle_px:
            return i, "right"
        if py - y <= handle_px:
            return i, "top"
        if (y + hh) - py <= handle_px:
            return i, "bottom"
        return i, "body"
    return None


def window_at(
    windows: list[dict[str, Any]],
    px: int,
    py: int,
    *,
    exclude: int | None = None,
) -> int | None:
    """Topmost window body containing point, optionally skipping one index."""
    for i in range(len(windows) - 1, -1, -1):
        if exclude is not None and i == exclude:
            continue
        x, y, ww, hh = _as_rect(windows[i])
        if x <= px < x + ww and y <= py < y + hh:
            return i
    return None


def cursor_for_handle(handle: Handle | None) -> str:
    """CSS / named cursor hint for the overlay."""
    return {
        None: "default",
        "body": "grab",
        "left": "ew-resize",
        "right": "ew-resize",
        "top": "ns-resize",
        "bottom": "ns-resize",
        "top-left": "nwse-resize",
        "bottom-right": "nwse-resize",
        "top-right": "nesw-resize",
        "bottom-left": "nesw-resize",
    }.get(handle or None, "default")
