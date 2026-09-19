local b = require("myplug.circ_b")

local function a_fn()
    return b.b_fn()
end

return { a_fn = a_fn }
