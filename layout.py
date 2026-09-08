#!/usr/bin/env python3
"""Layout planner for window-arrange.

Hyprland reports monitor width/height in *physical* pixels and client
geometry in *logical* pixels (physical / scale). moveactive/resizeactive
also expect logical coordinates. This module always plans in logical space
so the same defaults work on scale 1.0 (many desktops) and fractional
scale (e.g. Framework / HiDPI Omarchy laptops ~1.57).

Grid cells are built by integer partition of the work area so neighboring
windows never share pixels (gap is true empty space). Cell sizing prefers
browser-friendly minimum widths so toolkits that clamp min-size cannot
push one window over the next.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from typing import Any


def logical_monitor_box(mon: dict[str, Any]) -> tuple[int, int, int, int, float]:
    """Return (x, y, logical_w, logical_h, scale) for a hyprctl monitor object.

    x/y are already in the global layout coordinate system (logical).
    width/height are physical and must be divided by scale.
    """
    scale = float(mon.get("scale") or 1.0)
    if scale <= 0:
        scale = 1.0
    # Round to nearest pixel; Hyprland logical sizes are integral for clients.
    lw = max(1, int(round(int(mon["width"]) / scale)))
    lh = max(1, int(round(int(mon["height"]) / scale)))
    mx = int(mon.get("x", 0))
    my = int(mon.get("y", 0))
    return mx, my, lw, lh, scale


def work_area(
    mon: dict[str, Any], outer: int
) -> tuple[int, int, int, int, int, int]:
    """Padded usable rectangle in logical pixels.

    Returns (x0, y0, x1, y1, area_w, area_h).
    reserved from hyprctl is [left, top, right, bottom] in logical px.
    """
    mx, my, mw, mh, _scale = logical_monitor_box(mon)
    res = mon.get("reserved") or [0, 0, 0, 0]
    rl, rt, rr, rb = (int(res[0]), int(res[1]), int(res[2]), int(res[3]))
    outer = max(0, int(outer))
    x0 = mx + rl + outer
    y0 = my + rt + outer
    x1 = mx + mw - rr - outer
    y1 = my + mh - rb - outer
    area_w = max(200, x1 - x0)
    area_h = max(200, y1 - y0)
    return x0, y0, x1, y1, area_w, area_h


def is_phone(c: dict[str, Any]) -> bool:
    cls = (c.get("class") or "").lower()
    title = (c.get("title") or "").lower()
    return cls == "scrcpy" or title == "pixel" or "scrcpy" in cls


def wants_wide(c: dict[str, Any]) -> bool:
    """True for toolkits that often clamp to a large minimum width (~480–500px)."""
    cls = (c.get("class") or "").lower()
    title = (c.get("title") or "").lower()
    needles = (
        "chromium", "chrome", "firefox", "brave", "msedge", "vivaldi",
        "browser", "zen", "librewolf",
        # Electron shells on this HiDPI panel also refuse narrow frames.
        "goose", "electron", "slack", "discord", "code", "cursor", "1password",
    )
    return any(n in cls or n in title for n in needles)


def wants_tall(c: dict[str, Any]) -> bool:
    """True for apps that often clamp to a large minimum height (~400px)."""
    cls = (c.get("class") or "").lower()
    title = (c.get("title") or "").lower()
    needles = (
        "goose", "electron", "slack", "discord", "code", "cursor",
        "1password", "bitwarden",
    )
    return any(n in cls or n in title for n in needles)


def toolkit_min_size(c: dict[str, Any]) -> tuple[int, int]:
    """Observed Wayland min sizes on Omarchy HiDPI (logical px).

    Clients silently refuse smaller resize requests and keep their min frame,
    which shifts a center-anchored resize into neighboring cells. Planning at
    or above these floors keeps top-left placement honest.
    """
    cls = (c.get("class") or "").lower()
    title = (c.get("title") or "").lower()
    # Browsers: hard min width ~500; height is flexible.
    if any(
        n in cls or n in title
        for n in (
            "chromium",
            "chrome",
            "firefox",
            "brave",
            "msedge",
            "vivaldi",
            "zen",
            "librewolf",
        )
    ):
        return 500, 200
    # 1Password (and Bitwarden): Omarchy float-tag apps; hard width clamp is
    # ~784 logical px on this HiDPI panel — far above the generic Electron floor.
    # Planning a narrower cell center-anchors the clamp and drifts top-left into
    # the neighbor (seen at 617-wide 3-col cells).
    if any(n in cls or n in title for n in ("1password", "bitwarden")):
        return 784, 400
    # Electron / Goose: both axes clamp.
    if any(n in cls or n in title for n in ("goose", "electron", "slack", "discord", "code", "cursor")):
        return 480, 400
    # GTK file manager: min height ~380, width ~360.
    if "nautilus" in cls or "nautilus" in title:
        return 360, 380
    if "diskutility" in cls or "gnome-disks" in cls or "baobab" in cls:
        return 360, 200
    return 200, 120


def min_cell_width_for(grid_w: int, gap: int) -> int:
    """Largest per-cell width we can demand and still fit two gutters on a row.

    Used to cap column count so toolkit min-size clamps cannot spill.
    Chromium's hard min is ~500 logical px on this HiDPI panel.
    """
    target = 500
    # Never demand more than half the grid (2-col always fits target when grid
    # is wide enough); still allow narrower grids to drop the target.
    half = (grid_w - gap) // 2 if grid_w > gap else target
    return max(360, min(target, half))


def adaptive_defaults(area_w: int, area_h: int) -> dict[str, int]:
    """Derive gap / outer / phone strip sizes from the logical work area.

    Slightly roomier than the first cut so five-up grids keep visible gutters
    on both HiDPI logical ~1440-wide and scale-1 Mac-like panels.
    """
    # Reference: ~1440x960 logical (2256x1504 @ 1.5667) and similar MacBooks.
    ref_w, ref_h = 1440, 900
    factor = math.sqrt((max(area_w, 1) * max(area_h, 1)) / (ref_w * ref_h))
    factor = max(0.75, min(1.35, factor))
    # Roomier gutters: gap ~12, outer ~16 at reference density.
    gap = max(8, int(round(12 * factor)))
    outer = max(10, int(round(16 * factor)))
    phone_w = max(240, min(420, int(round(area_w * 0.25))))
    phone_h = max(480, min(area_h, int(round(phone_w * 2.1))))
    return {"gap": gap, "outer": outer, "phone_w": phone_w, "phone_h": phone_h}


def split_axis(total: int, count: int, gap: int) -> list[tuple[int, int]]:
    """Partition `total` px into `count` segments separated by `gap`.

    Returns list of (offset_from_start, size). Sizes differ by at most 1px
    so the sum of sizes + gaps equals `total` exactly — no leftover strip
    and no neighbor overlap from truncated floats.
    """
    if count <= 0:
        return []
    if count == 1:
        return [(0, max(1, total))]
    gap = max(0, gap)
    usable = total - gap * (count - 1)
    if usable < count:
        # Degenerate: give everyone 1px and accept clipping rather than crash.
        usable = count
    base, rem = divmod(usable, count)
    out: list[tuple[int, int]] = []
    pos = 0
    for i in range(count):
        size = base + (1 if i < rem else 0)
        out.append((pos, size))
        pos += size + gap
    return out


def rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """Axis-aligned overlap test; touching edges (share a line) is OK."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def choose_grid(n: int, grid_w: int, grid_h: int, gap: int) -> tuple[int, int]:
    """Pick (cols, rows) for n windows.

    Caps column count so average cell width stays at or above toolkit min
    widths (~500 logical px). Prefers two rows when height allows so electron
    shells keep ~400px height.

    For six windows on ~1400-wide logical canvases, a 3x2 grid (~461x440)
    beats a 2x3 / dual stack of three (~700x292): height under ~400 is worse
    for Goose/Electron than a modestly-narrower browser column.
    """
    if n <= 0:
        return 0, 0

    prefer_cw = min_cell_width_for(grid_w, gap)
    prefer_rh = 400
    hard_cw = max(160, min(240, grid_w // 5 if grid_w else 160))
    hard_rh = max(120, min(160, grid_h // 4 if grid_h else 120))
    # Prefer columns that hold prefer_cw; allow one extra col when a 2-row
    # layout needs it to keep row height above toolkit clamps.
    max_cols = max(1, (grid_w + gap) // (prefer_cw + gap))
    # Soft width for the extra column (still above hard_cw).
    soft_cw = max(hard_cw, min(prefer_cw, 460))
    soft_max_cols = max(max_cols, (grid_w + gap) // (soft_cw + gap))

    best: tuple[float, int, int] | None = None
    for cols in range(1, n + 1):
        rows = math.ceil(n / cols)
        cw = (grid_w - gap * (cols - 1)) / cols
        rh = (grid_h - gap * (rows - 1)) / rows
        if cw < hard_cw or rh < hard_rh:
            continue
        empty = cols * rows - n
        aspect = cw / max(rh, 1.0)
        score = abs(math.log(max(aspect, 0.05) / 1.45))
        score += 0.35 * empty
        score += 0.10 * abs(cols - math.ceil(math.sqrt(n)))
        if cw < prefer_cw:
            score += 2.0 * ((prefer_cw - cw) / prefer_cw)
        if rh < prefer_rh:
            # Height clamps (Goose ~400) hurt more than a mild width shortfall.
            score += 2.4 * ((prefer_rh - rh) / prefer_rh)
        if rows >= 3 and rh < prefer_rh:
            score += 1.8
        # Prefer filling even counts into two equal rows when width allows.
        if n >= 6 and rows == 2 and empty == 0 and cw >= soft_cw:
            score -= 0.8
        # Heavy penalty past soft max; light penalty between prefer and soft.
        if cols > soft_max_cols:
            score += 3.0 * (cols - soft_max_cols)
        elif cols > max_cols:
            score += 0.9 * (cols - max_cols)
        if best is None or score < best[0]:
            best = (score, cols, rows)

    if best is None:
        cols = max(1, min(n, soft_max_cols))
        rows = int(math.ceil(n / cols))
        return cols, rows
    return best[1], best[2]


def row_counts_for(n: int, cols: int, rows: int) -> list[int]:
    """How many windows per row (top → bottom).

    Five windows:
      - 2 rows / max 2 cols → impossible as pure grid; use 2+3 only when
        3-col width still safe, else 2+2+1 (full-width last row).
      - 2 rows / 3 cols → 2+3 (wide apps on the 2-cell row).
    """
    if n <= 0 or rows <= 0:
        return []
    if n == 5 and rows == 2 and cols >= 3:
        return [2, 3]
    if n == 5 and rows == 2 and cols <= 2:
        # 2-col cap cannot host 2+3; fall through to even split (stacks handle
        # the real 5-up path via place_column_stacks).
        return [2, 3] if cols >= 3 else [3, 2]
    if n == 5 and rows >= 3:
        return [2, 2, 1]
    counts: list[int] = []
    left = n
    for r in range(rows):
        if left <= 0:
            break
        take = min(cols, left)
        if r == rows - 1:
            take = left
        counts.append(take)
        left -= take
    return counts


def split_axis_weighted(
    total: int, weights: list[int], gap: int
) -> list[tuple[int, int]]:
    """Partition `total` px into len(weights) segments by relative weight + gap.

    Guarantees each segment is at least 1px and the span covers `total` exactly
    (same contract as split_axis). Weights are clamped to >= 1.
    """
    n = len(weights)
    if n <= 0:
        return []
    if n == 1:
        return [(0, max(1, total))]
    gap = max(0, gap)
    usable = total - gap * (n - 1)
    if usable < n:
        usable = n
    wts = [max(1, int(w)) for w in weights]
    wsum = sum(wts)
    raw = [usable * w / wsum for w in wts]
    sizes = [max(1, int(v)) for v in raw]
    # Fix rounding drift so sum(sizes) == usable.
    drift = usable - sum(sizes)
    i = 0
    while drift != 0 and n > 0:
        if drift > 0:
            sizes[i % n] += 1
            drift -= 1
        else:
            if sizes[i % n] > 1:
                sizes[i % n] -= 1
                drift += 1
        i += 1
        if i > n * (abs(drift) + 2):
            break
    out: list[tuple[int, int]] = []
    pos = 0
    for size in sizes:
        out.append((pos, size))
        pos += size + gap
    return out


def _stack_column_counts(n: int, n_cols: int) -> list[int]:
    """How many windows per stack column (left → right), summing to n."""
    if n <= 0 or n_cols <= 0:
        return []
    if n_cols == 1:
        return [n]
    if n_cols == 2:
        # 5 → 3+2 (taller right pair). 6 → 3+3. 7 → 4+3 (legacy two-col).
        if n == 5:
            return [3, 2]
        right = n // 2
        return [n - right, right]
    # 3+ columns: balance, bias leftover to the left (more flexible apps).
    base, rem = divmod(n, n_cols)
    counts = [base + (1 if i < rem else 0) for i in range(n_cols)]
    # Prefer not leaving a 1-cell middle column when n=7 → 3+2+2.
    if n == 7 and n_cols == 3:
        return [3, 2, 2]
    if n == 8 and n_cols == 3:
        return [3, 3, 2]
    return counts


def place_column_stacks(
    n: int,
    grid_x0: int,
    grid_y0: int,
    grid_w: int,
    grid_h: int,
    gap: int,
    height_weights: list[list[int]] | None = None,
    *,
    min_col_w: int = 500,
) -> list[tuple[int, int, int, int]]:
    """Multi-column stack layout when a uniform grid would crush toolkit mins.

    Example n=5 → 3+2 two-col stacks. n=7 on a wide canvas → 3+2+2 three-col
    so tall apps (~400h) still fit; two-col 4+3 would leave ~240px rows.
    Optional per-column height_weights grow tall-min apps inside a stack.
    Cells are returned column-major (left→right, top→bottom within each).
    """
    if n <= 0:
        return []
    # As many columns as width allows at min_col_w, capped so each stack has
    # enough height for a tall min (~400) when possible.
    max_by_w = max(1, (grid_w + gap) // (max(min_col_w, 1) + gap))
    # Rows needed if we use k columns ≈ ceil(n/k); want row height ≥ 400.
    prefer_rh = 400
    best_cols = 2 if n >= 2 else 1
    for k in range(1, min(n, max_by_w) + 1):
        rows_k = math.ceil(n / k)
        rh = (grid_h - gap * (rows_k - 1)) / max(rows_k, 1)
        cw = (grid_w - gap * (k - 1)) / k
        if cw < min_col_w * 0.9:
            continue
        # Prefer the smallest k that keeps rh above prefer_rh; otherwise max k.
        if rh >= prefer_rh:
            best_cols = k
            break
        best_cols = k
    # n=5 always two-col 3+2 (classic).
    if n == 5:
        best_cols = 2
    counts = _stack_column_counts(n, best_cols)
    col_bands = split_axis(grid_w, len(counts), gap)
    cells: list[tuple[int, int, int, int]] = []
    for col_i, ((cx, cw), count) in enumerate(zip(col_bands, counts)):
        if count <= 0:
            continue
        weights = None
        if height_weights is not None and col_i < len(height_weights):
            weights = height_weights[col_i]
        if weights is not None and len(weights) == count:
            row_bands = split_axis_weighted(grid_h, weights, gap)
        else:
            row_bands = split_axis(grid_h, count, gap)
        for ry, rh in row_bands:
            cells.append((grid_x0 + cx, grid_y0 + ry, cw, rh))
    return cells


def place_grid(
    n: int,
    grid_x0: int,
    grid_y0: int,
    grid_w: int,
    grid_h: int,
    gap: int,
    *,
    min_cell_w: int | None = None,
) -> list[tuple[int, int, int, int]]:
    """Return n non-overlapping (x, y, w, h) cells covering the grid with gutters.

    Uses a uniform row grid when every cell can stay at toolkit-safe size.
    Falls back to two-column stacks whenever a uniform grid would force a cell
    narrower than Chromium's ~500px clamp (5- and 6-up on ~1440 logical).
    """
    if n <= 0:
        return []
    prefer_cw = min_cell_w if min_cell_w is not None else min_cell_width_for(grid_w, gap)
    prefer_rh = 400
    cols, rows = choose_grid(n, grid_w, grid_h, gap)
    if cols <= 0 or rows <= 0:
        return []

    densest_cols = cols
    if n == 5 and rows == 2:
        densest_cols = 3  # 2+3 layout
    densest_cw = (grid_w - gap * (densest_cols - 1)) / max(densest_cols, 1)
    densest_rh = (grid_h - gap * (rows - 1)) / max(rows, 1)

    # Any time the densest cell would be under the toolkit floor AND two
    # half-width columns still clear that floor, use stacks. Covers 5-up and
    # 6-up on HiDPI (~461px 3-col cells vs Chromium 500).
    can_stack = grid_w >= prefer_cw * 2 + gap
    if can_stack and densest_cw + 0.5 < prefer_cw:
        return place_column_stacks(
            n, grid_x0, grid_y0, grid_w, grid_h, gap, min_col_w=prefer_cw
        )
    # Secondary: height-crushed multi-row with soft width miss.
    soft_cw = max(160, min(prefer_cw, 460))
    if (
        can_stack
        and n >= 5
        and densest_cw < soft_cw
        and densest_rh < prefer_rh
        and not (n % 2 == 0 and rows == 2 and densest_cw >= soft_cw)
    ):
        return place_column_stacks(
            n, grid_x0, grid_y0, grid_w, grid_h, gap, min_col_w=prefer_cw
        )
    # Tertiary: 7+ windows where a uniform grid keeps width but crushes height
    # below tall toolkit mins — prefer multi-col stacks sized to prefer_cw.
    if (
        can_stack
        and n >= 7
        and densest_rh + 0.5 < prefer_rh
        and grid_w >= prefer_cw * 3 + gap * 2
    ):
        return place_column_stacks(
            n, grid_x0, grid_y0, grid_w, grid_h, gap, min_col_w=prefer_cw
        )

    counts = row_counts_for(n, cols, rows)
    row_bands = split_axis(grid_h, len(counts), gap)
    cells: list[tuple[int, int, int, int]] = []
    for r, row_count in enumerate(counts):
        col_bands = split_axis(grid_w, row_count, gap)
        ry, rh = row_bands[r]
        for c in range(row_count):
            cx, cw = col_bands[c]
            cells.append((grid_x0 + cx, grid_y0 + ry, cw, rh))
    return cells


def _window_demand(c: dict[str, Any]) -> tuple[int, int, int]:
    """Sort key: taller/wider toolkit mins first."""
    mw, mh = toolkit_min_size(c)
    return (
        2 if wants_tall(c) else 0,
        2 if wants_wide(c) else 0,
        mh + mw,
    )


def assign_windows_to_cells(
    windows: list[dict[str, Any]],
    cells: list[tuple[int, int, int, int]],
) -> list[tuple[dict[str, Any], tuple[int, int, int, int]]]:
    """Match windows to cells: min-size-heavy apps get the tallest+widest cells first."""
    if not windows or not cells:
        return []
    cell_order = sorted(
        range(len(cells)),
        key=lambda i: (cells[i][3], cells[i][2], cells[i][2] * cells[i][3]),
        reverse=True,
    )
    win_order = sorted(
        range(len(windows)),
        key=lambda i: (
            _window_demand(windows[i]),
            (windows[i].get("class") or "").lower(),
        ),
        reverse=True,
    )
    pairs: list[tuple[dict[str, Any], tuple[int, int, int, int]] | None] = [None] * len(windows)
    used_cells: set[int] = set()
    for wi in win_order:
        for ci in cell_order:
            if ci in used_cells:
                continue
            pairs[wi] = (windows[wi], cells[ci])
            used_cells.add(ci)
            break
    out: list[tuple[dict[str, Any], tuple[int, int, int, int]]] = []
    for i, p in enumerate(pairs):
        if p is None:
            for ci in range(len(cells)):
                if ci not in used_cells:
                    p = (windows[i], cells[ci])
                    used_cells.add(ci)
                    break
        if p is not None:
            out.append(p)
    return out


def _group_cells_by_column(
    cells: list[tuple[int, int, int, int]],
) -> list[list[int]]:
    """Indices of cells stacked in the same column band (same x and width).

    Keyed by (x, w) — not x alone. A 2+3 five-up grid puts a wide top-left
    cell and a narrower bottom-left cell on the same x; grouping those
    together and forcing one width was collapsing the 3-col bottom row into
    the top-row half-width and overlapping neighbors.
    """
    if not cells:
        return []
    by_band: dict[tuple[int, int], list[int]] = {}
    for i, (x, _y, w, _h) in enumerate(cells):
        by_band.setdefault((x, w), []).append(i)
    cols: list[list[int]] = []
    for key in sorted(by_band):
        idxs = by_band[key]
        idxs.sort(key=lambda i: cells[i][1])
        cols.append(idxs)
    return cols


def fit_pairs_to_toolkit_mins(
    pairs: list[tuple[dict[str, Any], tuple[int, int, int, int]]],
    gap: int,
) -> list[tuple[dict[str, Any], tuple[int, int, int, int]]]:
    """Re-flow heights inside each column band so toolkit min-heights fit.

    Only cells that share the same x and width are reflowed together (true
    stacks). When a band has three equal ~292px rows, Goose/1Password's 400px
    clamp overflows; this boosts tall cells and shrinks flexible ones
    (terminals) so the band still packs exactly with gutters and no overlap.
    """
    if not pairs:
        return []
    cells = [cell for _w, cell in pairs]
    col_groups = _group_cells_by_column(cells)
    # Work on a mutable copy of cell geometry.
    new_cells: list[tuple[int, int, int, int]] = [c for c in cells]

    for idxs in col_groups:
        if len(idxs) <= 1:
            # Single cell: keep planned geometry (clamp handled at apply time).
            continue

        # Column vertical span from current cells.
        top = min(new_cells[i][1] for i in idxs)
        bottom = max(new_cells[i][1] + new_cells[i][3] for i in idxs)
        total_h = bottom - top
        x = new_cells[idxs[0]][0]
        w = new_cells[idxs[0]][2]

        weights: list[int] = []
        for i in idxs:
            wwin = pairs[i][0]
            _mw, mh = toolkit_min_size(wwin)
            # Weight by min height; flexible apps keep a modest floor so they
            # still remain usable after donors give space to Goose/1Password.
            base = max(mh, 160)
            if wants_tall(wwin):
                base = max(base, 400)
            if wants_wide(wwin) and not wants_tall(wwin):
                base = max(base, 220)
            weights.append(base)

        # Prefer honoring weights; split_axis_weighted always fills total_h.
        bands = split_axis_weighted(total_h, weights, gap)
        for band_i, cell_i in enumerate(idxs):
            ry, rh = bands[band_i]
            new_cells[cell_i] = (x, top + ry, w, rh)

    return [(pairs[i][0], new_cells[i]) for i in range(len(pairs))]


def pack_window_cells(
    windows: list[dict[str, Any]],
    grid_x0: int,
    grid_y0: int,
    grid_w: int,
    grid_h: int,
    gap: int,
) -> list[tuple[dict[str, Any], tuple[int, int, int, int]]]:
    """Plan non-overlapping cells sized for the windows' toolkit mins."""
    n = len(windows)
    if n <= 0:
        return []
    # Demand the max observed min-width among this set so Chromium forces stacks.
    demand_w = max((toolkit_min_size(w)[0] for w in windows), default=500)
    prefer_cw = max(min_cell_width_for(grid_w, gap), min(demand_w, (grid_w - gap) // 2 if grid_w > gap else demand_w))
    cells = place_grid(
        n, grid_x0, grid_y0, grid_w, grid_h, gap, min_cell_w=prefer_cw
    )
    pairs = assign_windows_to_cells(windows, cells)
    return fit_pairs_to_toolkit_mins(pairs, gap)


def build_plan(
    monitors: list[dict[str, Any]],
    clients: list[dict[str, Any]],
    active: dict[str, Any],
    *,
    gap: int | None = None,
    outer: int | None = None,
    phone_w: int | None = None,
    phone_h: int | None = None,
    auto_adapt: bool = True,
) -> list[dict[str, Any]]:
    """Build placement plan (logical px) for mapped windows on the active workspace."""
    ws_id = active.get("id")
    mon = next((m for m in monitors if m.get("focused")), monitors[0])

    # First pass work area with a provisional outer to size adaptive defaults.
    probe_outer = 16 if outer is None else outer
    _x0, _y0, _x1, _y1, area_w, area_h = work_area(mon, probe_outer)
    derived = adaptive_defaults(area_w, area_h) if auto_adapt else {
        "gap": 12, "outer": 16, "phone_w": 360, "phone_h": 800,
    }

    gap_i = int(gap if gap is not None else derived["gap"])
    outer_i = int(outer if outer is not None else derived["outer"])
    phone_w_i = int(phone_w if phone_w is not None else derived["phone_w"])
    phone_h_i = int(phone_h if phone_h is not None else derived["phone_h"])

    # Recompute work area with the outer we will actually use.
    x0, y0, x1, y1, area_w, area_h = work_area(mon, outer_i)

    wins: list[dict[str, Any]] = []
    for c in clients:
        if not c.get("mapped") or c.get("hidden"):
            continue
        ws = c.get("workspace") or {}
        if ws.get("id") != ws_id:
            continue
        if str(ws.get("name", "")).startswith("special"):
            continue
        if not c.get("address"):
            continue
        wins.append(c)

    if not wins:
        return []

    phones = [c for c in wins if is_phone(c)]
    others = [c for c in wins if not is_phone(c)]
    others.sort(
        key=lambda c: (
            0 if not c.get("floating") else 1,
            (c.get("class") or "").lower(),
            (c.get("title") or "").lower(),
        )
    )

    plan: list[dict[str, Any]] = []
    phone = phones[0] if phones else None
    others = others + phones[1:]

    right_w = 0
    if phone:
        pw = min(phone_w_i, max(240, area_w // 5))
        ph = min(phone_h_i, area_h)
        if ph / max(pw, 1) < 1.7:
            ph = min(area_h, int(pw * 2.1))
        px = x1 - pw
        py = y0 + max(0, (area_h - ph) // 2)
        plan.append(
            {
                "address": phone["address"],
                "class": phone.get("class") or "",
                "role": "phone",
                "mode": "float",
                "x": int(px),
                "y": int(py),
                "w": int(pw),
                "h": int(ph),
                "fullscreen": phone.get("fullscreen") not in (0, False, None),
                "floating": bool(phone.get("floating")),
                "pinned": bool(phone.get("pinned")),
                "meta": {
                    "gap": gap_i,
                    "outer": outer_i,
                    "logical": list(logical_monitor_box(mon)[2:4]),
                },
            }
        )
        right_w = pw + gap_i

    grid_x0, grid_y0 = x0, y0
    grid_w = max(200, (x1 - right_w) - grid_x0)
    grid_h = max(200, y1 - grid_y0)
    n = len(others)

    if n > 0:
        pairs = pack_window_cells(others, grid_x0, grid_y0, grid_w, grid_h, gap_i)
        cols, rows = choose_grid(n, grid_w, grid_h, gap_i)
        # Reflect stack mode in meta when densest uniform cell would be too narrow.
        prefer_cw = min_cell_width_for(grid_w, gap_i)
        densest = (grid_w - gap_i * (max(cols, 1) - 1)) / max(cols, 1) if cols else grid_w
        if densest + 0.5 < prefer_cw and grid_w >= prefer_cw * 2 + gap_i:
            cols, rows = 2, max(1, (n + 1) // 2)
        for c, (cx, cy, cell_w, cell_h) in pairs:
            mw, mh = toolkit_min_size(c)
            plan.append(
                {
                    "address": c["address"],
                    "class": c.get("class") or "",
                    "role": "grid",
                    "mode": "float",
                    "x": int(cx),
                    "y": int(cy),
                    "w": int(cell_w),
                    "h": int(cell_h),
                    "fullscreen": c.get("fullscreen") not in (0, False, None),
                    "floating": bool(c.get("floating")),
                    "pinned": bool(c.get("pinned")),
                    "min_w": int(mw),
                    "min_h": int(mh),
                    "meta": {
                        "gap": gap_i,
                        "outer": outer_i,
                        "cols": cols,
                        "rows": rows,
                        "logical": list(logical_monitor_box(mon)[2:4]),
                        "toolkit_min": [int(mw), int(mh)],
                    },
                }
            )

    return plan


def load_json(cmd: list[str]) -> Any:
    return json.loads(subprocess.check_output(cmd, text=True))


def plan_from_hypr(
    gap: int | None = None,
    outer: int | None = None,
    phone_w: int | None = None,
    phone_h: int | None = None,
    auto_adapt: bool = True,
) -> list[dict[str, Any]]:
    monitors = load_json(["hyprctl", "monitors", "-j"])
    clients = load_json(["hyprctl", "clients", "-j"])
    active = load_json(["hyprctl", "activeworkspace", "-j"])
    return build_plan(
        monitors,
        clients,
        active,
        gap=gap,
        outer=outer,
        phone_w=phone_w,
        phone_h=phone_h,
        auto_adapt=auto_adapt,
    )


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return int(raw)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    # WINDOW_ARRANGE_ADAPT=0 disables density scaling (still uses logical geometry).
    auto = os.environ.get("WINDOW_ARRANGE_ADAPT", "1") not in ("0", "false", "False", "no")
    plan = plan_from_hypr(
        gap=_env_int("GAP"),
        outer=_env_int("OUTER"),
        phone_w=_env_int("PHONE_W"),
        phone_h=_env_int("PHONE_H"),
        auto_adapt=auto,
    )
    if not plan:
        print("NO_WINDOWS", file=sys.stderr)
        return 0
    for item in plan:
        out = {k: v for k, v in item.items() if k != "meta"}
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
