# window-arrange

Omarchy / Hyprland helper that tidies every window on the **active workspace**.

- **Not fullscreen** — exits maximized/fullscreen first so the Omarchy top bar (clock, shortcuts) and desktop wallpaper stay visible
- **Logical geometry** — plans in Hyprland logical pixels (`physical / scale`) so HiDPI panels and scale-1.0 screens share the same density
- **Adaptive padding** — roomier gap/outer (~12/16 at reference density); override with env vars
- **Even non-overlapping grid** — integer-split cells with true gutters; prefers browser-safe widths so min-size clamps cannot stack windows
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

`install.sh` puts the binary and `layout.py` on `~/.local/bin` (and a copy under `~/.local/share/window-arrange`) and binds **Super+Alt+A**.

Manual:

```bash
install -m 0755 window-arrange ~/.local/bin/window-arrange
install -m 0644 layout.py ~/.local/bin/layout.py
install -m 0644 layout.py ~/.local/share/window-arrange/layout.py
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
| Arrange windows | **Super+Alt+A** | **Cmd+Opt+A** |

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `WINDOW_ARRANGE_GAP` | adaptive (~12) | Gap between grid cells |
| `WINDOW_ARRANGE_OUTER` | adaptive (~16) | Padding on top/bottom/left/right (outside reserved bars) |
| `WINDOW_ARRANGE_PHONE_W` | adaptive | scrcpy strip width (logical px) |
| `WINDOW_ARRANGE_PHONE_H` | adaptive | scrcpy strip height (logical px) |
| `WINDOW_ARRANGE_ADAPT` | `1` | Set `0` to lock 12/16/360/800 defaults (still logical geometry) |
Example:

```bash
WINDOW_ARRANGE_OUTER=16 WINDOW_ARRANGE_GAP=10 window-arrange
WINDOW_ARRANGE_ADAPT=0 window-arrange --dry-run
```

## Tests

```bash
python3 -m unittest tests.test_layout -v
```

Coverage includes HiDPI fractional scale (physical 2256x1504 @ 1.5667 → logical 1440x960) and scale-1.0 Mac-like canvases so placements stay on-screen on both.

## Requirements

- [Omarchy](https://omarchy.org) (or Hyprland with `hyprctl`)
- `python3`
- `hyprctl` on `PATH`
- Optional: `omarchy-notification-send` / `notify-send`

## License

Private — for personal use.
