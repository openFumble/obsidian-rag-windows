<#
.SYNOPSIS
    Installs obsidian-rag-windows: local RAG search for your Obsidian vault in Claude Desktop.

.DESCRIPTION
    1. Finds your Obsidian vault (or asks for it)
    2. Installs uv (Python runner) if missing
    3. Installs Ollama (local AI runtime) if missing and downloads the embedding model
    4. Copies obsidian_rag.py to %LOCALAPPDATA%\obsidian-rag-windows and writes the config
    5. Builds the first search index
    6. Registers the server in Claude Desktop (a backup of the config is kept)

    Nothing in your vault is changed by the installation. Run it again any time to repair or update.

.EXAMPLE
    .\install.ps1
.EXAMPLE
    .\install.ps1 -VaultPath "D:\Notes" -Yes
#>
[CmdletBinding()]
param(
    [string]$VaultPath,
    [string]$Model = "bge-m3",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "obsidian-rag-windows"),
    [string]$ServerName = "obsidian-rag",
    [string]$ClaudeConfig,
    [switch]$Reconfigure,
    [switch]$SkipOllama,
    [switch]$SkipClaude,
    [switch]$SkipIndex,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$RepoRaw = "https://raw.githubusercontent.com/openFumble/obsidian-rag-windows/main"

function Write-Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    OK  $text" -ForegroundColor Green }
function Write-Info($text) { Write-Host "    $text" }
function Write-Warn2($text){ Write-Host "    !   $text" -ForegroundColor Yellow }
function Fail($text) {
    Write-Host ""
    Write-Host "ERROR: $text" -ForegroundColor Red
    Write-Host "Nothing was registered in Claude Desktop. Fix the problem and run the installer again."
    exit 1
}

function Ask-YesNo($question, [bool]$default = $true) {
    if ($Yes) { return $default }
    $suffix = "[Y/n]"
    if (-not $default) { $suffix = "[y/N]" }
    $answer = Read-Host "$question $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $default }
    return $answer.Trim().ToLower().StartsWith("y") -or $answer.Trim().ToLower().StartsWith("j")
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user;$env:Path"
}

Write-Host ""
Write-Host "obsidian-rag-windows installer" -ForegroundColor White
Write-Host "Local search for your Obsidian vault in Claude Desktop. Your notes never leave this PC."

