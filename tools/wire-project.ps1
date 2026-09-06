# wire-project.ps1 - point a neuronav install at a project and wire its MCP clients.
#
#   powershell -ExecutionPolicy Bypass -File tools\wire-project.ps1 -ProjectPath E:\path\to\proj
#
# One neuronav install can serve MANY projects: each gets a named config
# profile (config/<name>.json here) and per-project MCP entries that pass
# NEURONAV_CONFIG in the server env. Idempotent. BOM-free writes only
# (PowerShell 5 utf8 BOM kills json.loads downstream).

param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [string]$Name = "",
    [string[]]$IncludeDirs = @(),
    [string[]]$Extensions = @(),
    [switch]$WithBaseShards
)

$ErrorActionPreference = "Stop"
$navRoot = (Split-Path -Parent $PSScriptRoot)          # neuronav install
$py = Join-Path $navRoot ".venv\Scripts\python.exe"
$nav = Join-Path $navRoot "nav.py"
$proj = (Resolve-Path $ProjectPath).Path
if (-not $Name) { $Name = Split-Path -Leaf $proj }
if (-not (Test-Path $py))  { throw "venv missing - create $navRoot\.venv first (chromadb httpx 'mcp<2')" }
if (-not (Test-Path $nav)) { throw "nav.py not found at $navRoot" }

# 1. named config profile in the install
$cfg = @{ root = $proj }
if ($IncludeDirs.Count) { $cfg.include_dirs = $IncludeDirs }
if ($Extensions.Count)  { $cfg.extensions = $Extensions }
$cfgPath = Join-Path $navRoot "config\$Name.json"
[System.IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 5))
Write-Host "profile: config/$Name.json -> $proj"

# 2. index (seed from project shards if asked, then rescan)
if ($WithBaseShards) {
    $shards = Join-Path $proj ".neuronav\base"
    if (Test-Path $shards) {
        Copy-Item (Join-Path $shards "*") (Join-Path $navRoot "base") -Force -ErrorAction SilentlyContinue
    }
}
$env:NEURONAV_CONFIG = $cfgPath
& $py -X utf8 $nav import-base
& $py -X utf8 $nav rescan
Remove-Item Env:\NEURONAV_CONFIG -ErrorAction SilentlyContinue

# 3. project-side MCP entries (env selects the profile)
$pyJson = ($py -replace "\\", "\\")
$srvJson = ((Join-Path $navRoot "server.py") -replace "\\", "\\")
$cfgJson = ($cfgPath -replace "\\", "\\")

$ccPath = Join-Path $proj ".mcp.json"
$cc = if (Test-Path $ccPath) { Get-Content $ccPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
$ccServer = [pscustomobject]@{
    command = $pyJson; args = @("-X", "utf8", $srvJson)
    env = [pscustomobject]@{ NEURONAV_CONFIG = $cfgJson }
}
if ($cc.PSObject.Properties.Name -contains "mcpServers") {
    $cc.mcpServers | Add-Member -Force NoteProperty "neuronav" $ccServer
} else {
    $cc | Add-Member -Force NoteProperty mcpServers ([pscustomobject]@{ "neuronav" = $ccServer })
}
[System.IO.File]::WriteAllText($ccPath, ($cc | ConvertTo-Json -Depth 10))
Write-Host ".mcp.json: wired (profile $Name)"

$ocPath = Join-Path $proj "opencode.json"
if (Test-Path $ocPath) {
    $oc = Get-Content $ocPath -Raw | ConvertFrom-Json
    $ocMcp = [pscustomobject]@{
        type = "local"; command = @($pyJson, "-X", "utf8", $srvJson); enabled = $true
        environment = [pscustomobject]@{ NEURONAV_CONFIG = $cfgJson }
    }
    if ($oc.PSObject.Properties.Name -contains "mcp") {
        $oc.mcp | Add-Member -Force NoteProperty "neuronav" $ocMcp
    } else {
        $oc | Add-Member -Force NoteProperty mcp ([pscustomobject]@{ "neuronav" = $ocMcp })
    }
    [System.IO.File]::WriteAllText($ocPath, ($oc | ConvertTo-Json -Depth 10))
    Write-Host "opencode.json: wired"
}

Write-Host "== done. restart client sessions in $proj. agent guidance snippet: templates\agents-snippet.md =="
