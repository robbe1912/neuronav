-- metatable __index chain: Derived falls back to Base
local Base = {}
local Derived = setmetatable({}, { __index = Base })

function Base.b_meth() return 1 end
function Derived.d_meth() return 2 end

return Derived