# ---------------------------------------------------------------------------
Write-Step "1/6  Obsidian vault"
# ---------------------------------------------------------------------------
if (-not $VaultPath) {
    $known = @()
    $obsJson = Join-Path $env:APPDATA "obsidian\obsidian.json"
    if (Test-Path $obsJson) {
        try {
            $obs = Get-Content $obsJson -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($p in $obs.vaults.PSObject.Properties) {
                if ($p.Value.path -and (Test-Path $p.Value.path)) { $known += $p.Value.path }
            }
        } catch { }
    }
    if ($known.Count -eq 1) {
        if (Ask-YesNo "Use the vault '$($known[0])'?") { $VaultPath = $known[0] }
    } elseif ($known.Count -gt 1) {
        Write-Info "Obsidian knows these vaults:"
        for ($i = 0; $i -lt $known.Count; $i++) { Write-Info ("  [{0}] {1}" -f ($i + 1), $known[$i]) }
        $choice = Read-Host "Number of the vault to use (or Enter to choose another folder)"
        if ($choice -match '^\d+$' -and [int]$choice -ge 1 -and [int]$choice -le $known.Count) {
            $VaultPath = $known[[int]$choice - 1]
        }
    }
    if (-not $VaultPath) {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
            $dlg.Description = "Select your Obsidian vault folder"
            if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $VaultPath = $dlg.SelectedPath }
        } catch { }
    }
    if (-not $VaultPath) { $VaultPath = Read-Host "Path to your Obsidian vault folder" }
}
$VaultPath = $VaultPath.Trim().Trim('"')
if (-not (Test-Path $VaultPath -PathType Container)) { Fail "Vault folder not found: $VaultPath" }
$VaultPath = (Resolve-Path $VaultPath).Path
$mdCount = (Get-ChildItem $VaultPath -Recurse -Filter *.md -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\\.(obsidian|trash|git)\\' }).Count
if (-not (Test-Path (Join-Path $VaultPath ".obsidian"))) {
    Write-Warn2 "No .obsidian folder found - is this really an Obsidian vault? Continuing anyway."
}
Write-Ok "$VaultPath ($mdCount notes)"

# ---------------------------------------------------------------------------
Write-Step "2/6  uv (runs the Python program, installs its two libraries)"
# ---------------------------------------------------------------------------
function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($c in @((Join-Path $env:USERPROFILE ".local\bin\uv.exe"), (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe"))) {
        if (Test-Path $c) { return $c }
    }
    return $null
}
$Uv = Find-Uv
if (-not $Uv) {
    Write-Info "Installing uv from astral.sh ..."
    try {
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Host
    } catch { Fail "Could not install uv: $_  (manual: https://docs.astral.sh/uv/getting-started/installation/)" }
    Refresh-Path
    $Uv = Find-Uv
    if (-not $Uv) { Fail "uv was installed but cannot be found. Open a new PowerShell window and run the installer again." }
}
$Uv = (Resolve-Path $Uv).Path
Write-Ok "$Uv ($(& $Uv --version))"

# ---------------------------------------------------------------------------
Write-Step "3/6  Ollama and the embedding model '$Model'"
# ---------------------------------------------------------------------------
function Test-Ollama {
    try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null; return $true }
    catch { return $false }
}
function Find-Ollama {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $c = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $c) { return $c }
    return $null
}
if ($SkipOllama) {
    Write-Warn2 "Skipped (-SkipOllama). Make sure Ollama runs and has the model '$Model'."
} else {
    $Ollama = Find-Ollama
    if (-not $Ollama) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $winget) {
            Start-Process "https://ollama.com/download/windows"
            Fail "Ollama is missing and winget is not available. Install Ollama from the page that just opened, then run the installer again."
        }
        Write-Info "Installing Ollama with winget (about 1 GB, this can take a few minutes) ..."
        & winget install --id Ollama.Ollama -e --silent --accept-package-agreements --accept-source-agreements | Out-Host
        Refresh-Path
        $Ollama = Find-Ollama
        if (-not $Ollama) { Fail "Ollama installation did not finish. Install it from https://ollama.com/download/windows and run the installer again." }
    }
    Write-Ok "Ollama: $Ollama"
    if (-not (Test-Ollama)) {
        Write-Info "Starting Ollama ..."
        $app = Join-Path (Split-Path $Ollama) "ollama app.exe"
        if (Test-Path $app) { Start-Process $app } else { Start-Process $Ollama -ArgumentList "serve" -WindowStyle Hidden }
        $deadline = (Get-Date).AddSeconds(60)
        while (-not (Test-Ollama) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
        if (-not (Test-Ollama)) { Fail "Ollama does not answer on http://127.0.0.1:11434. Start Ollama from the Start menu and run the installer again." }
    }
    Write-Ok "Ollama is running"
    $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 10
    $have = @($tags.models | ForEach-Object { $_.name }) | Where-Object { $_ -eq $Model -or $_ -eq "$($Model):latest" }
    if (-not $have) {
        Write-Info "Downloading the model '$Model' (bge-m3: about 1.2 GB, one time) ..."
        & $Ollama pull $Model
        if ($LASTEXITCODE -ne 0) { Fail "Could not download the model '$Model'." }
    }
    Write-Ok "Model '$Model' is available"
}

# ---------------------------------------------------------------------------
Write-Step "4/6  Program files and configuration"
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$Script = Join-Path $InstallDir "obsidian_rag.py"
$here = $PSScriptRoot
foreach ($f in @("obsidian_rag.py", "uninstall.ps1", "uninstall.cmd")) {
    $src = if ($here) { Join-Path $here $f } else { $null }
    $dst = Join-Path $InstallDir $f
    if ($src -and (Test-Path $src)) {
        Copy-Item $src $dst -Force
    } else {
        Write-Info "Downloading $f ..."
        Invoke-WebRequest -Uri "$RepoRaw/$f" -OutFile $dst -UseBasicParsing
    }
}
Write-Ok "Installed to $InstallDir"

