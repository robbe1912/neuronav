-- dynamic require: mention-floor discipline (issue #342)
local function load_mod(name)
    return require("myplug." .. name)
end

-- unresolved literal: loud degrade, never a silent drop
local ext = require("some.external.lib")

return { load_mod = load_mod }
