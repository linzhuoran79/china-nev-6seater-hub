# Copy generated fund records into the local fund-filing-scraper output folder.
# Run from the repository root after git pull:
#   powershell -ExecutionPolicy Bypass -File scripts/copy_to_local_output.ps1

$SourceDir = Join-Path $PSScriptRoot "..\output" | Resolve-Path
$TargetDir = Join-Path $env:USERPROFILE "Projects\fund-filing-scraper\output"

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $TargetDir "cache") | Out-Null

Copy-Item -Path (Join-Path $SourceDir "*") -Destination $TargetDir -Recurse -Force

Write-Host "Copied files to $TargetDir"
Get-ChildItem $TargetDir | Format-Table Name, Length, LastWriteTime
