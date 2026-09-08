-- window-arrange-hook.lua
-- Auto-arrange every mapped app window as soon as it opens on Omarchy/Hyprland.
-- Installed to ~/.config/hypr/window-arrange-hook.lua and required from autostart.
--
-- window.open fires after window rules are applied. A short oneshot debounce
-- coalesces burst opens (boot / multi-window launch) into a single arrange.
-- A second delayed pass catches clients that map late after the first pass
-- (e.g. Nautilus as a 7th window).
--
-- Do NOT listen for window.fullscreen: SUPER+ALT+F (Mac Super+Option+F) is
-- Omarchy "Full width" / maximize. A fullscreen listener re-ran arrange and
-- immediately undid the maximize (looked like the focused Chromium "closed").

local M = {}

local gen = 0
-- Primary debounce: coalesce burst opens.
local debounce_ms = tonumber(os.getenv("WINDOW_ARRANGE_OPEN_DEBOUNCE_MS") or "") or 120
if debounce_ms < 0 then
  debounce_ms = 0
end
if debounce_ms > 2000 then
  debounce_ms = 2000
end
-- Second pass: late map after open (Nautilus, Electron shells).
local settle_ms = tonumber(os.getenv("WINDOW_ARRANGE_OPEN_SETTLE_MS") or "") or 450
if settle_ms < 0 then
  settle_ms = 0
end
if settle_ms > 5000 then
  settle_ms = 5000
end

local function should_skip(w)
  if w == nil then
    return true
  end

  local ok_mapped, mapped = pcall(function()
    return w.mapped
  end)
  if ok_mapped and mapped == false then
    return true
  end

  local ok_hidden, hidden = pcall(function()
    return w.hidden
  end)
  if ok_hidden and hidden then
    return true
  end

  local cls = ""
  local title = ""
  pcall(function()
    cls = string.lower(tostring(w.class or ""))
  end)
  pcall(function()
    title = string.lower(tostring(w.title or ""))
  end)

  -- Interactive editor overlay (GTK layer-shell / app window).
  if title:find("window%-arrange", 1, false) or cls:find("window%-arrange", 1, false) then
    return true
  end

  -- Stay off special / scratchpad workspaces.
  local ok_ws, ws = pcall(function()
    return w.workspace
  end)
  if ok_ws and ws ~= nil then
    local name = ""
    pcall(function()
      name = tostring(ws.name or "")
    end)
    if name:sub(1, 7) == "special" then
      return true
    end
  end

  return false
end

local function run_arrange()
  -- Quiet toast on auto path; manual Super+J still notifies by default.
  -- PATH must include ~/.local/bin for hypr exec (set by install / user profile).
  hl.exec_cmd("env WINDOW_ARRANGE_NOTIFY=0 PATH=\"$HOME/.local/bin:$PATH\" window-arrange")
end

local function schedule_arrange()
  gen = gen + 1
  local my = gen
  if debounce_ms <= 0 then
    run_arrange()
  else
    hl.timer(function()
      if my ~= gen then
        return
      end
      run_arrange()
    end, { timeout = debounce_ms, type = "oneshot" })
  end
  -- Second pass after toolkit chrome settles (late map / min-size apply).
  if settle_ms > 0 then
    hl.timer(function()
      if my ~= gen then
        return
      end
      run_arrange()
    end, { timeout = settle_ms, type = "oneshot" })
  end
end

function M.setup()
  -- Avoid double-registering on config reload.
  if M._bound then
    return
  end
  M._bound = true

  hl.on("window.open", function(w)
    if should_skip(w) then
      return
    end
    schedule_arrange()
  end)
end

M.setup()

return M
