-- window-arrange gold focus outline
-- Persistent glowing gold border + outer glow on the focused window.
-- Installed into ~/.config/hypr/looknfeel.lua (after Omarchy defaults + theme)
-- so it wins over theme border colors and survives theme switches / reloads.

-- Multi-stop gold gradient (bright core → warm amber → soft champagne).
local active_border_color = {
  colors = {
    "rgba(fff6c8ff)",
    "rgba(ffd700ff)",
    "rgba(ffb347ff)",
    "rgba(ffe66dff)",
    "rgba(ffd700ff)",
  },
  angle = 45,
}

-- Muted bronze so unfocused windows stay quiet against the gold focus.
local inactive_border_color = "rgba(5a4a32aa)"

-- Soft outer halo that only paints on the focused client.
local active_glow_color = {
  colors = {
    "rgba(ffd700cc)",
    "rgba(ffb34799)",
    "rgba(ffe66d66)",
  },
  angle = 45,
}
local inactive_glow_color = "rgba(00000000)"

hl.config({
  general = {
    -- Slightly thicker rim so the gold reads clearly on HiDPI.
    border_size = 3,
    col = {
      active_border = active_border_color,
      inactive_border = inactive_border_color,
    },
  },

  decoration = {
    glow = {
      enabled = true,
      range = 22,
      render_power = 3,
      color = active_glow_color,
      color_inactive = inactive_glow_color,
    },
  },

  group = {
    col = {
      border_active = active_border_color,
      border_inactive = inactive_border_color,
    },
  },
})

-- Keep the gold gradient slowly drifting while a window stays focused.
-- borderangle / glowangle loop = continuous spin of the multi-stop gradient.
hl.animation({ leaf = "border", enabled = true, speed = 5.39, bezier = "easeOutQuint" })
hl.animation({ leaf = "borderangle", enabled = true, speed = 45, bezier = "linear", style = "loop" })
hl.animation({ leaf = "glowangle", enabled = true, speed = 55, bezier = "linear", style = "loop" })
