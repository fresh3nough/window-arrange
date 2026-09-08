#!/usr/bin/env python3
"""Interactive geometry helpers for window-arrange.

Pure functions (no hyprctl) so rearrange + neighbor-aware resize can be unit
tested. Windows are axis-aligned rects in logical pixels: {id, x, y, w, h, ...}.

Resize keeps the gutter between neighbors: dragging an edge grows one window
and shrinks every abutting neighbor on that edge in the same motion (i3-style
split adjust). Corners combine the two axes.

After any resize the layout is guaranteed free of interior overlaps: free-edge
growth is clamped against non-neighbor obstacles (and work-area bounds) so a
tile cannot slide into another cell. Bounds clamping never shifts the opposite
edge of a window (that was the classic "clamp → overlap" bug).
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
# Pixel hit target for edge/corner grabs in the overlay (logical px).
# Generous on purpose: at scale 2.0 a 10px zone is a 5px physical target and
# users always miss it, ending up on body-drag reorder instead of resize.
HANDLE_PX = 28
CORNER_PX = 36

def _as_rect(w: dict[str, Any]) -> tuple[int, int, int, int]:
    return int(w["x"]), int(w["y"]), int(w["w"]), int(w["h"])


def clone_windows(windows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [deepcopy(dict(w)) for w in windows]


def rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """True when interiors intersect. Touching edges (share a line) is OK."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def any_overlaps(
    windows: list[dict[str, Any]],
) -> list[tuple[int, int, tuple[int, int, int, int], tuple[int, int, int, int]]]:
    """Return every pair of overlapping window indices with their rects."""
    bad: list[tuple[int, int, tuple[int, int, int, int], tuple[int, int, int, int]]] = []
    for i in range(len(windows)):
        ai = _as_rect(windows[i])
        for j in range(i + 1, len(windows)):
            bj = _as_rect(windows[j])
            if rects_overlap(ai, bj):
                bad.append((i, j, ai, bj))
    return bad


def _window_mins(
    w: dict[str, Any], min_w: int = DEFAULT_MIN_W, min_h: int = DEFAULT_MIN_H
) -> tuple[int, int]:
    """Per-window toolkit minimums, falling back to the caller's global floor.

    Toolkit-clamped apps (Chromium ~500w, Goose ~480x400, Nautilus ~360x380)
    refuse to render smaller, so resizing a neighbor below its own min width
    made the live clamp overlap it. Use each window's own min when present.
    """
    mw = int(w.get("min_w") or min_w)
    mh = int(w.get("min_h") or min_h)
    return mw, mh


def layout_is_valid(
    windows: list[dict[str, Any]],
    bounds: tuple[int, int, int, int] | None = None,
    *,
    min_w: int = DEFAULT_MIN_W,
    min_h: int = DEFAULT_MIN_H,
) -> bool:
    """No interior overlaps, every tile >= its own min size, inside bounds."""
    for w in windows:
        mw, mh = _window_mins(w, min_w, min_h)
        if int(w["w"]) < mw or int(w["h"]) < mh:
            return False
        if bounds is not None:
            x0, y0, x1, y1 = bounds
            x, y, ww, hh = _as_rect(w)
            if x < x0 or y < y0 or x + ww > x1 or y + hh > y1:
                return False
    return not any_overlaps(windows)


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
    neighbor_mins: list[int] | None = None,
) -> int:
    """Clamp delta so primary stays >= min_size and each neighbor >= its own min.

    Positive delta grows primary and shrinks neighbors (right/bottom edges).
    Negative delta shrinks primary and grows neighbors.
    """
    if delta == 0:
        return 0
    # Primary lower bound
    if delta < 0:
        delta = max(delta, min_size - primary_size)
    # Neighbors shrink when delta > 0 — cap by each neighbor's own min.
    if delta > 0 and neighbor_sizes:
        mins = neighbor_mins or [min_size] * len(neighbor_sizes)
        max_shrink = min(
            s - m for s, m in zip(neighbor_sizes, mins)
        )
        delta = min(delta, max(0, max_shrink))
    return delta


