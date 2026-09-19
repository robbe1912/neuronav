-- dead module: never required, no convention name
local function orphan_fn() return 1 end

local function orphan_two()
    return orphan_fn()
end

return orphan_two
