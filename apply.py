#!/usr/bin/env python3
"""Apply a list of window geometries through one hyprctl eval (Hyprland 0.56+).

Shared by the one-shot arranger and the interactive editor so both paths float,
strip Omarchy tags, exit fullscreen, and lock cells the same way.

Toolkit min-size clamps (Chromium ~500w, Goose ~480x400, 1Password ~784w)
silently refuse a smaller resize; Hyprland's resize is center-anchored, so the
top-left drifts into the neighbor. Omarchy also tags 1Password / Bitwarden /
dialogs with `floating-window`, which forces `float + center + size {875, 600}`
via `/usr/share/omarchy/default/hypr/apps/system.lua`. Bare
`hl.dsp.window.float({})` **toggles** float, so a second arrange (or a stale
floating bit) tiles the window and the next resize balloons to the monitor.

We:

1. Drop a low min_size window rule for each class before geometry.
2. Strip Omarchy float/pop tags, then `float { action = "enable" }` (never
   toggle), then strip tags again (class rules may re-tag 1Password).
3. resize → move → resize → move (rules can reflow after float).
4. Final settle pass: enable-float + strip + resize/move again after the
   client has applied its configure, so clamps / tag size rules cannot leave
   overlaps. Move-only pins for oversize clamps keep the live frame inside the
   planned work-area bounds (never cut off the right/bottom edge).
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any


def load_json(cmd: list[str]) -> Any:
    return json.loads(subprocess.check_output(cmd, text=True))


def lua_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def fs_mode(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 1 if value else 0


def work_bounds_from_plan(plan: list[dict[str, Any]]) -> tuple[int, int, int, int]:
    """Union of planned cells expanded by any per-item bound_* meta.

    Used when a client clamps larger than its cell so the pin stays on-screen
    instead of locking the planned top-left and spilling past the monitor.
    """
    if not plan:
        return 0, 0, 0, 0
    xs: list[int] = []
    ys: list[int] = []
    x1s: list[int] = []
    y1s: list[int] = []
    for p in plan:
        meta = p.get("meta") or {}
        if "bound_x0" in meta and "bound_y0" in meta and "bound_x1" in meta and "bound_y1" in meta:
            xs.append(int(meta["bound_x0"]))
            ys.append(int(meta["bound_y0"]))
            x1s.append(int(meta["bound_x1"]))
            y1s.append(int(meta["bound_y1"]))
        xs.append(int(p["x"]))
        ys.append(int(p["y"]))
        x1s.append(int(p["x"]) + int(p["w"]))
        y1s.append(int(p["y"]) + int(p["h"]))
    return min(xs), min(ys), max(x1s), max(y1s)


def pin_xy_for_live_size(
    px: int,
    py: int,
    live_w: int,
    live_h: int,
    bounds: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Prefer planned top-left; shift up/left so live size stays inside bounds."""
    bx0, by0, bx1, by1 = bounds
    x, y = int(px), int(py)
    lw = max(0, int(live_w))
    lh = max(0, int(live_h))
    if lw > 0 and x + lw > bx1:
        x = max(bx0, bx1 - lw)
    if lh > 0 and y + lh > by1:
        y = max(by0, by1 - lh)
    if x < bx0:
        x = bx0
    if y < by0:
        y = by0
    return x, y


