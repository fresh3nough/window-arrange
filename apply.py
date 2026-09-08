#!/usr/bin/env python3
"""Apply a list of window geometries through one hyprctl eval (Hyprland 0.56+).

Shared by the one-shot arranger and the interactive editor so both paths float,
strip Omarchy tags, exit fullscreen, and lock cells the same way.

Toolkit min-size clamps (Chromium ~500w, Goose ~480x400) silently refuse a
smaller resize; Hyprland's resize is center-anchored, so the top-left drifts
into the neighbor. We:

1. Drop a low min_size window rule for each class before geometry.
2. resize → move → resize → move (rules can reflow after float).
3. Final settle pass: move every window to its planned top-left again after
   the client has applied its configure, so clamps cannot leave overlaps.
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
        "local function apply(w, x, y, rw, rh, need_fs, need_float, need_pin)",
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
        "  if need_float then",
        "    hl.dispatch(hl.dsp.window.float({ window = w }))",
        "  end",
        "  -- resize is center-anchored: always finish with move so a clamped",
        "  -- size cannot leave the top-left inside a neighbor cell.",
        "  hl.dispatch(hl.dsp.window.resize({ window = w, x = rw, y = rh }))",
        "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))",
        "  hl.dispatch(hl.dsp.window.resize({ window = w, x = rw, y = rh }))",
        "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))",
        "end",
        "local function settle(w, x, y, rw, rh)",
        "  hl.dispatch(hl.dsp.window.resize({ window = w, x = rw, y = rh }))",
        "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))",
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
        floating = bool(live.get("floating", p.get("floating", True)))
        pinned = bool(live.get("pinned", p.get("pinned")))
        tags = live.get("tags") or []
        if any(str(t).rstrip("*") == "pop" for t in tags):
            pinned = True
        need_float = not floating
        lines.append(
            "apply(%s, %d, %d, %d, %d, %s, %s, %s)"
            % (
                lua_str(f"address:{addr}"),
                int(p["x"]),
                int(p["y"]),
                int(p["w"]),
                int(p["h"]),
                "true" if fs_i else "false",
                "true" if need_float else "false",
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
        # Brief yield so Wayland clients finish configure, then re-move any
        # window whose top-left drifted (clamped size + center resize).
        time.sleep(0.05)
        try:
            live_list = load_json(["hyprctl", "clients", "-j"])
            live = {c["address"]: c for c in live_list if c.get("address")}
        except Exception:
            live = {}
        fixes: list[str] = []
        for p in plan:
            addr = p["address"]
            c = live.get(addr)
            if not c:
                continue
            at = c.get("at") or [0, 0]
            size = c.get("size") or [0, 0]
            px, py, pw, ph = int(p["x"]), int(p["y"]), int(p["w"]), int(p["h"])
            # If top-left drifted or size overshot into neighbor territory,
            # pin top-left again at the planned origin. Size may still be the
            # toolkit min (larger than cell) — move-last keeps the overflow on
            # the bottom/right edge of the cell instead of spanning neighbors.
            if at[0] != px or at[1] != py or size[0] != pw or size[1] != ph:
                fixes.append(
                    "settle(%s, %d, %d, %d, %d)"
                    % (lua_str(f"address:{addr}"), px, py, pw, ph)
                )
        if fixes:
            settle_lua = (
                "local function settle(w, x, y, rw, rh)\n"
                "  hl.dispatch(hl.dsp.window.resize({ window = w, x = rw, y = rh }))\n"
                "  hl.dispatch(hl.dsp.window.move({ window = w, x = x, y = y }))\n"
                "end\n"
                + "\n".join(fixes)
                + f'\nprint("settled={len(fixes)}")'
            )
            subprocess.run(
                ["hyprctl", "eval", settle_lua],
                text=True,
                capture_output=True,
            )
            result["settled"] = len(fixes)
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
