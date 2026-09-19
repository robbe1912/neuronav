-- a GLOBAL table: no local decl, shared namespace
GlobalSvc = {}

function GlobalSvc.ping() return "pong" end
function GlobalSvc.unused_g() return 0 end

return GlobalSvc
