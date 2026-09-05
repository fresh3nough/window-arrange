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
    needles = ("goose", "electron", "slack", "discord", "code", "cursor")
    return any(n in cls or n in title for n in needles)


def min_cell_width_for(grid_w: int, gap: int) -> int:
    """Largest per-cell width we can demand and still fit two gutters on a row.

    Used to cap column count so toolkit min-size clamps cannot spill.
    """
    # Observed: Goose ~480, Chromium ~500 on logical ~1440 canvases.
    target = 500
    # Hard floor still leaves room for 2-col layouts on small screens.
    return max(360, min(target, (grid_w - gap) // 2 if grid_w > gap else target))


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
    if n == 5 and rows == 2 and cols <= 2:
        # Caller still asked for 2 rows with 2-col cap: 2+3 would need 3 cols.
        # Prefer 2+3 visually but only when cols allows; otherwise 2+2+1 via rows=3.
        return [2, 3] if cols >= 3 else [2, 3]
    if n == 5 and rows == 2 and cols >= 3:
        return [2, 3]
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


def place_column_stacks(
    n: int,
    grid_x0: int,
    grid_y0: int,
    grid_w: int,
    grid_h: int,
    gap: int,
) -> list[tuple[int, int, int, int]]:
    """Two-column stack layout for counts that cannot form a safe uniform grid.

    Example n=5 → left stack of 3 + right stack of 2. Every cell is ~half the
    work-area wide (browser-safe). The shorter stack gets taller rows so
    electron/goose min-heights fit; the taller stack holds flexible apps.
    Cells are returned left-stack first (top→bottom), then right-stack.
    """
    if n <= 0:
        return []
    # Prefer a slightly larger right stack when odd: right gets fewer, taller cells.
    right_n = n // 2
    left_n = n - right_n
    # For 5: left 3, right 2. For 7: left 4, right 3.
    if n == 5:
        left_n, right_n = 3, 2
    col_bands = split_axis(grid_w, 2, gap)
    cells: list[tuple[int, int, int, int]] = []
    for (cx, cw), count in ((col_bands[0], left_n), (col_bands[1], right_n)):
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
) -> list[tuple[int, int, int, int]]:
    """Return n non-overlapping (x, y, w, h) cells covering the grid with gutters.

    Uses a uniform row grid when every cell can stay at toolkit-safe size.
    Falls back to two-column stacks when a uniform grid would force sub-min
    widths (the five-app HiDPI case: 3-col ~461px < Chromium/Goose clamp).
    """
    if n <= 0:
        return []
    prefer_cw = min_cell_width_for(grid_w, gap)
    prefer_rh = 400
    cols, rows = choose_grid(n, grid_w, grid_h, gap)
    if cols <= 0 or rows <= 0:
        return []

    # Uniform grid safe if the densest row still meets min width and height.
    densest_cols = cols
    if n == 5 and rows == 2:
        densest_cols = 3  # 2+3 layout
    densest_cw = (grid_w - gap * (densest_cols - 1)) / max(densest_cols, 1)
    densest_rh = (grid_h - gap * (rows - 1)) / max(rows, 1)
    soft_cw = max(160, min(prefer_cw, 460))
    # Five-up on ~1400-wide: 2+3 cells are ~461px < Chromium/Goose clamp (~500).
    # Always use two-column stacks there. Six-up 3x2 (~460x440) keeps height and
    # is preferred over dual 3-stacks (~700x292) that crush Goose min-height.
    if n == 5 and densest_cw < prefer_cw and grid_w >= prefer_cw * 2 + gap:
        return place_column_stacks(n, grid_x0, grid_y0, grid_w, grid_h, gap)
    needs_stacks = (
        n >= 6
        and grid_w >= prefer_cw * 2 + gap
        and densest_cw < soft_cw
        and densest_rh < prefer_rh
        # Even-count 2-row grids that clear soft width keep uniform cells.
        and not (n % 2 == 0 and rows == 2 and densest_cw >= soft_cw)
    )
    if needs_stacks:
        return place_column_stacks(n, grid_x0, grid_y0, grid_w, grid_h, gap)

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


def assign_windows_to_cells(
    windows: list[dict[str, Any]],
    cells: list[tuple[int, int, int, int]],
) -> list[tuple[dict[str, Any], tuple[int, int, int, int]]]:
    """Match windows to cells: min-size-heavy apps get the tallest+widest cells first."""
    if not windows or not cells:
        return []
    # Height first (Goose clamps ~400), then width (~480), then area.
    cell_order = sorted(
        range(len(cells)),
        key=lambda i: (cells[i][3], cells[i][2], cells[i][2] * cells[i][3]),
        reverse=True,
    )
    win_order = sorted(
        range(len(windows)),
        key=lambda i: (
            2 if wants_tall(windows[i]) else 0,
            2 if wants_wide(windows[i]) else 0,
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
        cells = place_grid(n, grid_x0, grid_y0, grid_w, grid_h, gap_i)
        cols, rows = choose_grid(n, grid_w, grid_h, gap_i)
        for c, (cx, cy, cell_w, cell_h) in assign_windows_to_cells(others, cells):
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
                    "meta": {
                        "gap": gap_i,
                        "outer": outer_i,
                        "cols": cols,
                        "rows": rows,
                        "logical": list(logical_monitor_box(mon)[2:4]),
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
