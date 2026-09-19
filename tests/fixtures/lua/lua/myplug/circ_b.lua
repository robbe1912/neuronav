local a = require("myplug.circ_a")

local function b_fn() return 1 end

local function b_unused()
    return a
end

return { b_fn = b_fn }
