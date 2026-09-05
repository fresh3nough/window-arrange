# window-arrange

Omarchy / Hyprland helper that tidies every window on the **active workspace**.

- **Not fullscreen** — exits maximized/fullscreen first so the Omarchy top bar (clock, shortcuts) and desktop wallpaper stay visible
- **12px padding** on all sides (outside the reserved bar)
- **Even grid** for normal apps
- **scrcpy / Pixel** stays a compact portrait strip on the right
- Works with Hyprland 0.56+ Lua dispatchers (`hl.dsp.window.*`)

## Install (other Omarchy machine)

```bash
cd ~/github
git clone git@github.com:fresh3nough/window-arrange.git
# or: gh repo clone fresh3nough/window-arrange
cd window-arrange
bash install.sh
```

`install.sh` puts the binary on `~/.local/bin` and binds **Super+Alt+A**.

Manual:

```bash
install -m 0755 window-arrange ~/.local/bin/window-arrange
# optional hotkey in ~/.config/hypr/bindings.lua:
#   o.bind("SUPER + ALT + A", "Arrange windows", "window-arrange")
```

## Usage

```bash
window-arrange              # apply layout
window-arrange --dry-run    # print plan only
```

| Action | Shortcut | On a Mac host |
|--------|----------|----------------|
| Arrange windows | **Super+Alt+A** | **⌘⌥A** |

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `WINDOW_ARRANGE_GAP` | `8` | Gap between grid cells |
| `WINDOW_ARRANGE_OUTER` | `12` | Padding on top/bottom/left/right (outside reserved bars) |
| `WINDOW_ARRANGE_PHONE_W` | `360` | scrcpy strip width |
| `WINDOW_ARRANGE_PHONE_H` | `800` | scrcpy strip height |

Example:

```bash
WINDOW_ARRANGE_OUTER=16 WINDOW_ARRANGE_GAP=10 window-arrange
```

## Requirements

- [Omarchy](https://omarchy.org) (or Hyprland with `hyprctl`)
- `python3`
- `hyprctl` on `PATH`
- Optional: `omarchy-notification-send` / `notify-send`

## License

Private — for personal use.
