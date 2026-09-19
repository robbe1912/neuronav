-- busted-style spec: test-dir + _spec entry rules
local function test_add()
    assert(1 + 1 == 2)
end

local function spec_helper()
    return test_add()
end
