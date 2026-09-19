local M = {}

function M.log(msg)
    print(msg)
    return msg
end

function M.unused_helper() return 42 end

return M
