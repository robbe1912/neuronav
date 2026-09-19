-- LÖVE convention root (main.lua)
local plug = require("myplug.init")

function love.load()
    plug.setup({})
end

function love.update(dt)
    GlobalSvc.ping()
    local D = require("myplug.base")
    D:unreachable_through_index()
end
