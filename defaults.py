#!/usr/bin/env python3
"""Persist last Super+B resize sizes as Hyprland launch defaults.

When the interactive editor commits a layout, each window class keeps the
size you left it at. Those sizes are written to:

  ~/.config/window-arrange/preferred-sizes.json   (source of truth)
  ~/.config/hypr/window-arrange-sizes.lua         (o.window size rules)

so the next launch of that app maps at the remembered size instead of the
toolkit / Omarchy default. Live rules are also pushed through `hyprctl eval`
so the change is active without a full session restart.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any

# Classes that are desktop chrome / always-on widgets — never pin their size.
SKIP_CLASSES = {
    "",
    "org.quickshell",
    "quickshell",
    "org.omarchy.screensaver",
    "xdg-desktop-portal-gtk",
}

# Title prefixes that mark transient / widget surfaces even under shared class.
SKIP_TITLE_PREFIXES = (
    "owl:",
    "window-arrange",
)

CONFIG_DIR_NAME = "window-arrange"
SIZES_JSON_NAME = "preferred-sizes.json"
SIZES_LUA_NAME = "window-arrange-sizes.lua"
LUA_BEGIN = "-- window-arrange:preferred-sizes-begin"
LUA_END = "-- window-arrange:preferred-sizes-end"


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, CONFIG_DIR_NAME)


def sizes_json_path() -> str:
    override = os.environ.get("WINDOW_ARRANGE_SIZES_JSON")
    if override:
        return override
    return os.path.join(config_dir(), SIZES_JSON_NAME)


def sizes_lua_path() -> str:
    override = os.environ.get("WINDOW_ARRANGE_SIZES_LUA")
    if override:
        return override
    hypr = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(hypr, "hypr", SIZES_LUA_NAME)


def hyprland_user_lua_path() -> str:
    hypr = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(hypr, "hypr", "hyprland.lua")


def _normalize_class(cls: str) -> str:
    return (cls or "").strip()


def should_persist(win: dict[str, Any]) -> bool:
    """True when this window is a normal app worth remembering size for."""
    cls = _normalize_class(str(win.get("class") or ""))
    if not cls or cls.lower() in {c.lower() for c in SKIP_CLASSES}:
        return False
    title = str(win.get("title") or "")
    low = title.lower()
    for prefix in SKIP_TITLE_PREFIXES:
        if low.startswith(prefix.lower()):
            return False
    try:
        w = int(win.get("w") or 0)
        h = int(win.get("h") or 0)
    except (TypeError, ValueError):
        return False
    if w < 80 or h < 80:
        return False
    return True


def load_preferred_sizes(path: str | None = None) -> dict[str, dict[str, int]]:
    """Return {class: {"w": int, "h": int}} from disk (empty on missing/bad)."""
    p = path or sizes_json_path()
    try:
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    entries = raw.get("sizes", raw) if isinstance(raw.get("sizes"), dict) else raw
    if not isinstance(entries, dict):
        return {}
    for cls, val in entries.items():
        key = _normalize_class(str(cls))
        if not key:
            continue
        if isinstance(val, dict):
            try:
                w = int(val.get("w") or val.get("width") or 0)
                h = int(val.get("h") or val.get("height") or 0)
            except (TypeError, ValueError):
                continue
        elif isinstance(val, (list, tuple)) and len(val) >= 2:
            try:
                w, h = int(val[0]), int(val[1])
            except (TypeError, ValueError):
                continue
        else:
            continue
        if w >= 80 and h >= 80:
            out[key] = {"w": w, "h": h}
    return out


def preferred_size_for(win: dict[str, Any], store: dict[str, dict[str, int]] | None = None) -> tuple[int, int] | None:
    """Lookup remembered (w, h) for a window class, or None."""
    cls = _normalize_class(str(win.get("class") or ""))
    if not cls:
        return None
    data = store if store is not None else load_preferred_sizes()
    hit = data.get(cls)
    if not hit:
        # Case-insensitive fallback.
        low = cls.lower()
        for k, v in data.items():
            if k.lower() == low:
                hit = v
                break
    if not hit:
        return None
    return int(hit["w"]), int(hit["h"])


def _atomic_write(path: str, text: str, mode: int = 0o644) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wa-sizes-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_preferred_sizes_json(
    sizes: dict[str, dict[str, int]], path: str | None = None
) -> str:
    """Write the preferred-size store. Returns path written."""
    p = path or sizes_json_path()
    # Stable key order for readable diffs.
    ordered = {
        k: {"w": int(v["w"]), "h": int(v["h"])}
        for k, v in sorted(sizes.items(), key=lambda kv: kv[0].lower())
    }
    payload = {
        "version": 1,
        "sizes": ordered,
    }
    _atomic_write(p, json.dumps(payload, indent=2, sort_keys=False) + "\n")
    return p


def _lua_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def render_sizes_lua(sizes: dict[str, dict[str, int]]) -> str:
    """Render a Hyprland user config snippet of o.window size rules.

    Size alone is not enough for Chromium/Brave: Omarchy's browser rules
    ``tile = true``, so a bare size rule is ignored until float. We set
    ``float = true`` + exact size so the first map lands at the Super+B
    size; window-arrange then places it in the grid without shrinking below
    the preferred floor.
    """
    lines = [
        "-- Auto-generated by window-arrange. Do not edit by hand;",
        "-- Super+B editor Done rewrites this file from preferred-sizes.json.",
        "-- Each class maps at its last Super+B resize size on launch.",
        LUA_BEGIN,
    ]
    for cls in sorted(sizes.keys(), key=str.lower):
        entry = sizes[cls]
        w, h = int(entry["w"]), int(entry["h"])
        esc = _lua_escape(cls)
        # float + size: tiled Chromium tag would otherwise discard size.
        lines.append(
            f'o.window({{ class = "{esc}" }}, '
            f"{{ float = true, size = {{ {w}, {h} }} }})"
        )
    lines.append(LUA_END)
    lines.append("")
    return "\n".join(lines)


def write_sizes_lua(
    sizes: dict[str, dict[str, int]], path: str | None = None
) -> str:
    """Write ~/.config/hypr/window-arrange-sizes.lua. Returns path."""
    p = path or sizes_lua_path()
    _atomic_write(p, render_sizes_lua(sizes))
    return p


def ensure_hyprland_requires_sizes(path: str | None = None) -> bool:
    """Make sure hyprland.lua requires the preferred-sizes module.

    Returns True when the file was modified.
    """
    p = path or hyprland_user_lua_path()
    require_line = 'require("hypr.window-arrange-sizes")'
    marker_begin = "-- window-arrange:preferred-sizes-require-begin"
    marker_end = "-- window-arrange:preferred-sizes-require-end"
    block = (
        f"\n{marker_begin}\n"
        f"-- Last Super+B resize sizes → default launch size per app class.\n"
        f"pcall(function()\n"
        f"  {require_line}\n"
        f"end)\n"
        f"{marker_end}\n"
    )

    try:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        # Create a minimal user hyprland.lua if somehow missing.
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        text = (
            "-- Learn how to configure Hyprland: https://wiki.hypr.land/Configuring/Start/\n"
            "dofile((os.getenv(\"OMARCHY_PATH\") or \"/usr/share/omarchy\") "
            '.. "/default/hypr/bootstrap.lua")\n'
            'require("default.hypr.omarchy")\n'
        )

    # Strip any previous managed block, then append fresh.
    pattern = re.compile(
        re.escape(marker_begin) + r".*?" + re.escape(marker_end) + r"\n?",
        re.DOTALL,
    )
    stripped = pattern.sub("", text)
    # Hand-added bare require outside our block: leave the file alone, but
    # still ensure the sizes module exists so the require doesn't fail loudly.
    if require_line in stripped and marker_begin not in stripped:
        if not os.path.isfile(sizes_lua_path()):
            write_sizes_lua(load_preferred_sizes())
        return False

    new_text = stripped.rstrip() + "\n" + block
    if new_text == text:
        return False
    _atomic_write(p, new_text)
    return True


def merge_sizes_from_windows(
    windows: list[dict[str, Any]],
    *,
    initial: list[dict[str, Any]] | None = None,
    only_changed: bool = True,
    existing: dict[str, dict[str, int]] | None = None,
) -> dict[str, dict[str, int]]:
    """Merge live window sizes into the preferred-size store.

    When ``only_changed`` and ``initial`` are set, only classes whose (w,h)
    differ from the editor-open snapshot are updated — pure swaps don't
    overwrite a remembered size with a grid cell.
    """
    store = dict(existing if existing is not None else load_preferred_sizes())

    initial_by_addr: dict[str, dict[str, Any]] = {}
    if initial:
        for w in initial:
            addr = w.get("address")
            if addr:
                initial_by_addr[str(addr)] = w

    # Last window wins per class (focused/top-most usually last in snapshot).
    for win in windows:
        if not should_persist(win):
            continue
        cls = _normalize_class(str(win.get("class") or ""))
        try:
            w = int(win["w"])
            h = int(win["h"])
        except (KeyError, TypeError, ValueError):
            continue

        if only_changed and initial_by_addr:
            addr = str(win.get("address") or "")
            before = initial_by_addr.get(addr)
            if before is not None:
                try:
                    bw, bh = int(before["w"]), int(before["h"])
                except (KeyError, TypeError, ValueError):
                    bw = bh = -1
                if bw == w and bh == h:
                    continue

        store[cls] = {"w": w, "h": h}
    return store


def apply_live_size_rules(sizes: dict[str, dict[str, int]]) -> dict[str, Any]:
    """Push size window rules into the running Hyprland via hyprctl eval."""
    if not sizes:
        return {"ok": True, "count": 0, "skipped": "empty"}
    if not _hyprctl_available():
        return {"ok": False, "error": "hyprctl unavailable", "count": 0}

    # Replace the previous global table of rules so re-Done doesn't stack.
    parts = [
        "if type(window_arrange_preferred_size_rules) == 'table' then",
        "  for _, r in ipairs(window_arrange_preferred_size_rules) do",
        "    pcall(function()",
        "      if r and r.set_enabled then r:set_enabled(false) end",
        "    end)",
        "  end",
        "end",
        "window_arrange_preferred_size_rules = {}",
    ]
    for cls, entry in sorted(sizes.items(), key=lambda kv: kv[0].lower()):
        w, h = int(entry["w"]), int(entry["h"])
        esc = _lua_escape(cls)
        # float + size so Chromium/Brave tile rules cannot discard the size
        # on first map (Omarchy tags chromium-based-browser with tile=true).
        parts.append(
            "pcall(function()\n"
            f'  local r = hl.window_rule({{ match = {{ class = "{esc}" }}, '
            f"float = true, size = {{ {w}, {h} }} }})\n"
            "  table.insert(window_arrange_preferred_size_rules, r)\n"
            "end)"
        )
    lua = "\n".join(parts)
    try:
        proc = subprocess.run(
            ["hyprctl", "eval", lua],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc), "count": 0}
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "count": len(sizes) if ok else 0,
        "eval_rc": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "error": None if ok else (proc.stderr or proc.stdout or "eval failed"),
    }


def _hyprctl_available() -> bool:
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        # Still allow hyprctl if the socket exists via default discovery.
        pass
    from shutil import which

    return which("hyprctl") is not None


def persist_resized_defaults(
    windows: list[dict[str, Any]],
    *,
    initial: list[dict[str, Any]] | None = None,
    only_changed: bool = True,
    apply_live: bool = True,
    ensure_require: bool = True,
) -> dict[str, Any]:
    """Save editor sizes as launch defaults. Returns a result summary."""
    merged = merge_sizes_from_windows(
        windows, initial=initial, only_changed=only_changed
    )
    json_path = save_preferred_sizes_json(merged)
    lua_path = write_sizes_lua(merged)
    require_changed = False
    if ensure_require:
        try:
            require_changed = ensure_hyprland_requires_sizes()
        except OSError as exc:
            return {
                "ok": False,
                "error": f"hyprland.lua update failed: {exc}",
                "json": json_path,
                "lua": lua_path,
                "count": len(merged),
            }
    live: dict[str, Any] = {"ok": True, "count": 0, "skipped": "disabled"}
    if apply_live:
        live = apply_live_size_rules(merged)
    # Optional config reload so the require'd file is picked up next map.
    reload_ok = None
    if require_changed and _hyprctl_available():
        try:
            proc = subprocess.run(
                ["hyprctl", "reload", "config-only"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode != 0:
                proc = subprocess.run(
                    ["hyprctl", "reload"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            reload_ok = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            reload_ok = False

    return {
        "ok": bool(live.get("ok", True)),
        "count": len(merged),
        "json": json_path,
        "lua": lua_path,
        "require_changed": require_changed,
        "reload_ok": reload_ok,
        "live": live,
        "sizes": merged,
        "error": live.get("error"),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: dump / rewrite rules from the JSON store."""
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage: defaults.py [show|write|apply]\n"
            "  show   print preferred sizes JSON path + contents\n"
            "  write  rewrite window-arrange-sizes.lua from JSON\n"
            "  apply  write lua + push live hyprctl rules"
        )
        return 0 if argv and argv[0] in ("-h", "--help") else 1
    cmd = argv[0]
    sizes = load_preferred_sizes()
    if cmd == "show":
        print(f"json={sizes_json_path()}")
        print(f"lua={sizes_lua_path()}")
        print(json.dumps(sizes, indent=2))
        return 0
    if cmd == "write":
        ensure_hyprland_requires_sizes()
        path = write_sizes_lua(sizes)
        print(f"wrote={path} count={len(sizes)}")
        return 0
    if cmd == "apply":
        result = persist_resized_defaults(
            [
                {"class": cls, "w": v["w"], "h": v["h"], "title": "", "address": f"x{i}"}
                for i, (cls, v) in enumerate(sizes.items())
            ],
            only_changed=False,
            initial=None,
        )
        # Re-merge would no-op with empty windows when sizes already loaded —
        # force write of the loaded store instead:
        save_preferred_sizes_json(sizes)
        write_sizes_lua(sizes)
        live = apply_live_size_rules(sizes)
        print(f"count={len(sizes)} live_ok={live.get('ok')} live={live.get('count')}")
        if live.get("error"):
            print(f"error={live['error']}", file=sys.stderr)
            return 1
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
