# swmg-nav bootstrap — one command per clone/worktree. Idempotent.
#
#   powershell -ExecutionPolicy Bypass -File .swmg-nav\bootstrap.ps1
#
# Requires: Python 3.11+, Ollama running with `ollama pull qwen3-embedding:0.6b`.
# uv is used if available (faster), falls back to python -m venv + pip.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path   # .swmg-nav
$root = Split-Path -Parent $here                           # checkout root
$py = Join-Path $here ".venv\Scripts\python.exe"

Write-Host "== swmg-nav bootstrap =="
Write-Host "checkout: $root"

# 1. venv + deps
if (-not (Test-Path $py)) {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        uv venv (Join-Path $here ".venv") | Out-Null
        uv pip install --python $py chromadb httpx "mcp<2" | Out-Null
    } else {
        python -m venv (Join-Path $here ".venv")
        & $py -m pip install --quiet --disable-pip-version-check chromadb httpx "mcp<2"
    }
}
Write-Host "deps: ok"

# 2. seed from tracked base shards (first run only), then heal to HEAD
& $py -X utf8 (Join-Path $here "nav.py") import-base
& $py -X utf8 (Join-Path $here "nav.py") rescan

$pyJson = ($py -replace "\\", "\\")
$srvJson = ((Join-Path $here "server.py") -replace "\\", "\\")
$serverCmd = @($pyJson, "-X", "utf8", $srvJson)

# 3a. OpenCode (project opencode.json is gitignored; per-checkout local wiring)
$ocPath = Join-Path $root "opencode.json"
$oc = if (Test-Path $ocPath) { Get-Content $ocPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
$oc | Add-Member -Force NoteProperty instructions @(".swmg-nav/agents.md")
$mcp = [pscustomobject]@{ type = "local"; command = $serverCmd; enabled = $true }
if ($oc.PSObject.Properties.Name -contains "mcp") {
    $oc.mcp | Add-Member -Force NoteProperty "swmg-nav" $mcp
} else {
    $oc | Add-Member -Force NoteProperty mcp ([pscustomobject]@{ "swmg-nav" = $mcp })
}
$oc | ConvertTo-Json -Depth 10 | Set-Content $ocPath -Encoding utf8
Write-Host "opencode: wired"

# 3b. Claude Code (project .mcp.json — committable so teammates get it free)
$ccPath = Join-Path $root ".mcp.json"
$cc = if (Test-Path $ccPath) { Get-Content $ccPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
$ccServer = [pscustomobject]@{ command = $pyJson; args = @("-X", "utf8", $srvJson) }
if ($cc.PSObject.Properties.Name -contains "mcpServers") {
    $cc.mcpServers | Add-Member -Force NoteProperty "swmg-nav" $ccServer
} else {
    $cc | Add-Member -Force NoteProperty mcpServers ([pscustomobject]@{ "swmg-nav" = $ccServer })
}
$cc | ConvertTo-Json -Depth 10 | Set-Content $ccPath -Encoding utf8
Write-Host "claude code: wired (.mcp.json)"

# 3c. VS Code (project .vscode/mcp.json)
$vsDir = Join-Path $root ".vscode"
$vsPath = Join-Path $vsDir "mcp.json"
$vs = if (Test-Path $vsPath) { Get-Content $vsPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
$vsServer = [pscustomobject]@{ command = $pyJson; args = @("-X", "utf8", $srvJson) }
if ($vs.PSObject.Properties.Name -contains "servers") {
    $vs.servers | Add-Member -Force NoteProperty "swmg-nav" $vsServer
} else {
    $vs | Add-Member -Force NoteProperty servers ([pscustomobject]@{ "swmg-nav" = $vsServer })
}
New-Item -ItemType Directory -Force -Path $vsDir | Out-Null
$vs | ConvertTo-Json -Depth 10 | Set-Content $vsPath -Encoding utf8
Write-Host "vs code: wired (.vscode/mcp.json)"

# 3d. Codex (global-only by design: ~/.codex/config.toml)
$codexDir = Join-Path $env:USERPROFILE ".codex"
$codexPath = Join-Path $codexDir "config.toml"
if (-not (Test-Path $codexDir)) { New-Item -ItemType Directory -Force -Path $codexDir | Out-Null }
$block = "[mcp_servers.swmg-nav]`ncommand = `"$pyJson`"`nargs = [`"-X`", `"utf8`", `"$srvJson`"]`n"
$existing = if (Test-Path $codexPath) { Get-Content $codexPath -Raw } else { "" }
if ($existing -notmatch "mcp_servers\.swmg-nav") {
    Add-Content -Path $codexPath -Value "`n$block" -Encoding utf8
    Write-Host "codex: wired (~/.codex/config.toml)"
} else {
    Write-Host "codex: already wired"
}

Write-Host "== done. restart your client session. =="
