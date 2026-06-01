# Stress-test compose stack (Windows PowerShell).
#   .\scripts\run_stress.ps1           # 2000 UEs (default)
#   .\scripts\run_stress.ps1 -NumUes 500
param(
    [int]$NumUes = 2000,
    [switch]$Detach
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$env:NUM_UES = "$NumUes"
Write-Host "NUM_UES=$env:NUM_UES"

$args = @("compose", "up", "--build")
if ($Detach) { $args += "-d" }
& docker @args
