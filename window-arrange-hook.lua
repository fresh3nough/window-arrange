-- window-arrange-hook.lua
-- Auto-arrange every mapped app window as soon as it opens on Omarchy/Hyprland.
-- Installed to ~/.config/hypr/window-arrange-hook.lua and required from autostart.
--
-- window.open fires after window rules are applied. A short oneshot debounce
-- coalesces burst opens (boot / multi-window launch) into a single arrange.

local M = {}

local gen = 0
local debounce_ms = tonumber(os.getenv("WINDOW_ARRANGE_OPEN_DEBOUNCE_MS") or "") or 80
if debounce_ms < 0 then
  debounce_ms = 0
end
if debounce_ms > 2000 then
  debounce_ms = 2000
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
  hl.exec_cmd("env WINDOW_ARRANGE_NOTIFY=0 window-arrange")
end

local function schedule_arrange()
  gen = gen + 1
  local my = gen
  if debounce_ms <= 0 then
    run_arrange()
    return
  end
  hl.timer(function()
    if my ~= gen then
      return
    end
    run_arrange()
  end, { timeout = debounce_ms, type = "oneshot" })
end

function M.setup()
  hl.on("window.open", function(w)
    if should_skip(w) then
      return
    end
    schedule_arrange()
  end)
end

M.setup()

return M
