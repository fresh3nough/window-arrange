#!/usr/bin/env python3
"""Layout planner for window-arrange.

Hyprland reports monitor width/height in *physical* pixels and client
geometry in *logical* pixels (physical / scale). moveactive/resizeactive
also expect logical coordinates. This module always plans in logical space
so the same defaults work on scale 1.0 (many desktops) and fractional
scale (e.g. Framework / HiDPI Omarchy laptops ~1.57).
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


def adaptive_defaults(area_w: int, area_h: int) -> dict[str, int]:
    """Derive gap / outer / phone strip sizes from the logical work area.

    Keeps a Mac-like density on ~1500px-wide logical panels and tightens
    slightly on smaller logical screens so cells stay usable.
    """
    # Reference: ~1440x960 logical (2256x1504 @ 1.5667) and similar MacBooks.
    ref_w, ref_h = 1440, 900
    # Geometric mean scale vs reference; clamp so extremes stay sane.
    factor = math.sqrt((max(area_w, 1) * max(area_h, 1)) / (ref_w * ref_h))
    factor = max(0.75, min(1.35, factor))
    gap = max(4, int(round(8 * factor)))
    outer = max(6, int(round(12 * factor)))
    # Portrait phone strip: ~25% of width, height ~2.1x width, capped by area.
    phone_w = max(240, min(420, int(round(area_w * 0.25))))
    phone_h = max(480, min(area_h, int(round(phone_w * 2.1))))
    return {"gap": gap, "outer": outer, "phone_w": phone_w, "phone_h": phone_h}


def choose_grid(n: int, grid_w: int, grid_h: int, gap: int) -> tuple[int, int, float, float]:
    """Pick cols/rows and cell size for n windows inside grid_w x grid_h."""
    if n <= 0:
        return 0, 0, 0.0, 0.0
    # Minimum cell size scales gently with grid so tiny logical screens still tile.
    min_cw = max(120, min(180, grid_w // 4 if grid_w else 120))
    min_rh = max(100, min(140, grid_h // 3 if grid_h else 100))
    best: tuple[float, int, int, float, float] | None = None
    for cols in range(1, n + 1):
        rows = math.ceil(n / cols)
        cw = (grid_w - gap * (cols - 1)) / cols
        rh = (grid_h - gap * (rows - 1)) / rows
        if cw < min_cw or rh < min_rh:
            continue
        aspect = cw / max(rh, 1)
        empty = cols * rows - n
        score = abs(math.log(max(aspect, 0.05) / 1.55))
        score += 0.25 * empty
        score += 0.12 * abs(cols - math.ceil(math.sqrt(n)))
        if grid_w > grid_h * 1.4:
            score += 0.08 * rows
        if best is None or score < best[0]:
            best = (score, cols, rows, cw, rh)
    if best is None:
        cols, rows = n, 1
        cw = (grid_w - gap * (cols - 1)) / max(cols, 1)
        rh = float(grid_h)
        return cols, rows, cw, rh
    _, cols, rows, cw, rh = best
    return cols, rows, cw, rh


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
    probe_outer = 12 if outer is None else outer
    _x0, _y0, _x1, _y1, area_w, area_h = work_area(mon, probe_outer)
    derived = adaptive_defaults(area_w, area_h) if auto_adapt else {
        "gap": 8, "outer": 12, "phone_w": 360, "phone_h": 800,
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
        cols, rows, cw, rh = choose_grid(n, grid_w, grid_h, gap_i)
        cw_i, rh_i = int(cw), int(rh)
        for i, c in enumerate(others):
            r, col = i // cols, i % cols
            row_start = r * cols
            row_count = min(cols, n - row_start)
            if row_count < cols and row_count > 0:
                cell_w = int((grid_w - gap_i * (row_count - 1)) / row_count)
                cx = grid_x0 + col * (cell_w + gap_i)
            else:
                cell_w = cw_i
                cx = grid_x0 + col * (cw_i + gap_i)
            cy = grid_y0 + r * (rh_i + gap_i)
            cell_h = rh_i
            # Clamp inside padded work area (never under the bar / off-screen).
            cx = max(grid_x0, min(cx, x1 - right_w - 120))
            cy = max(grid_y0, min(cy, y1 - 120))
            if cx + cell_w > x1 - right_w:
                cell_w = max(120, (x1 - right_w) - cx)
            if cy + cell_h > y1:
                cell_h = max(120, y1 - cy)
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
        # meta is for debugging/tests; strip before apply path if present
        out = {k: v for k, v in item.items() if k != "meta"}
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
