-- paren-less require idiom (plenary-style, the dominant Neovim form)
require "myplug.circ_a"
local util = require "myplug.util"

local P = {}

function P.run(x)
    util.log("paren-less chain")
    return x + 1
end

return P
