#!/usr/bin/env bash
# Install window-arrange on Omarchy / Hyprland.
#   curl -fsSL … | bash   OR   bash install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SHARE_DIR="${HOME}/.local/share/window-arrange"
mkdir -p "$BIN_DIR" "$SHARE_DIR"

# Soft-check interactive editor deps (arrange itself only needs python3 + hyprctl).
if ! python3 -c 'import gi; gi.require_version("Gtk4LayerShell","1.0"); gi.require_version("Gtk","4.0"); from gi.repository import Gtk4LayerShell' 2>/dev/null; then
  echo "note: gtk4-layer-shell GI missing — editor needs: sudo pacman -S gtk4-layer-shell" >&2
fi
if ! python3 -c 'import cairo' 2>/dev/null; then
  echo "note: python-cairo missing — editor needs: sudo pacman -S python-cairo" >&2
fi

# Python modules must sit next to the launcher (or under share/) for planning + editor.
for mod in layout.py geometry.py apply.py editor.py; do
  install -m 0644 "${ROOT}/${mod}" "${SHARE_DIR}/${mod}"
  install -m 0644 "${ROOT}/${mod}" "${BIN_DIR}/${mod}"
done
install -m 0755 "${ROOT}/window-arrange" "${BIN_DIR}/window-arrange"

# Lua hook: hl.on("window.open") → debounced window-arrange (Hyprland 0.56+).
HOOK_SRC="${ROOT}/window-arrange-hook.lua"
HOOK_DST="${HOME}/.config/hypr/window-arrange-hook.lua"
mkdir -p "$(dirname "$HOOK_DST")"
install -m 0644 "$HOOK_SRC" "$HOOK_DST"
install -m 0644 "$HOOK_SRC" "${SHARE_DIR}/window-arrange-hook.lua"

# Ensure ~/.local/bin is on PATH for this shell and future logins
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) export PATH="${BIN_DIR}:$PATH" ;;
esac
if [ -f "${HOME}/.bashrc" ] && ! grep -q '\.local/bin' "${HOME}/.bashrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "${HOME}/.bashrc"
fi

BIND_FILE="${HOME}/.config/hypr/bindings.lua"
mkdir -p "$(dirname "$BIND_FILE")"
if [ ! -f "$BIND_FILE" ]; then
  cat > "$BIND_FILE" <<'LUA'
-- Personal Hyprland keybinding overrides
LUA
fi

tmp="$(mktemp)"
awk '
  BEGIN { skip=0 }
  /^-- window-arrange:begin$/ { skip=1; next }
  /^-- window-arrange:end$/ { skip=0; next }
  /^-- omarchy-rice:window-arrange-begin$/ { skip=1; next }
  /^-- omarchy-rice:window-arrange-end$/ { skip=0; next }
  skip==0 { print }
' "$BIND_FILE" > "$tmp"
cat >> "$tmp" <<'LUA'

-- window-arrange:begin
-- Responsive padded grid + scrcpy phone strip. Bar & wallpaper stay visible.
-- Geometry is planned in Hyprland logical pixels (physical / scale) for HiDPI + scale-1.
-- Super+J default is dwindle togglesplit — useless once windows are floating,
-- maximized, or popped (the stacked mess on ultrawide / Surface Book). Rebind it.
-- Super+B is free on stock Omarchy (browser is Super+Shift+B).
-- CLI: window-arrange   |   dry-run: window-arrange --dry-run
-- Edit: window-arrange --edit  (live drag-swap + edge/corner neighbor resize)
-- Boot: window-arrange --on-start (waits for autostart apps, then arranges once)
-- Auto: every new mapped window → debounced arrange (see window-arrange-hook.lua)
hl.unbind("SUPER + J")
o.bind("SUPER + J", "Arrange windows (grid)", "window-arrange")
o.bind("SUPER + ALT + A", "Arrange windows", "window-arrange")
o.bind("SUPER + B", "Arrange windows (edit)", "window-arrange --edit")
-- window-arrange:end
LUA
mv "$tmp" "$BIND_FILE"

# After Hyprland autostart apps map, run arrange once (quiet toast).
# Also require the live window.open hook so every new app window rearranges.
AUTOSTART_FILE="${HOME}/.config/hypr/autostart.lua"
mkdir -p "$(dirname "$AUTOSTART_FILE")"
if [ ! -f "$AUTOSTART_FILE" ]; then
  cat > "$AUTOSTART_FILE" <<'LUA'
-- Extra autostart processes.
LUA
fi
tmp="$(mktemp)"
awk '
  BEGIN { skip=0 }
  /^-- window-arrange:begin$/ { skip=1; next }
  /^-- window-arrange:end$/ { skip=0; next }
  /^-- omarchy-rice:window-arrange-begin$/ { skip=1; next }
  /^-- omarchy-rice:window-arrange-end$/ { skip=0; next }
  skip==0 { print }
' "$AUTOSTART_FILE" > "$tmp"
cat >> "$tmp" <<'LUA'

-- window-arrange:begin
-- Live: every new mapped app window → debounced arrange (quiet toast).
-- Boot: after autoload apps start, wait for a stable mapped set then arrange once.
-- WINDOW_ARRANGE_NOTIFY defaults to 0 on auto/on-start paths.
require("hypr.window-arrange-hook")
o.exec_on_start("window-arrange --on-start")
-- window-arrange:end
LUA
mv "$tmp" "$AUTOSTART_FILE"

if command -v hyprctl >/dev/null 2>&1 && [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then
  # Omarchy/Hyprland 0.56 rejects legacy keyword reload; eval reload is fine.
  hyprctl reload config-only >/dev/null 2>&1 \
    || hyprctl reload >/dev/null 2>&1 \
    || true
fi

echo "Installed: ${BIN_DIR}/window-arrange"
echo "Modules:   ${SHARE_DIR}/{layout,geometry,apply,editor}.py"
echo "Hook:      ${HOOK_DST}  (hl.on window.open → arrange)"
echo "Hotkeys:   Super+J / Super+Alt+A arrange · Super+B edit"
echo "Autostart: window-arrange --on-start  (via ~/.config/hypr/autostart.lua)"
echo "Auto:      every new app window rearranges instantly"
echo "Run:       window-arrange"
echo "Edit:      window-arrange --edit"
echo "Dry-run:   window-arrange --dry-run"
echo "Boot wait: window-arrange --on-start"
echo "Tests:     python3 -m unittest tests.test_layout tests.test_geometry -v"