def _plan_column_groups(
    plan: list[dict[str, Any]], tol: int = 24
) -> list[list[dict[str, Any]]]:
    """Group planned cells into vertical stacks (same x band, sorted top→bottom)."""
    if not plan:
        return []
    items = sorted(plan, key=lambda p: (int(p["x"]), int(p["y"])))
    groups: list[list[dict[str, Any]]] = []
    for p in items:
        placed = False
        for g in groups:
            gx = int(g[0]["x"])
            gw = int(g[0]["w"])
            # Same column if x aligns and widths are similar (stack strip).
            if abs(int(p["x"]) - gx) <= tol and abs(int(p["w"]) - gw) <= max(tol, gw // 5):
                g.append(p)
                placed = True
                break
        if not placed:
            groups.append([p])
    for g in groups:
        g.sort(key=lambda p: int(p["y"]))
    return groups


def reflow_columns_from_live(
    plan: list[dict[str, Any]],
    live: dict[str, dict[str, Any]],
    bounds: tuple[int, int, int, int],
    gap: int = 12,
) -> list[tuple[str, int, int, int, int]]:
    """Recompute column y-stacks from live sizes so neighbors do not overlap.

    Hard-tall live clamps (Nautilus ~380, Code ~312, Goose ~400) keep their
    height; flexible donors (terminals/browser/disks) shrink so the column
    still fits the planned span. Returns (address, x, y, w, h) settle targets.
    """
    try:
        from layout import split_axis_with_floors
    except Exception:
        return []

    out: list[tuple[str, int, int, int, int]] = []
    _bx0, _by0, bx1, _by1 = bounds

    for group in _plan_column_groups(plan):
        if len(group) <= 1:
            continue
        ordered = sorted(group, key=lambda p: int(p["y"]))
        top = min(int(p["y"]) for p in ordered)
        bottom = max(int(p["y"]) + int(p["h"]) for p in ordered)
        total_h = bottom - top
        if total_h <= 0:
            continue

        live_sizes: list[tuple[int, int]] = []
        hard_flags: list[bool] = []
        need_reflow = False
        boxes: list[tuple[int, int, int, int]] = []
        for p in ordered:
            c = live.get(p["address"]) or {}
            size = c.get("size") or [int(p["w"]), int(p["h"])]
            at = c.get("at") or [int(p["x"]), int(p["y"])]
            lw, lh = max(1, int(size[0])), max(1, int(size[1]))
            ph = int(p["h"])
            mh = int(p.get("min_h") or 0)
            # Hard if client clamped up or toolkit min is tall.
            hard = lh > ph + 2 or mh >= 280 or lh >= 280
            if lh > ph + 2:
                need_reflow = True
            hard_flags.append(hard)
            live_sizes.append((lw, lh))
            boxes.append((int(at[0]), int(at[1]), lw, lh))

        if not need_reflow:
            for i, a in enumerate(boxes):
                for b in boxes[i + 1 :]:
                    ox = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
                    oy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
                    if ox > 0 and oy > 0:
                        need_reflow = True
                        break
                if need_reflow:
                    break
        if not need_reflow:
            continue

        n = len(ordered)
        gaps_total = gap * max(0, n - 1)
        usable = max(n, total_h - gaps_total)

        # Floors: hard keeps toolkit/live min; flex collapses to 80 so the
        # column can absorb two tall clamps without mid-stack overlap.
        floors: list[int] = []
        for p, hard, (_lw, lh) in zip(ordered, hard_flags, live_sizes):
            mh = int(p.get("min_h") or 0)
            if hard:
                # Prefer toolkit min when live is the unclamped planned size.
                floors.append(max(mh if mh >= 280 else lh, min(lh, max(mh, 280)), 80))
            else:
                floors.append(80)
        hard_need = sum(f for f, h in zip(floors, hard_flags) if h)
        flex_n = sum(1 for h in hard_flags if not h)
        if hard_need + flex_n * 80 > usable and flex_n:
            # Still over after flex collapse: keep hard mins, flex stays 80;
            # split_axis_with_floors will scale hard floors as last resort.
            pass
        bands = split_axis_with_floors(total_h, floors, gap, floors)
        col_x = int(ordered[0]["x"])
        for p, (lw, _lh), (ry, rh) in zip(ordered, live_sizes, bands):
            place_w = max(lw, 1)
            place_h = max(int(rh), 1)
            x = col_x
            if x + place_w > bx1:
                x = max(bounds[0], bx1 - place_w)
            y = top + int(ry)
            out.append((p["address"], x, y, place_w, place_h))
    return out


def cascade_pin_column(
    group: list[dict[str, Any]],
    live: dict[str, dict[str, Any]],
    targets: dict[str, tuple[int, int, int, int]],
    bounds: tuple[int, int, int, int],
    gap: int = 12,
) -> list[tuple[str, int, int]]:
    """Top→bottom pin using target bands, falling back to live height.

    Uses the reflow target height when the client accepted it (live ≤ target+2).
    When the client refused shrink, uses live height and pushes neighbors down
    — lower flex windows may clip past the work bound; mid-stack overlap is
    avoided.
    """
    if len(group) <= 1:
        return []
    ordered = sorted(group, key=lambda q: int(q["y"]))
    top = min(int(p["y"]) for p in ordered)
    col_x = int(ordered[0]["x"])
    bx0, _by0, bx1, by1 = bounds
    y = top
    out: list[tuple[str, int, int]] = []
    for p in ordered:
        addr = p["address"]
        c = live.get(addr) or {}
        size = c.get("size") or [int(p["w"]), int(p["h"])]
        lw, lh = max(1, int(size[0])), max(1, int(size[1]))
        tx, ty, tw, th = targets.get(addr, (col_x, y, lw, lh))
        # Prefer target height if client roughly accepted it; else live.
        use_h = th if lh <= th + 2 else lh
        x = col_x
        if x + lw > bx1:
            x = max(bx0, bx1 - lw)
        if x < bx0:
            x = bx0
        # Keep on-screen top if possible without climbing into previous frame.
        py = y
        if py + use_h > by1 and py > top:
            # Do not shift up (would overlap previous); allow bottom clip.
            pass
        out.append((addr, x, py))
        y = py + use_h + gap
    return out


def build_apply_lua(
    plan: list[dict[str, Any]],
    clients: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Build a Lua chunk that places every planned window."""
    clients = clients or {}
    lines: list[str] = [
        # Soften toolkit clamps for the classes we are about to place. Hyprland
        # still cannot force a client below its shell min, but the rule keeps
        # floating-window size={875,600} from re-inflating after we strip tags.
        "local function soften(class, mw, mh)",
        "  if class == nil or class == '' then return end",
        "  pcall(function()",
        "    hl.window_rule({ match = { class = class }, min_size = { mw, mh } })",
        "  end)",
        "end",
        # Prepare a window for geometry: tags off, floated (enable, not toggle).
        # 1Password's class rule re-applies +floating-window when other class
        # rules fire, so strip both before and after ensure_float.
        "local function prepare(w, need_fs, need_pin)",
        "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window*' })) end)",
        "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window' })) end)",
        "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-pop' })) end)",
        "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-pop*' })) end)",
        "  if need_fs then",
        "    hl.dispatch(hl.dsp.window.fullscreen({ window = w, mode = false }))",
        "    hl.dispatch(hl.dsp.window.fullscreen({ window = w, mode = false }))",
        "  end",
        "  if need_pin then",
        "    pcall(function() hl.dispatch(hl.dsp.window.pin({ window = w })) end)",
        "  end",
        "  pcall(function() hl.dispatch(hl.dsp.window.float({ window = w, action = 'enable' })) end)",
        "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window*' })) end)",
        "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window' })) end)",
        "end",
        "local function place(w, x, y, rw, rh)",
        "  -- resize is center-anchored: always finish with move so a clamped",
        "  -- size cannot leave the top-left inside a neighbor cell.",
        "  hl.dispatch(hl.dsp.window.resize({ window = w, x = rw, y = rh }))",
        "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))",
        "  hl.dispatch(hl.dsp.window.resize({ window = w, x = rw, y = rh }))",
        "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))",
        "end",
        "local function apply(w, x, y, rw, rh, need_fs, need_pin)",
        "  prepare(w, need_fs, need_pin)",
        "  place(w, x, y, rw, rh)",
        "end",
        "local function settle(w, x, y, rw, rh)",
        # Re-strip + enable-float: Omarchy class rules can re-tag 1Password
        # after the first configure, which would snap size back to 875x600.
        "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window*' })) end)",
        "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window' })) end)",
        "  pcall(function() hl.dispatch(hl.dsp.window.float({ window = w, action = 'enable' })) end)",
        "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window*' })) end)",
        "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window' })) end)",
        "  place(w, x, y, rw, rh)",
        "end",
    ]

    # One soften rule per unique class.
    seen_cls: set[str] = set()
    for p in plan:
        cls = str(p.get("class") or "")
        if not cls or cls in seen_cls:
            continue
        seen_cls.add(cls)
        mw = int(p.get("min_w") or 100)
        mh = int(p.get("min_h") or 100)
        # Ask below observed clamp so the rule never raises a floor.
        lines.append(
            "soften(%s, %d, %d)" % (lua_str(cls), max(80, mw // 2), max(80, mh // 2))
        )

    for p in plan:
        addr = p["address"]
        live = clients.get(addr, {})
        fs_i = fs_mode(live.get("fullscreen", p.get("fullscreen") or 0))
        pinned = bool(live.get("pinned", p.get("pinned")))
        tags = live.get("tags") or []
        if any(str(t).rstrip("*") == "pop" for t in tags):
            pinned = True
        # Always prepare (enable-float + strip). Conditional bare float used to
        # toggle and tile 1Password when the floating bit was already true.
        lines.append(
            "apply(%s, %d, %d, %d, %d, %s, %s)"
            % (
                lua_str(f"address:{addr}"),
                int(p["x"]),
                int(p["y"]),
                int(p["w"]),
                int(p["h"]),
                "true" if fs_i else "false",
                "true" if pinned else "false",
            )
        )

    # Settle pass after every client has seen its configure.
    for p in plan:
        addr = p["address"]
        lines.append(
            "settle(%s, %d, %d, %d, %d)"
            % (
                lua_str(f"address:{addr}"),
                int(p["x"]),
                int(p["y"]),
                int(p["w"]),
                int(p["h"]),
            )
        )

    lines.append(f'print("arranged={len(plan)}")')
    return "\n".join(lines)


def apply_plan(
    plan: list[dict[str, Any]],
    *,
    refresh_clients: bool = True,
    settle_retry: bool = True,
) -> dict[str, Any]:
    """Apply geometries. Returns {count, ms, eval_rc, error?, overlaps?}."""
    if not plan:
        return {"count": 0, "ms": 0.0, "eval_rc": 0}

    clients: dict[str, dict[str, Any]] = {}
    if refresh_clients:
        try:
            clients = {
                c["address"]: c
                for c in load_json(["hyprctl", "clients", "-j"])
                if c.get("address")
            }
        except Exception as e:
            return {"count": 0, "ms": 0.0, "eval_rc": 1, "error": f"clients:{e}"}

    lua = build_apply_lua(plan, clients)
    t0 = time.monotonic()
    proc = subprocess.run(
        ["hyprctl", "eval", lua],
        text=True,
        capture_output=True,
    )
    dt = (time.monotonic() - t0) * 1000
    err = (proc.stderr or "").strip()
    out = (proc.stdout or "").strip()
    result: dict[str, Any] = {
        "count": len(plan),
        "ms": dt,
        "eval_rc": proc.returncode,
    }
    if proc.returncode != 0 and err:
        result["error"] = err[:300]
    elif "error:" in out.lower():
        result["error"] = out[:300]

    if settle_retry:
        # Yield so Wayland clients finish configure. A clamping resize in the
        # same hyprctl eval as move (1Password ~784w) leaves top-left drifted;
        # a later move-only eval pins it inside work bounds. Omarchy may also
        # re-tag floating-window.
        time.sleep(0.05)
        try:
            live_list = load_json(["hyprctl", "clients", "-j"])
            live = {c["address"]: c for c in live_list if c.get("address")}
        except Exception:
            live = {}

        bounds = work_bounds_from_plan(plan)
        place_fixes: list[str] = []
        # addr -> pin lua line (dedupe; last wins after re-read)
        move_pins: dict[str, str] = {}

        def queue_pin(addr: str, px: int, py: int, live_w: int, live_h: int) -> None:
            sx, sy = pin_xy_for_live_size(px, py, live_w, live_h, bounds)
            move_pins[addr] = (
                "pin(%s, %d, %d)" % (lua_str(f"address:{addr}"), sx, sy)
            )

        for p in plan:
            addr = p["address"]
            c = live.get(addr)
            if not c:
                continue
            at = c.get("at") or [0, 0]
            size = c.get("size") or [0, 0]
            px, py, pw, ph = int(p["x"]), int(p["y"]), int(p["w"]), int(p["h"])
            tags = c.get("tags") or []
            tagged_float = any(
                str(t).rstrip("*") == "floating-window" for t in tags
            )
            lw, lh = int(size[0]), int(size[1])
            # Desired pin (planned TL, shifted if live size would leave bounds).
            want_x, want_y = pin_xy_for_live_size(px, py, lw, lh, bounds)
            pos_bad = at[0] != want_x or at[1] != want_y
            size_small = lw < pw or lh < ph
            size_mismatch = lw != pw or lh != ph
            if not (pos_bad or size_mismatch or tagged_float):
                continue
            # Oversize windows: settle-shrink to the planned target whenever it
            # meets the toolkit min (plans now size ≥ min, so the resize is
            # accepted). Only fall back to move-only for true clamps that the
            # target itself cannot satisfy (target < toolkit min).
            min_w = int(p.get("min_w") or 0)
            min_h = int(p.get("min_h") or 0)
            target_ok = (pw >= min_w - 5) and (ph >= min_h - 5)
            if (lw > pw or lh > ph) and not tagged_float:
                if target_ok:
                    place_fixes.append(
                        "settle(%s, %d, %d, %d, %d)"
                        % (lua_str(f"address:{addr}"), px, py, pw, ph)
                    )
                else:
                    queue_pin(addr, px, py, lw, lh)
            elif size_small or tagged_float:
                place_fixes.append(
                    "settle(%s, %d, %d, %d, %d)"
                    % (lua_str(f"address:{addr}"), px, py, pw, ph)
                )
            elif pos_bad:
                queue_pin(addr, px, py, lw, lh)

        settled = 0
        if place_fixes:
            settle_lua = (
                "local function place(w, x, y, rw, rh)\n"
                "  hl.dispatch(hl.dsp.window.resize({ window = w, x = rw, y = rh }))\n"
                "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))\n"
                "  hl.dispatch(hl.dsp.window.resize({ window = w, x = rw, y = rh }))\n"
                "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))\n"
                "end\n"
                "local function settle(w, x, y, rw, rh)\n"
                "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window*' })) end)\n"
                "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window' })) end)\n"
                "  pcall(function() hl.dispatch(hl.dsp.window.float({ window = w, action = 'enable' })) end)\n"
                "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window*' })) end)\n"
                "  pcall(function() hl.dispatch(hl.dsp.window.tag({ window = w, tag = '-floating-window' })) end)\n"
                "  place(w, x, y, rw, rh)\n"
                "end\n"
                + "\n".join(place_fixes)
                + f'\nprint("settled={len(place_fixes)}")'
            )
            subprocess.run(
                ["hyprctl", "eval", settle_lua],
                text=True,
                capture_output=True,
            )
            settled += len(place_fixes)
            # Clamping resize in settle can drift again — re-read for pin pass.
            time.sleep(0.05)
            try:
                live = {
                    c["address"]: c
                    for c in load_json(["hyprctl", "clients", "-j"])
                    if c.get("address")
                }
            except Exception:
                pass
            for p in plan:
                addr = p["address"]
                c = live.get(addr)
                if not c:
                    continue
                at = c.get("at") or [0, 0]
                size = c.get("size") or [0, 0]
                px, py = int(p["x"]), int(p["y"])
                lw, lh = int(size[0]), int(size[1])
                want_x, want_y = pin_xy_for_live_size(px, py, lw, lh, bounds)
                if at[0] != want_x or at[1] != want_y:
                    queue_pin(addr, px, py, lw, lh)

        if move_pins:
            # Separate eval from any resize: move-only pins clamped clients
            # inside work bounds so 1Password cannot spill past the monitor.
            move_fixes = list(move_pins.values())
            pin_lua = (
                "local function pin(w, x, y)\n"
                "  pcall(function() hl.dispatch(hl.dsp.window.float({ window = w, action = 'enable' })) end)\n"
                "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))\n"
                "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))\n"
                "end\n"
                + "\n".join(move_fixes)
                + f'\nprint("pinned={len(move_fixes)}")'
            )
            subprocess.run(
                ["hyprctl", "eval", pin_lua],
                text=True,
                capture_output=True,
            )
            settled += len(move_fixes)

        # Column reflow: when a client still clamped taller than its cell
        # (Nautilus ~380h in a short leftover stack), rebuild that column from
        # live heights so neighbors no longer overlap. Move-only when live
        # size already matches the reflowed band; settle when we need a shrink.
        try:
            live = {
                c["address"]: c
                for c in load_json(["hyprctl", "clients", "-j"])
                if c.get("address")
            }
        except Exception:
            live = live if "live" in dir() else {}
        gap_i = 12
        if plan:
            meta0 = (plan[0].get("meta") or {})
            try:
                gap_i = int(meta0.get("gap") or 12)
            except Exception:
                gap_i = 12
        reflow = reflow_columns_from_live(plan, live, bounds, gap=gap_i)
        if reflow:
            place_lines: list[str] = []
            for addr, x, y, w, h in reflow:
                c = live.get(addr) or {}
                size = c.get("size") or [0, 0]
                lw, lh = int(size[0]), int(size[1])
                # Always settle when target differs; clamp may refuse shrink
                # but move still lands top-left on the cascade y.
                if lw != w or lh != h or True:
                    place_lines.append(
                        "settle(%s, %d, %d, %d, %d)"
                        % (lua_str(f"address:{addr}"), x, y, w, h)
                    )
            if place_lines:
                reflow_lua = (
                    "local function place(w, x, y, rw, rh)\n"
                    "  hl.dispatch(hl.dsp.window.resize({ window = w, x = rw, y = rh }))\n"
                    "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))\n"
                    "  hl.dispatch(hl.dsp.window.resize({ window = w, x = rw, y = rh }))\n"
                    "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))\n"
                    "end\n"
                    "local function settle(w, x, y, rw, rh)\n"
                    "  pcall(function() hl.dispatch(hl.dsp.window.float({ window = w, action = 'enable' })) end)\n"
                    "  place(w, x, y, rw, rh)\n"
                    "end\n"
                    + "\n".join(place_lines)
                    + f'\nprint("reflowed={len(place_lines)}")'
                )
                subprocess.run(
                    ["hyprctl", "eval", reflow_lua],
                    text=True,
                    capture_output=True,
                )
                settled += len(place_lines)
                time.sleep(0.05)
                try:
                    live = {
                        c["address"]: c
                        for c in load_json(["hyprctl", "clients", "-j"])
                        if c.get("address")
                    }
                except Exception:
                    pass

            # Cascade pin using reflow targets so accepted shrinks stay packed
            # and refused hard-talls push flex donors down (clip) not overlap.
            target_map = {addr: (x, y, w, h) for addr, x, y, w, h in reflow}
            pin_lines: list[str] = []
            for group in _plan_column_groups(plan):
                for addr, x, y in cascade_pin_column(
                    group, live, target_map, bounds, gap=gap_i
                ):
                    pin_lines.append(
                        "pin(%s, %d, %d)" % (lua_str(f"address:{addr}"), x, y)
                    )
            if pin_lines:
                by_addr: dict[str, str] = {}
                for line in pin_lines:
                    try:
                        key = line.split(",")[0]
                    except Exception:
                        key = line
                    by_addr[key] = line
                pins = list(by_addr.values())
                pin_lua = (
                    "local function pin(w, x, y)\n"
                    "  pcall(function() hl.dispatch(hl.dsp.window.float({ window = w, action = 'enable' })) end)\n"
                    "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))\n"
                    "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))\n"
                    "end\n"
                    + "\n".join(pins)
                    + f'\nprint("cascade_pinned={len(pins)}")'
                )
                subprocess.run(
                    ["hyprctl", "eval", pin_lua],
                    text=True,
                    capture_output=True,
                )
                settled += len(pins)
                result["reflowed"] = len(reflow)

        if settled:
            result["settled"] = settled
            result["ms"] = (time.monotonic() - t0) * 1000

    return result


def snapshot_workspace_windows() -> list[dict[str, Any]]:
    """Live mapped windows on the active workspace as editor-ready dicts."""
    active = load_json(["hyprctl", "activeworkspace", "-j"])
    clients = load_json(["hyprctl", "clients", "-j"])
    ws_id = active.get("id")
    wins: list[dict[str, Any]] = []
    for c in clients:
        if not c.get("mapped") or c.get("hidden"):
            continue
        ws = c.get("workspace") or {}
        if ws.get("id") != ws_id:
            continue
        if str(ws.get("name", "")).startswith("special"):
            continue
        addr = c.get("address")
        if not addr:
            continue
        at = c.get("at") or [0, 0]
        size = c.get("size") or [100, 100]
        wins.append(
            {
                "address": addr,
                "class": c.get("class") or "",
                "title": c.get("title") or "",
                "x": int(at[0]),
                "y": int(at[1]),
                "w": int(size[0]),
                "h": int(size[1]),
                "floating": bool(c.get("floating")),
                "fullscreen": c.get("fullscreen") not in (0, False, None),
                "pinned": bool(c.get("pinned")),
                "role": "phone"
                if (
                    (c.get("class") or "").lower() == "scrcpy"
                    or "scrcpy" in (c.get("class") or "").lower()
                    or (c.get("title") or "").lower() == "pixel"
                )
                else "grid",
            }
        )
    return wins


def focused_monitor() -> dict[str, Any]:
    mons = load_json(["hyprctl", "monitors", "-j"])
    return next((m for m in mons if m.get("focused")), mons[0])
