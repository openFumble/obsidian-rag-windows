<#
.SYNOPSIS
    Removes obsidian-rag-windows from Claude Desktop and deletes its index.

.DESCRIPTION
    Your vault is never touched. Ollama, the embedding model and uv stay installed
    (they may be used by other programs); the commands to remove them are printed at the end.

.EXAMPLE
    .\uninstall.ps1
.EXAMPLE
    .\uninstall.ps1 -KeepData
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "obsidian-rag-windows"),
    [string]$ServerName = "obsidian-rag",
    [string]$ClaudeConfig,
    [switch]$KeepData,
    [switch]$Yes
)
$ErrorActionPreference = "Stop"

function Ask-YesNo($question, [bool]$default = $true) {
    if ($Yes) { return $default }
    $suffix = "[Y/n]"
    if (-not $default) { $suffix = "[y/N]" }
    $answer = Read-Host "$question $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $default }
    return $answer.Trim().ToLower().StartsWith("y") -or $answer.Trim().ToLower().StartsWith("j")
}

Write-Host ""
Write-Host "obsidian-rag-windows uninstaller" -ForegroundColor White

$running = Get-Process -Name "Claude" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "Claude Desktop is running and must be closed first." -ForegroundColor Yellow
    if (Ask-YesNo "Close Claude Desktop now?") { $running | Stop-Process -Force; Start-Sleep -Seconds 2 }
    else { Write-Host "Please quit Claude Desktop and run the uninstaller again."; exit 1 }
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    $c = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $c) { $uv = $c }
}
$script = Join-Path $InstallDir "obsidian_rag.py"
if ($uv -and (Test-Path $script)) {
    $regArgs = @("run", "--script", $script, "register", "--name", $ServerName, "--remove")
    if ($ClaudeConfig) { $regArgs += @("--claude-config", $ClaudeConfig) }
    & $uv @regArgs
    Write-Host "Removed '$ServerName' from the Claude Desktop config (a backup was kept next to it)." -ForegroundColor Green
} else {
    Write-Host "Could not remove the Claude Desktop entry automatically." -ForegroundColor Yellow
    Write-Host "Claude Desktop > Settings > Developer > Edit Config, delete the '$ServerName' block."
}

if (-not $KeepData -and (Test-Path $InstallDir)) {
    if (Ask-YesNo "Delete the index, config and log in $InstallDir?") {
        Remove-Item -Recurse -Force $InstallDir
        Write-Host "Deleted $InstallDir" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "Your vault was not changed."
Write-Host "Still installed (remove only if nothing else needs them):"
Write-Host "  Embedding model:  ollama rm bge-m3"
Write-Host "  Ollama:           winget uninstall Ollama.Ollama"
Write-Host "  uv:               uv cache clean   then delete uv.exe from $env:USERPROFILE\.local\bin"
