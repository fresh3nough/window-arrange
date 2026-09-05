#!/usr/bin/env bash
# Install window-arrange on Omarchy / Hyprland.
#   curl -fsSL … | bash   OR   bash install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
mkdir -p "$BIN_DIR"
install -m 0755 "${ROOT}/window-arrange" "${BIN_DIR}/window-arrange"

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
  skip==0 { print }
' "$BIND_FILE" > "$tmp"
cat >> "$tmp" <<'LUA'

-- window-arrange:begin
-- Auto-arrange windows: padded grid + scrcpy phone strip. Bar & wallpaper stay visible.
-- CLI: window-arrange   |   dry-run: window-arrange --dry-run
o.bind("SUPER + ALT + A", "Arrange windows", "window-arrange")
-- window-arrange:end
LUA
mv "$tmp" "$BIND_FILE"

if command -v hyprctl >/dev/null 2>&1 && [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then
  hyprctl reload config-only >/dev/null 2>&1 \
    || hyprctl reload >/dev/null 2>&1 \
    || true
fi

echo "Installed: ${BIN_DIR}/window-arrange"
echo "Hotkey:    Super+Alt+A  (Mac host: ⌘⌥A)"
echo "Run:       window-arrange"
echo "Dry-run:   window-arrange --dry-run"
