-- plugin root (Neovim layout): lua/myplug/init.lua
local util = require("myplug.util")
local M = {}

function M.setup(opts)
    util.log("setup")
    return M
end

function M.teardown()
    local T = {}
    function T.inner() return 1 end
    T:inner()
    T.inner()
    return T.inner()
end

return M