$env:OBSIDIAN_RAG_HOME = $InstallDir
$cfgFile = Join-Path $InstallDir "config.toml"
$initArgs = @("run", "--script", $Script, "init", "--vault", $VaultPath, "--model", $Model)
if ($Reconfigure) { $initArgs += "--force" }
if ((Test-Path $cfgFile) -and -not $Reconfigure) {
    $current = Select-String -Path $cfgFile -Pattern '^vault_path\s*=\s*(.+)$' | Select-Object -First 1
    Write-Info "Keeping the existing config ($cfgFile)."
    if ($current) { Write-Info "Vault in config: $($current.Matches[0].Groups[1].Value)  (use -Reconfigure to change)" }
} else {
    Write-Info "Preparing Python and libraries (first time only) ..."
    & $Uv @initArgs
    if ($LASTEXITCODE -ne 0) { Fail "Could not write the configuration." }
    Write-Ok "Config: $cfgFile"
}

# ---------------------------------------------------------------------------
Write-Step "5/6  First search index"
# ---------------------------------------------------------------------------
if ($SkipIndex) {
    Write-Warn2 "Skipped (-SkipIndex). The server builds the index on its first start."
} else {
    Write-Info "Reading $mdCount notes and computing embeddings. Large vaults take a while; later only changes are processed."
    & $Uv run --script $Script index
    if ($LASTEXITCODE -ne 0) { Write-Warn2 "Some notes could not be indexed (see above). The server retries them automatically." }
    else { Write-Ok "Index built" }
}

# ---------------------------------------------------------------------------
Write-Step "6/6  Claude Desktop"
# ---------------------------------------------------------------------------
if ($SkipClaude) {
    Write-Warn2 "Skipped (-SkipClaude)."
} else {
    $running = Get-Process -Name "Claude" -ErrorAction SilentlyContinue
    if ($running) {
        Write-Warn2 "Claude Desktop is running. It must be closed, otherwise it overwrites the change when it exits."
        if (Ask-YesNo "Close Claude Desktop now?") {
            $running | Stop-Process -Force
            Start-Sleep -Seconds 2
        } else {
            Fail "Please quit Claude Desktop (tray icon > Quit) and run the installer again."
        }
    }
    $regArgs = @("run", "--script", $Script, "register", "--name", $ServerName, "--uv", $Uv)
    if ($ClaudeConfig) { $regArgs += @("--claude-config", $ClaudeConfig) }
    $out = & $Uv @regArgs
    if ($LASTEXITCODE -ne 0) { Fail "Could not update the Claude Desktop config." }
    try {
        foreach ($r in ($out | Out-String | ConvertFrom-Json)) { Write-Ok "$($r.action): $($r.file)" }
    } catch { Write-Info ($out | Out-String) }
}

# ---------------------------------------------------------------------------
Write-Step "Self-test"
# ---------------------------------------------------------------------------
$check = & $Uv run --script $Script check | Out-String
try {
    $c = $check | ConvertFrom-Json
    Write-Info ("Notes indexed: {0}   Chunks: {1}   Vector search: {2}   Keyword search: {3}" -f `
        $c.index.notes, $c.index.chunks, $c.index.vector_search, $c.index.keyword_search)
    Write-Info "Embedding test: $($c.embedding)"
    if ($c.ok) { Write-Ok "Everything works" } else { Write-Warn2 "Self-test reported a problem - see the values above." }
    $selfTestOk = [bool]$c.ok
} catch { Write-Info $check; $selfTestOk = $false }

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Start Claude Desktop and ask for example: 'Search my vault for ...'"
Write-Host "The tools appear under the name '$ServerName'. Index, config and log: $InstallDir"
Write-Host "Uninstall: $InstallDir\uninstall.cmd"
if ($selfTestOk) { exit 0 } else { exit 3 }
