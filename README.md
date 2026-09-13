# window-arrange

Omarchy / Hyprland helper that tidies every window on the **active workspace**.

- **Not fullscreen** — exits maximized/fullscreen first so the Omarchy top bar (clock, shortcuts) and desktop wallpaper stay visible
- **Logical geometry** — plans in Hyprland logical pixels (`physical / scale`) so HiDPI panels and scale-1.0 screens share the same density
- **Adaptive padding** — roomier gap/outer (~12/16 at reference density); override with env vars
- **Even non-overlapping grid** — integer-split cells with true gutters; prefers browser-safe widths so min-size clamps cannot stack windows
- **Toolkit-aware packing** — Chromium (~500w) / Goose (~480×400) force stacks + floor-aware heights; 1Password (~784w) gets a dedicated right strip so 5–7 apps never cut off; settle pins oversize clamps inside work bounds
- **scrcpy / Pixel** stays a compact landscape strip on the right
- **Interactive edit** — drag windows to swap cells; drag edges/corners to resize and push neighbors (i3-style gutters stay put)
- **Auto on open** — every new mapped app window triggers a debounced arrange (`hl.on("window.open")`)
- Works with Hyprland 0.56+ Lua dispatchers (`hl.dsp.window.*`)

## Install (other Omarchy machine)

```bash
cd ~/github
git clone git@github.com:fresh3nough/window-arrange.git
# or: gh repo clone fresh3nough/window-arrange
cd window-arrange
bash install.sh
```

`install.sh` puts the binary and modules on `~/.local/bin` (and a copy under `~/.local/share/window-arrange`), rebinds **Super+J** (replaces dwindle togglesplit), binds **Super+B** for the interactive editor, binds **Super+Alt+A**, installs **`~/.config/hypr/window-arrange-hook.lua`** so every new app window auto-arranges, and hooks **`window-arrange --on-start`** into `~/.config/hypr/autostart.lua` so the grid also runs after autoload apps map.

Why Super+J: default Omarchy `togglesplit` only flips already-tiled dwindle leaves. Floated / popped / maximized windows ignore it and keep stacking — the usual ultrawide / Surface Book mess. Arrange always re-packs the free work area as a responsive grid instead.

Why Super+B for edit: free on stock Omarchy (browser is **Super+Shift+B**).

Manual:

```bash
install -m 0755 window-arrange ~/.local/bin/window-arrange
for m in layout.py geometry.py apply.py editor.py; do
  install -m 0644 "$m" ~/.local/bin/"$m"
  install -m 0644 "$m" ~/.local/share/window-arrange/"$m"
done
# optional hotkeys in ~/.config/hypr/bindings.lua:
#   hl.unbind("SUPER + J")
#   o.bind("SUPER + J", "Arrange windows (grid)", "window-arrange")
#   o.bind("SUPER + ALT + A", "Arrange windows", "window-arrange")
#   o.bind("SUPER + B", "Arrange windows (edit)", "window-arrange --edit")
# optional boot + live open hooks in ~/.config/hypr/autostart.lua:
#   require("hypr.window-arrange-hook")
#   o.exec_on_start("window-arrange --on-start")
# and copy window-arrange-hook.lua → ~/.config/hypr/window-arrange-hook.lua
```

## Performance

Hyprland **0.56+** (Omarchy) is Lua-first: legacy `hyprctl dispatch resizewindowpixel …`
is rejected, so older "batch of pixel dispatches" paths reported OK and moved
nothing. Current path:

1. One `layout.py` plan (logical px) — 2-col stacks when cells would be < toolkit min  
2. One `hyprctl clients -j` snapshot (fullscreen / pinned / tags)  
3. One `hyprctl eval` via `apply.py` (soften `min_size`, strip Omarchy float tags,
   exit fs, `float { action = "enable" }`, resize+move, settle pass)  

Typical wall time on 5–6 windows: **~5–20ms** eval (+~50ms settle). No focus cycling.

## Usage

