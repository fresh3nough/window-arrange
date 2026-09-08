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


def split_axis_with_floors(
    total: int, floors: list[int], gap: int, weights: list[int] | None = None
) -> list[tuple[int, int]]:
    """Partition `total` honoring absolute min sizes, then weight the remainder.

    Toolkit clamps need absolute px (Goose/1Password ~400h), not only relative
    weight — a 5-stack weighted split otherwise gives Goose ~300 of 854 usable.
    When floors sum above usable, scale them down proportionally (best effort).
    """
    n = len(floors)
    if n <= 0:
        return []
    if n == 1:
        return [(0, max(1, total))]
    gap = max(0, gap)
    usable = total - gap * (n - 1)
    if usable < n:
        usable = n
    mins = [max(1, int(f)) for f in floors]
    need = sum(mins)
    if need > usable:
        # Protect large floors (tall toolkit mins ~400) first; shrink flexible
        # donors down to a hard 80px before touching a tall floor. Only if still
        # over budget do we scale the protected floors.
        hard_flex = 80
        protected = [i for i, m in enumerate(mins) if m >= 350]
        flex = [i for i in range(n) if i not in protected]
        sizes = list(mins)
        # Collapse flexible first.
        for i in flex:
            sizes[i] = hard_flex
        over = sum(sizes) - usable
        if over > 0 and flex:
            # Already at hard_flex; need to cut protected.
            pass
        elif over < 0:
            # Give remainder back by weight among all.
            rem = -over
            wts = [max(1, int(w)) for w in (weights if weights is not None else floors)]
            wsum = sum(wts) or 1
            for i in range(n):
                add = int(rem * wts[i] / wsum)
                sizes[i] += add
            drift = usable - sum(sizes)
            i = 0
            while drift != 0 and n > 0:
                if drift > 0:
                    sizes[i % n] += 1
                    drift -= 1
                else:
                    floor_i = hard_flex if i % n in flex else (350 if i % n in protected else 1)
                    if sizes[i % n] > floor_i:
                        sizes[i % n] -= 1
                        drift += 1
                i += 1
                if i > n * (abs(drift) + 2):
                    break
        if sum(sizes) > usable:
            # Still over: scale everything, but bias cut toward flex.
            over = sum(sizes) - usable
            for i in flex:
                if over <= 0:
                    break
                cut = min(over, max(0, sizes[i] - hard_flex))
                sizes[i] -= cut
                over -= cut
            if over > 0:
                for i in protected:
                    if over <= 0:
                        break
                    cut = min(over, max(0, sizes[i] - hard_flex))
                    sizes[i] -= cut
                    over -= cut
            if over > 0:
                scale = usable / max(sum(sizes), 1)
                sizes = [max(1, int(s * scale)) for s in sizes]
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
    else:
        rem = usable - need
        wts = [max(1, int(w)) for w in (weights if weights is not None else floors)]
        wsum = sum(wts)
        extra = [int(rem * w / wsum) for w in wts]
        sizes = [mins[i] + extra[i] for i in range(n)]
        drift = usable - sum(sizes)
        i = 0
        while drift != 0 and n > 0:
            if drift > 0:
                sizes[i % n] += 1
                drift -= 1
            else:
                # Never shrink below floor.
                if sizes[i % n] > mins[i]:
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

    Example n=5 → 3+2 two-col stacks. n=7 → prefer 3+2+2 three-col so two
    tall apps (~400h) can each sit in a 2-row stack; two-col 4+3 puts both
    tall apps in a 3-row band (~300h) and their clamps overlap.
    Optional per-column height_weights grow tall-min apps inside a stack.
    Cells are returned column-major (left→right, top→bottom within each).
    """
    if n <= 0:
        return []
    prefer_rh = 400
    # Soft floor lets 7-up take 3 columns on ~1440 logical (~460w) when two
    # columns would crush row height below tall toolkit mins.
    soft_col_w = max(360, min(int(min_col_w), 460))
    max_by_hard = max(1, (grid_w + gap) // (max(min_col_w, 1) + gap))
    max_by_soft = max(1, (grid_w + gap) // (soft_col_w + gap))
    max_by_w = max(max_by_hard, max_by_soft if n >= 7 else max_by_hard)
    best_cols = 2 if n >= 2 else 1
    for k in range(1, min(n, max_by_w) + 1):
        rows_k = math.ceil(n / k)
        rh = (grid_h - gap * (rows_k - 1)) / max(rows_k, 1)
        cw = (grid_w - gap * (k - 1)) / k
        floor = min_col_w * 0.9 if k <= max_by_hard else soft_col_w * 0.9
        if cw < floor:
            continue
        # Prefer the smallest k that keeps densest stack rows above prefer_rh.
        if rh >= prefer_rh:
            best_cols = k
            break
        best_cols = k
    # n=5 always two-col 3+2 (classic).
    if n == 5:
        best_cols = 2
    # n=7/8: force 3-col 3+2+2 / 3+3+2 when width clears soft floor so two
    # tall apps land in 2-row stacks (~445h) instead of sharing a 3-row band.
    if n in (7, 8) and grid_w >= soft_col_w * 3 + gap * 2:
        best_cols = max(best_cols, 3)
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
    # Secondary: height-crushed multi-row. Prefer stacks whenever average row
    # height drops under tall toolkit mins (~400), even if cell width is fine
    # (7-up on ~1440 logical is 2×4 at ~216h — Goose/1Password clamp hard).
    soft_cw = max(160, min(prefer_cw, 460))
    if (
        can_stack
        and n >= 5
        and densest_rh + 0.5 < prefer_rh
        and not (n % 2 == 0 and rows == 2 and densest_cw >= soft_cw and densest_rh >= prefer_rh * 0.85)
    ):
        return place_column_stacks(
            n, grid_x0, grid_y0, grid_w, grid_h, gap, min_col_w=prefer_cw
        )
    # Tertiary: 7+ on wide canvases that can host 3 stack columns.
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
    """Match windows to cells: min-size-heavy apps get the best cells first.

    Preference order for a demanding app:
      1. taller cell height (fits ~400h clamps)
      2. fewer siblings in the same column stack (2-row beats 3-row when both
         are ~tall — avoids seating two Goose/1Password clamps in one 3-stack)
      3. wider cell width (fits ~784w / Chromium ~500)
    """
    if not windows or not cells:
        return []
    # How many cells share each column band (same x+w).
    stack_size: list[int] = [1] * len(cells)
    for idxs in _group_cells_by_column(cells):
        for i in idxs:
            stack_size[i] = len(idxs)

    def cell_key(i: int) -> tuple:
        x, y, w, h = cells[i]
        # Sort descending via negatives where needed in reverse=True below.
        return (h, -stack_size[i], w, w * h)

    cell_order = sorted(range(len(cells)), key=cell_key, reverse=True)
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
    # Track how many tall apps already took a seat in each column band so a
    # second 400h clamp prefers a different stack.
    tall_seated: dict[tuple[int, int], int] = {}
    for wi in win_order:
        win = windows[wi]
        need_tall = wants_tall(win)
        chosen = None
        for ci in cell_order:
            if ci in used_cells:
                continue
            x, _y, w, _h = cells[ci]
            band = (x, w)
            if need_tall and tall_seated.get(band, 0) >= 1 and stack_size[ci] >= 3:
                # Leave room: another tall already owns this deep stack.
                continue
            chosen = ci
            break
        if chosen is None:
            for ci in cell_order:
                if ci not in used_cells:
                    chosen = ci
                    break
        if chosen is None:
            continue
        pairs[wi] = (win, cells[chosen])
        used_cells.add(chosen)
        if need_tall:
            x, _y, w, _h = cells[chosen]
            band = (x, w)
            tall_seated[band] = tall_seated.get(band, 0) + 1
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


def _group_cells_by_row(
    cells: list[tuple[int, int, int, int]],
) -> list[list[int]]:
    """Indices of cells that share a row band (same y and height), left→right."""
    if not cells:
        return []
    by_band: dict[tuple[int, int], list[int]] = {}
    for i, (_x, y, _w, h) in enumerate(cells):
        by_band.setdefault((y, h), []).append(i)
    rows: list[list[int]] = []
    for key in sorted(by_band):
        idxs = by_band[key]
        idxs.sort(key=lambda i: cells[i][0])
        rows.append(idxs)
    return rows


def _height_weight_for(win: dict[str, Any]) -> int:
    """Vertical split weight so tall toolkit mins keep usable donors."""
    _mw, mh = toolkit_min_size(win)
    base = max(mh, 160)
    if wants_tall(win):
        base = max(base, 400)
    if wants_wide(win) and not wants_tall(win):
        base = max(base, 220)
    return base


def _width_weight_for(win: dict[str, Any]) -> int:
    """Horizontal split weight so wide clamps (1Password ~784) keep a column."""
    mw, _mh = toolkit_min_size(win)
    base = max(mw, 200)
    if wants_wide(win):
        base = max(base, mw, 500)
    return base


def _columns_span_full_height(
    col_groups: list[list[int]],
    cells: list[tuple[int, int, int, int]],
    grid_y0: int,
    grid_h: int,
    tol: int = 3,
) -> bool:
    """True when every column band covers the full grid height (stack layout)."""
    if not col_groups or grid_h <= 0:
        return False
    bottom = grid_y0 + grid_h
    for idxs in col_groups:
        top = min(cells[i][1] for i in idxs)
        bot = max(cells[i][1] + cells[i][3] for i in idxs)
        if abs(top - grid_y0) > tol or abs(bot - bottom) > tol:
            return False
    return True


def fit_pairs_to_toolkit_mins(
    pairs: list[tuple[dict[str, Any], tuple[int, int, int, int]]],
    gap: int,
    grid_x0: int | None = None,
    grid_y0: int | None = None,
    grid_w: int | None = None,
    grid_h: int | None = None,
) -> list[tuple[dict[str, Any], tuple[int, int, int, int]]]:
    """Re-flow cell sizes so toolkit min widths/heights fit without overlap.

    1. Height: true vertical stacks (same x+w) share a weighted split so
       Goose/1Password ~400h does not overflow a 3-up column.
    2. Width: full-height stack columns get a weighted horizontal split so
       1Password's ~784w clamp is planned into the column (equal half-width
       columns of ~698 on HiDPI were pinning top-left and cutting off the
       right edge). Non-stack row grids reflow each row band the same way.
    """
    if not pairs:
        return []
    cells = [cell for _w, cell in pairs]
    new_cells: list[tuple[int, int, int, int]] = [c for c in cells]

    # Infer grid box from cells when caller omits it (tests / older callers).
    if grid_x0 is None:
        grid_x0 = min(c[0] for c in new_cells)
    if grid_y0 is None:
        grid_y0 = min(c[1] for c in new_cells)
    if grid_w is None:
        grid_w = max(c[0] + c[2] for c in new_cells) - grid_x0
    if grid_h is None:
        grid_h = max(c[1] + c[3] for c in new_cells) - grid_y0

    col_groups = _group_cells_by_column(new_cells)

    # --- vertical reflow inside each column stack ---
    for idxs in col_groups:
        if len(idxs) <= 1:
            continue
        top = min(new_cells[i][1] for i in idxs)
        bottom = max(new_cells[i][1] + new_cells[i][3] for i in idxs)
        total_h = bottom - top
        x = new_cells[idxs[0]][0]
        w = new_cells[idxs[0]][2]
        floors = []
        weights = []
        for i in idxs:
            win = pairs[i][0]
            _mw, mh = toolkit_min_size(win)
            # Absolute floor so Goose/1Password keep ~400h when the column can.
            fl = max(mh if wants_tall(win) else min(mh, 200), 120)
            floors.append(fl)
            weights.append(_height_weight_for(win))
        bands = split_axis_with_floors(total_h, floors, gap, weights)
        for band_i, cell_i in enumerate(idxs):
            ry, rh = bands[band_i]
            new_cells[cell_i] = (x, top + ry, w, rh)

    # --- horizontal reflow ---
    col_groups = _group_cells_by_column(new_cells)
    if (
        len(col_groups) >= 2
        and _columns_span_full_height(col_groups, new_cells, grid_y0, grid_h)
    ):
        # Stack layout: one width per column, weighted by the hungriest app.
        weights = [
            max(_width_weight_for(pairs[i][0]) for i in idxs) for idxs in col_groups
        ]
        bands = split_axis_weighted(grid_w, weights, gap)
        for col_i, idxs in enumerate(col_groups):
            cx, cw = bands[col_i]
            nx = grid_x0 + cx
            for cell_i in idxs:
                _x, y, _w, h = new_cells[cell_i]
                new_cells[cell_i] = (nx, y, cw, h)
    else:
        # Row grid (2+3 etc.): weight widths inside each shared y-band.
        for idxs in _group_cells_by_row(new_cells):
            if len(idxs) <= 1:
                continue
            left = min(new_cells[i][0] for i in idxs)
            right = max(new_cells[i][0] + new_cells[i][2] for i in idxs)
            total_w = right - left
            weights = [_width_weight_for(pairs[i][0]) for i in idxs]
            bands = split_axis_weighted(total_w, weights, gap)
            y = new_cells[idxs[0]][1]
            h = new_cells[idxs[0]][3]
            for band_i, cell_i in enumerate(idxs):
                cx, cw = bands[band_i]
                new_cells[cell_i] = (left + cx, y, cw, h)

    return [(pairs[i][0], new_cells[i]) for i in range(len(pairs))]


def _oversized_width_clamps(
    windows: list[dict[str, Any]],
    grid_w: int,
    gap: int,
) -> list[dict[str, Any]]:
    """Windows whose min width cannot fit in a half-grid column (1Password ~784)."""
    half = (grid_w - gap) // 2 if grid_w > gap else grid_w
    out: list[dict[str, Any]] = []
    for w in windows:
        mw, _mh = toolkit_min_size(w)
        if mw > half:
            out.append(w)
    return out


def pack_window_cells(
    windows: list[dict[str, Any]],
    grid_x0: int,
    grid_y0: int,
    grid_w: int,
    grid_h: int,
    gap: int,
) -> list[tuple[dict[str, Any], tuple[int, int, int, int]]]:
    """Plan non-overlapping cells sized for the windows' toolkit mins.

    When one app (1Password ~784w) exceeds half the grid, reserve a full-height
    strip of that width on the right and pack the rest into the leftover
    region. Equal half-width stacks cannot honor the clamp without cutting the
    right edge off-screen; a dedicated strip + leftover pack keeps every frame
    on-monitor and non-overlapping for 5–7 apps.
    """
    n = len(windows)
    if n <= 0:
        return []

    half = (grid_w - gap) // 2 if grid_w > gap else 500
    oversize = _oversized_width_clamps(windows, grid_w, gap)

    # Dedicated strip path: exactly one oversize clamp and enough leftover for
    # the remaining windows (need ≥360px so Chromium/foot stay usable).
    if len(oversize) == 1 and n >= 2:
        big = oversize[0]
        mw, mh = toolkit_min_size(big)
        # Cap strip so leftover stays useful; never exceed the grid.
        strip_w = min(max(mw, 1), grid_w)
        leftover_w = grid_w - strip_w - gap
        min_left = 360 if n >= 3 else 280
        if leftover_w >= min_left and strip_w + gap + leftover_w <= grid_w:
            rest = [w for w in windows if w is not big]
            # Prefer putting the wide strip on the right (matches phone strip).
            left_x0 = grid_x0
            strip_x0 = grid_x0 + leftover_w + gap

            # When leftover would crush many windows AND the strip can host two
            # ≥400h rows, seat a second tall app (Goose/code) in the strip.
            strip_wins: list[dict[str, Any]] = [big]
            prefer_rh = 400
            two_row_h = (grid_h - gap) // 2
            if (
                len(rest) >= 5
                and two_row_h >= prefer_rh
                and mh <= two_row_h + 20
            ):
                partners = sorted(
                    [w for w in rest if wants_tall(w)],
                    key=lambda w: toolkit_min_size(w)[1],
                    reverse=True,
                )
                if partners:
                    partner = partners[0]
                    rest = [w for w in rest if w is not partner]
                    strip_wins.append(partner)

            # Leftover next to a 784 strip is often ~600w — too narrow for two
            # Chromium-safe columns. Force a single full-width stack so every
            # rest cell is leftover_w wide (clamps cannot spill into the strip).
            rest_cells: list[tuple[int, int, int, int]] = []
            if rest:
                rest_demand = max(
                    (toolkit_min_size(w)[0] for w in rest), default=500
                )
                can_two = leftover_w >= rest_demand * 2 + gap
                if can_two:
                    rest_prefer = min_cell_width_for(leftover_w, gap)
                    rest_cells = place_grid(
                        len(rest),
                        left_x0,
                        grid_y0,
                        leftover_w,
                        grid_h,
                        gap,
                        min_cell_w=rest_prefer,
                    )
                else:
                    # One column, weighted heights for tall rest apps.
                    weights = [_height_weight_for(w) for w in rest]
                    # Sort rest so tall apps get weight via assign after equal
                    # bands; fit_pairs will reweight. Use equal bands first.
                    bands = split_axis(grid_h, len(rest), gap)
                    rest_cells = [
                        (left_x0, grid_y0 + ry, leftover_w, rh)
                        for ry, rh in bands
                    ]
            rest_pairs = (
                assign_windows_to_cells(rest, rest_cells) if rest else []
            )
            if rest_pairs:
                rest_pairs = fit_pairs_to_toolkit_mins(
                    rest_pairs,
                    gap,
                    grid_x0=left_x0,
                    grid_y0=grid_y0,
                    grid_w=leftover_w,
                    grid_h=grid_h,
                )

            # Strip cells: full-height solo, or floor-aware 2-row stack.
            if len(strip_wins) == 1:
                strip_pairs = [
                    (strip_wins[0], (strip_x0, grid_y0, strip_w, grid_h))
                ]
            else:
                floors = [toolkit_min_size(w)[1] for w in strip_wins]
                weights = [_height_weight_for(w) for w in strip_wins]
                bands = split_axis_with_floors(grid_h, floors, gap, weights)
                strip_pairs = [
                    (w, (strip_x0, grid_y0 + ry, strip_w, rh))
                    for w, (ry, rh) in zip(strip_wins, bands)
                ]
            return list(rest_pairs) + strip_pairs

    # Standard path: stack/grid + height/width weight reflow.
    demand_w = max((toolkit_min_size(w)[0] for w in windows), default=500)
    prefer_cw = max(
        min_cell_width_for(grid_w, gap),
        min(demand_w, half) if demand_w <= half else half,
    )
    cells = place_grid(
        n, grid_x0, grid_y0, grid_w, grid_h, gap, min_cell_w=prefer_cw
    )
    pairs = assign_windows_to_cells(windows, cells)
    return fit_pairs_to_toolkit_mins(
        pairs, gap, grid_x0=grid_x0, grid_y0=grid_y0, grid_w=grid_w, grid_h=grid_h
    )


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
                    # Work-area bounds for settle pins of oversize clamps.
                    "bound_x0": int(x0),
                    "bound_y0": int(y0),
                    "bound_x1": int(x1),
                    "bound_y1": int(y1),
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
                        "bound_x0": int(x0),
                        "bound_y0": int(y0),
                        "bound_x1": int(x1),
                        "bound_y1": int(y1),
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