def _clamp_delta_shrink_primary_grows_on_neg(
    primary_size: int,
    neighbor_sizes: list[int],
    delta: int,
    min_size: int,
    neighbor_mins: list[int] | None = None,
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
    # Shrinking neighbors (delta < 0) — cap by each neighbor's own min.
    if delta < 0 and neighbor_sizes:
        mins = neighbor_mins or [min_size] * len(neighbor_sizes)
        max_shrink = min(
            s - m for s, m in zip(neighbor_sizes, mins)
        )
        delta = max(delta, -max(0, max_shrink))
    return delta


def _apply_edge_delta(
    out: list[dict[str, Any]],
    index: int,
    edge: Edge,
    delta: int,
    neighbors: list[int],
) -> None:
    """Mutate `out` applying an already-clamped edge delta."""
    if delta == 0:
        return
    src = out[index]
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


def _max_free_growth(
    windows: list[dict[str, Any]],
    index: int,
    edge: Edge,
    gap: int,
    bounds: tuple[int, int, int, int] | None,
    ignore: set[int],
) -> int:
    """How many px the free edge of `index` can grow before hitting an obstacle.

    Obstacles are any window not in `ignore` (typically the primary + its
    abutting neighbors already being resized) plus the work-area bound.
    Gap is reserved between the growing edge and any obstacle.
    """
    src = windows[index]
    sx, sy, sw, sh = _as_rect(src)
    gap = max(0, int(gap))
    limit = 10**9

    if edge == "right":
        if bounds is not None:
            limit = min(limit, bounds[2] - (sx + sw))
        for j, other in enumerate(windows):
            if j in ignore:
                continue
            ox, oy, ow, oh = _as_rect(other)
            # Obstacle starts to the right and vertically overlaps.
            if ox >= sx + sw and _vert_overlap(src, other) > 0:
                limit = min(limit, ox - gap - (sx + sw))
    elif edge == "left":
        # Growth on left means decreasing x (negative delta). Return max |delta|.
        if bounds is not None:
            limit = min(limit, sx - bounds[0])
        for j, other in enumerate(windows):
            if j in ignore:
                continue
            ox, oy, ow, oh = _as_rect(other)
            if ox + ow <= sx and _vert_overlap(src, other) > 0:
                limit = min(limit, sx - gap - (ox + ow))
    elif edge == "bottom":
        if bounds is not None:
            limit = min(limit, bounds[3] - (sy + sh))
        for j, other in enumerate(windows):
            if j in ignore:
                continue
            ox, oy, ow, oh = _as_rect(other)
            if oy >= sy + sh and _horiz_overlap(src, other) > 0:
                limit = min(limit, oy - gap - (sy + sh))
    elif edge == "top":
        if bounds is not None:
            limit = min(limit, sy - bounds[1])
        for j, other in enumerate(windows):
            if j in ignore:
                continue
            ox, oy, ow, oh = _as_rect(other)
            if oy + oh <= sy and _horiz_overlap(src, other) > 0:
                limit = min(limit, sy - gap - (oy + oh))
    return max(0, int(limit))


def _max_neighbor_outward_growth(
    windows: list[dict[str, Any]],
    neighbors: list[int],
    edge: Edge,
    gap: int,
    bounds: tuple[int, int, int, int] | None,
    ignore: set[int],
) -> int:
    """When primary shrinks, neighbors grow outward; clamp by their free space."""
    if not neighbors:
        return 10**9
    # Neighbor grows on the side opposite the shared edge.
    grow_edge: Edge = {
        "right": "left",   # neighbor sits to the right; grows leftward into freed gap? No —
        # Actually for right-edge shrink (delta<0): neighbor x decreases? Wait.
        # right edge delta<0: src.w decreases; neighbor: x += delta (moves left), w -= delta (grows).
        # Neighbor's LEFT edge moves left — free growth is toward the primary, already
        # accounted by the shared gutter. The neighbor's RIGHT edge stays put.
        # So no outward obstacle check needed for the classic abutting case.
        #
        # EXCEPT when primary shrinks on left edge (delta>0): neighbor grows its right
        # (n.w += delta) while n.x stays — neighbor expands rightward. That CAN hit
        # something to the right of the neighbor.
        "left": "right",
        "bottom": "top",
        "top": "bottom",
    }[edge]

    # Only the cases where neighbor expands away from the primary need a check:
    # left-edge positive delta → neighbors grow right
    # right-edge negative delta → neighbors grow left (their x moves, right edge fixed) — OK
    # top-edge positive delta → neighbors grow bottom
    # bottom-edge negative delta → neighbors grow top — OK
    if edge in ("right", "bottom"):
        # Neighbors grow toward primary (inward). Right/bottom edge of neighbor fixed.
        return 10**9

    limit = 10**9
    for j in neighbors:
        # How far can neighbor j grow on grow_edge?
        limit = min(
            limit,
            _max_free_growth(windows, j, grow_edge, gap, bounds, ignore),
        )
    return max(0, int(limit))


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
    and no gap opens between tiles. Free-edge growth is clamped against
    non-neighbor obstacles and `bounds` so cells never overlap. Unlike a naive
    post-hoc bounds clamp, the opposite edge of every window stays fixed.
    """
    if delta == 0 or index < 0 or index >= len(windows):
        return clone_windows(windows)

    out = clone_windows(windows)
    src = out[index]
    neighbors = find_neighbors(out, index, edge, gap=gap)
    ignore = {index, *neighbors}
    # Resolve per-window minimums so neighbors can't be shrunk below their own
    # toolkit min (Chromium ~500w) — that previously left live overlaps.
    src_mw, src_mh = _window_mins(src, min_w, min_h)
    n_mins = [_window_mins(out[j], min_w, min_h) for j in neighbors]

    if edge in ("left", "right"):
        min_size = src_mw
        sizes = [out[j]["w"] for j in neighbors]
        neighbor_mins = [m[0] for m in n_mins]
        if edge == "right":
            delta = _clamp_delta_grow(src["w"], sizes, delta, min_size, neighbor_mins)
            if delta > 0:
                # Growing primary right: blocked by free-space obstacles.
                free = _max_free_growth(out, index, "right", gap, bounds, ignore)
                delta = min(delta, free)
            # delta < 0 shrinks primary; neighbors grow left (inward) — safe.
        else:
            delta = _clamp_delta_shrink_primary_grows_on_neg(
                src["w"], sizes, delta, min_size, neighbor_mins
            )
            if delta < 0:
                # Growing primary left.
                free = _max_free_growth(out, index, "left", gap, bounds, ignore)
                delta = max(delta, -free)
            elif delta > 0:
                # Shrinking primary; neighbors grow right — clamp by their free space.
                free = _max_neighbor_outward_growth(
                    out, neighbors, "left", gap, bounds, ignore
                )
                delta = min(delta, free)
    else:
        min_size = src_mh
        sizes = [out[j]["h"] for j in neighbors]
        neighbor_mins = [m[1] for m in n_mins]
        if edge == "bottom":
            delta = _clamp_delta_grow(src["h"], sizes, delta, min_size, neighbor_mins)
            if delta > 0:
                free = _max_free_growth(out, index, "bottom", gap, bounds, ignore)
                delta = min(delta, free)
        else:
            delta = _clamp_delta_shrink_primary_grows_on_neg(
                src["h"], sizes, delta, min_size, neighbor_mins
            )
            if delta < 0:
                free = _max_free_growth(out, index, "top", gap, bounds, ignore)
                delta = max(delta, -free)
            elif delta > 0:
                free = _max_neighbor_outward_growth(
                    out, neighbors, "top", gap, bounds, ignore
                )
                delta = min(delta, free)

    if delta == 0:
        return out

    _apply_edge_delta(out, index, edge, delta, neighbors)

    # Safety net: if anything still overlaps (floating junk, weird geometry),
    # refuse the motion and return the pre-drag layout.
    if not layout_is_valid(out, bounds, min_w=min_w, min_h=min_h):
        return clone_windows(windows)
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

    # Apply axes sequentially from the original snapshot so each axis sees a
    # consistent neighbor set. If the combined result is invalid, try each axis
    # alone and keep the best valid partial.
    candidate = out
    if h_edge is not None and dx:
        candidate = resize_edge(
            candidate, index, h_edge, dx, gap=gap, min_w=min_w, min_h=min_h, bounds=bounds
        )
    if v_edge is not None and dy:
        candidate = resize_edge(
            candidate, index, v_edge, dy, gap=gap, min_w=min_w, min_h=min_h, bounds=bounds
        )

    if layout_is_valid(candidate, bounds, min_w=min_w, min_h=min_h):
        return candidate

    # Combined move failed — try horizontal only, then vertical only.
    if h_edge is not None and dx:
        only_h = resize_edge(
            out, index, h_edge, dx, gap=gap, min_w=min_w, min_h=min_h, bounds=bounds
        )
        if layout_is_valid(only_h, bounds, min_w=min_w, min_h=min_h):
            return only_h
    if v_edge is not None and dy:
        only_v = resize_edge(
            out, index, v_edge, dy, gap=gap, min_w=min_w, min_h=min_h, bounds=bounds
        )
        if layout_is_valid(only_v, bounds, min_w=min_w, min_h=min_h):
            return only_v
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


# How close two windows' vertical centers / y-ranges must be to share a row band.
BAND_Y_SLOP = 48


def _split_axis(total: int, count: int, gap: int) -> list[tuple[int, int]]:
    """Partition `total` px into `count` segments separated by `gap`.

    Returns [(offset_from_start, size), ...] — same contract as layout.split_axis.
    """
    if count <= 0:
        return []
    if count == 1:
        return [(0, max(1, total))]
    gap = max(0, int(gap))
    usable = total - gap * (count - 1)
    if usable < count:
        usable = count
    base, rem = divmod(usable, count)
    out: list[tuple[int, int]] = []
    pos = 0
    for i in range(count):
        size = base + (1 if i < rem else 0)
        out.append((pos, size))
        pos += size + gap
    return out


def detect_row_bands(
    windows: list[dict[str, Any]],
    *,
    y_slop: int = BAND_Y_SLOP,
) -> list[list[int]]:
    """Group window indices into horizontal row-bands, top → bottom.

    Windows whose vertical spans substantially overlap (or whose y-centers are
    within `y_slop`) share a band. Within a band, indices are left → right.
    This is what lets a full-width bottom tile trade places with a 2-up top row.
    """
    n = len(windows)
    if n == 0:
        return []
    # Greedy cluster by sorted y-center.
    order = sorted(range(n), key=lambda i: (windows[i]["y"] + windows[i]["h"] / 2, windows[i]["x"]))
    bands: list[list[int]] = []
    band_y0: list[float] = []
    band_y1: list[float] = []
    for i in order:
        y = float(windows[i]["y"])
        y1 = y + float(windows[i]["h"])
        cy = (y + y1) / 2.0
        placed = False
        for b, members in enumerate(bands):
            # Join if center is near the band's vertical range, or ranges overlap a lot.
            if band_y0[b] - y_slop <= cy <= band_y1[b] + y_slop:
                members.append(i)
                band_y0[b] = min(band_y0[b], y)
                band_y1[b] = max(band_y1[b], y1)
                placed = True
                break
            # Substantial vertical overlap with the band's union.
            ov = interval_overlap(int(y), int(y1), int(band_y0[b]), int(band_y1[b]))
            if ov > 0.4 * min(y1 - y, band_y1[b] - band_y0[b]):
                members.append(i)
                band_y0[b] = min(band_y0[b], y)
                band_y1[b] = max(band_y1[b], y1)
                placed = True
                break
        if not placed:
            bands.append([i])
            band_y0.append(y)
            band_y1.append(y1)
    # Sort bands top→bottom by their top edge; members left→right.
    ranked = sorted(range(len(bands)), key=lambda b: band_y0[b])
    result: list[list[int]] = []
    for b in ranked:
        members = sorted(bands[b], key=lambda i: windows[i]["x"])
        result.append(members)
    return result


def _band_frame(
    windows: list[dict[str, Any]],
    members: list[int],
) -> tuple[int, int, int, int]:
    """Bounding box of a band: (x0, y0, x1, y1)."""
    xs = [windows[i]["x"] for i in members]
    ys = [windows[i]["y"] for i in members]
    x1s = [windows[i]["x"] + windows[i]["w"] for i in members]
    y1s = [windows[i]["y"] + windows[i]["h"] for i in members]
    return min(xs), min(ys), max(x1s), max(y1s)


def _place_in_frame(
    out: list[dict[str, Any]],
    members: list[int],
    frame: tuple[int, int, int, int],
    gap: int,
    *,
    min_w: int,
    min_h: int,
) -> None:
    """Lay `members` (left→right order) evenly into `frame` as a single row."""
    x0, y0, x1, y1 = frame
    fw = max(1, x1 - x0)
    fh = max(1, y1 - y0)
    n = len(members)
    if n == 0:
        return
    segs = _split_axis(fw, n, gap)
    for (off, size), idx in zip(segs, members):
        out[idx]["x"] = int(x0 + off)
        out[idx]["y"] = int(y0)
        out[idx]["w"] = max(min_w, int(size))
        out[idx]["h"] = max(min_h, int(fh))
        # If min_w forced a wider tile, clip to frame rather than overflow.
        if out[idx]["x"] + out[idx]["w"] > x1:
            out[idx]["w"] = max(min_w, x1 - out[idx]["x"])
        if out[idx]["y"] + out[idx]["h"] > y1:
            out[idx]["h"] = max(min_h, y1 - out[idx]["y"])


def swap_bands(
    windows: list[dict[str, Any]],
    band_a: list[int],
    band_b: list[int],
    *,
    gap: int = DEFAULT_GAP,
    min_w: int = DEFAULT_MIN_W,
    min_h: int = DEFAULT_MIN_H,
    bounds: tuple[int, int, int, int] | None = None,
) -> list[dict[str, Any]]:
    """Exchange the row-frames of two bands, redistributing members into each.

    Classic case: top band has 2 half-width tiles, bottom band has 1 full-width
    tile. After the swap the single window fills the old top frame and the pair
    splits the old bottom frame — identities stay with their windows.
    """
    if not band_a or not band_b:
        return clone_windows(windows)
    if set(band_a) & set(band_b):
        return clone_windows(windows)

    out = clone_windows(windows)
    frame_a = _band_frame(windows, band_a)
    frame_b = _band_frame(windows, band_b)

    # Preserve left→right order within each band as they move.
    order_a = sorted(band_a, key=lambda i: windows[i]["x"])
    order_b = sorted(band_b, key=lambda i: windows[i]["x"])

    _place_in_frame(out, order_a, frame_b, gap, min_w=min_w, min_h=min_h)
    _place_in_frame(out, order_b, frame_a, gap, min_w=min_w, min_h=min_h)

    if not layout_is_valid(out, bounds, min_w=min_w, min_h=min_h):
        return clone_windows(windows)
    return out


def rearrange_drop(
    windows: list[dict[str, Any]],
    src: int,
    dst: int,
    *,
    gap: int = DEFAULT_GAP,
    min_w: int = DEFAULT_MIN_W,
    min_h: int = DEFAULT_MIN_H,
    bounds: tuple[int, int, int, int] | None = None,
) -> list[dict[str, Any]]:
    """Body-drop rearrange: same-band → cell swap; cross-band → full band swap.

    Dropping any window from a multi-tile top row onto the full-width bottom
    window (or the reverse) flips the two row frames so the pair moves down and
    the single window moves up — the interaction the 1:1 geometry swap could
    not express.
    """
    out = clone_windows(windows)
    n = len(out)
    if src == dst or src < 0 or dst < 0 or src >= n or dst >= n:
        return out

    bands = detect_row_bands(out)
    band_of = [-1] * n
    for bi, members in enumerate(bands):
        for i in members:
            band_of[i] = bi

    bs, bd = band_of[src], band_of[dst]
    if bs < 0 or bd < 0:
        return swap_windows(out, src, dst)

    if bs == bd:
        # Same row: simple cell swap keeps the band's column structure.
        return swap_windows(out, src, dst)

    # Different rows: swap the entire bands' frames (handles 2-up ↔ 1-up).
    return swap_bands(
        out,
        bands[bs],
        bands[bd],
        gap=gap,
        min_w=min_w,
        min_h=min_h,
        bounds=bounds,
    )


def _clamp_handle_px(
    ww: int,
    hh: int,
    handle_px: int,
    corner_px: int,
) -> tuple[int, int]:
    """Keep a usable body grab on tiny tiles by capping zone size."""
    # Leave at least ~40% of each axis as pure body when the tile is small.
    max_h = max(6, min(handle_px, ww // 3, hh // 3))
    max_c = max(max_h, min(corner_px, ww // 2, hh // 2))
    return max_h, max_c


def _classify_handle_for_rect(
    px: int,
    py: int,
    x: int,
    y: int,
    ww: int,
    hh: int,
    handle_px: int,
    corner_px: int,
) -> tuple[Handle, float] | None:
    """Return (handle, distance) if point is on/near this rect, else None.

    Distance is 0 for interior hits and the outside offset for gutter grabs so
    callers can pick the nearest abutting edge when the pointer is in a gap.
    """
    hp, cp = _clamp_handle_px(ww, hh, handle_px, corner_px)
    # Reject points nowhere near the rect (interior or exterior band).
    if px < x - hp or px >= x + ww + hp or py < y - hp or py >= y + hh + hp:
        return None

    inside = x <= px < x + ww and y <= py < y + hh
    d_left = px - x  # 0 on edge, <0 outside left, >0 inside
    d_right = (x + ww) - px
    d_top = py - y
    d_bottom = (y + hh) - py

    near_left = -hp <= d_left <= cp
    near_right = -hp <= d_right <= cp
    near_top = -hp <= d_top <= cp
    near_bottom = -hp <= d_bottom <= cp

    # Corners first (larger target than edges).
    if near_top and near_left and d_left <= cp and d_top <= cp:
        dist = 0.0 if inside else float(max(0, -d_left, -d_top))
        return "top-left", dist
    if near_top and near_right and d_right <= cp and d_top <= cp:
        dist = 0.0 if inside else float(max(0, -d_right, -d_top))
        return "top-right", dist
    if near_bottom and near_left and d_left <= cp and d_bottom <= cp:
        dist = 0.0 if inside else float(max(0, -d_left, -d_bottom))
        return "bottom-left", dist
    if near_bottom and near_right and d_right <= cp and d_bottom <= cp:
        dist = 0.0 if inside else float(max(0, -d_right, -d_bottom))
        return "bottom-right", dist

    # Edges — require proximity on the primary axis within handle_px, and stay
    # within the segment (expanded by hp so gutters between tiles still count).
    if -hp <= d_left <= hp and (y - hp) <= py < (y + hh + hp):
        dist = 0.0 if inside else float(max(0, -d_left))
        return "left", dist
    if -hp <= d_right <= hp and (y - hp) <= py < (y + hh + hp):
        dist = 0.0 if inside else float(max(0, -d_right))
        return "right", dist
    if -hp <= d_top <= hp and (x - hp) <= px < (x + ww + hp):
        dist = 0.0 if inside else float(max(0, -d_top))
        return "top", dist
    if -hp <= d_bottom <= hp and (x - hp) <= px < (x + ww + hp):
        dist = 0.0 if inside else float(max(0, -d_bottom))
        return "bottom", dist

    if inside:
        return "body", 0.0
    return None


def hit_test_handle(
    windows: list[dict[str, Any]],
    px: int,
    py: int,
    *,
    handle_px: int = HANDLE_PX,
    corner_px: int = CORNER_PX,
) -> tuple[int, Handle] | None:
    """Return (index, handle) under point, preferring corners then edges then body.

    Hits extend *outside* the rect by ``handle_px`` so the gutter between tiled
    windows still grabs the nearest edge/corner (previously a dead zone that
    forced body-drag reorder). Interior edge/corner of a containing window
    beats exterior gutter hits; among gutters the nearest edge wins. Topmost
    (highest index) window wins for interior body when rects overlap.
    """
    interior_resize: tuple[int, Handle] | None = None
    interior_body: tuple[int, Handle] | None = None
    best_exterior: tuple[float, int, Handle] | None = None

    for i in range(len(windows) - 1, -1, -1):
        x, y, ww, hh = _as_rect(windows[i])
        classified = _classify_handle_for_rect(
            px, py, x, y, ww, hh, handle_px, corner_px
        )
        if classified is None:
            continue
        handle, dist = classified
        inside = x <= px < x + ww and y <= py < y + hh
        if inside:
            if handle != "body":
                if interior_resize is None:
                    interior_resize = (i, handle)
            elif interior_body is None:
                interior_body = (i, "body")
        elif handle != "body":
            if best_exterior is None or dist < best_exterior[0]:
                best_exterior = (dist, i, handle)

    if interior_resize is not None:
        return interior_resize
    if best_exterior is not None:
        return best_exterior[1], best_exterior[2]
    if interior_body is not None:
        return interior_body
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