```bash
window-arrange              # apply layout
window-arrange --dry-run    # print plan only
window-arrange --edit       # interactive drag-swap + neighbor resize overlay
window-arrange --on-start   # wait for autostart apps, then arrange once
```

| Action | Shortcut | On a Mac host |
|--------|----------|----------------|
| Arrange windows (grid) | **Super+J** | **Super+Option+J** |
| Arrange windows (alias) | **Super+Alt+A** | **Super+Option+A** |
| Edit layout (drag/resize live) | **Super+B** | **Super+Option+B** |
| Arrange after boot apps | autostart | `window-arrange --on-start` |
| Arrange on every new window | auto (`window.open`) | `hypr.window-arrange-hook` |

### Interactive editor

`window-arrange --edit` opens a **transparent** full-monitor overlay (GTK4 layer-shell) so the real desktop stays visible.

**Single-instance:** only one editor may run. A second Super+B / `--edit` replaces the previous overlay (SIGTERM → SIGKILL) under an exclusive flock. Gtk.Application also uses `ALLOW_REPLACEMENT|REPLACE`. This prevents stacked overlays from pegging CPU (~90% each). Set `WINDOW_ARRANGE_EDITOR_REPLACE=0` to refuse a second launch instead.

- **Drag a window's body** onto another window → **swap** cells in the same row, or **flip whole rows** when dropping across bands (e.g. 2-up top ↔ full-width bottom)
- **Drag an edge or corner** → grow/shrink that side; **abutting neighbors move with it** and real windows **resize live under the outline** (no white wash, no wait-for-Apply)
- Free-edge growth stops at non-neighbor obstacles and the work-area bound — cells never overlap
- **Enter** / **Done** keeps the live layout and closes
- **Esc** / **Cancel** / right-click **restores** the snapshot from when the editor opened
- **R** re-reads live window geometries

Requires `gtk4` + `gtk4-layer-shell` + `python-gobject`. On Arch/Omarchy: `sudo pacman -S gtk4-layer-shell`.

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `WINDOW_ARRANGE_GAP` | adaptive (~12) | Gap between grid cells |
| `WINDOW_ARRANGE_OUTER` | adaptive (~16) | Padding on top/bottom/left/right (outside reserved bars) |
| `WINDOW_ARRANGE_PHONE_W` | adaptive | scrcpy strip width (logical px) |
| `WINDOW_ARRANGE_PHONE_H` | adaptive | scrcpy strip height (logical px) |
| `WINDOW_ARRANGE_ADAPT` | `1` | Set `0` to lock 12/16/360/800 defaults (still logical geometry) |
| `WINDOW_ARRANGE_NOTIFY` | `1` (`0` under `--on-start` / auto-open) | Set `0` to silence toast |
| `WINDOW_ARRANGE_ON_START_WAIT` | `8` | Seconds `--on-start` polls for mapped windows |
| `WINDOW_ARRANGE_ON_START_MIN` | `2` | Min mapped windows before arranging |
| `WINDOW_ARRANGE_ON_START_STABLE` | `2` | Consecutive identical snapshots required |
| `WINDOW_ARRANGE_OPEN_DEBOUNCE_MS` | `80` | Coalesce burst `window.open` events before arrange |

Example:

```bash
WINDOW_ARRANGE_OUTER=16 WINDOW_ARRANGE_GAP=10 window-arrange
WINDOW_ARRANGE_ADAPT=0 window-arrange --dry-run
WINDOW_ARRANGE_ON_START_WAIT=12 window-arrange --on-start
```

## Tests

```bash
python3 -m unittest tests.test_layout tests.test_geometry -v
```

Coverage includes HiDPI fractional scale (physical 2256x1504 @ 1.5667 → logical 1440x960) and scale-1.0 Mac-like canvases so placements stay on-screen on both.

## Requirements

- [Omarchy](https://omarchy.org) (or Hyprland with `hyprctl`)
- `python3`
- `hyprctl` on `PATH`
- Interactive editor: `gtk4`, `gtk4-layer-shell`, `python-gobject`
- Optional: `omarchy-notification-send` / `notify-send`

## License

Private — for personal use.
