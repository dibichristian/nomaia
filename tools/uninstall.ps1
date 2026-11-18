# Desinstalle NOMAIA.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 -RemoveData
#
# Par defaut, les donnees de %APPDATA%\NOMAIA sont CONSERVEES : reinstaller
# NOMAIA retrouve toutes les taches. -RemoveData les supprime definitivement.

param(
    [switch]$RemoveData,
    [switch]$Silent
)

$ErrorActionPreference = 'Stop'

$programme   = Join-Path $env:LOCALAPPDATA 'Programs\NOMAIA'
$donnees     = Join-Path $env:APPDATA 'NOMAIA'
$cleRegistre = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\NOMAIA'

$raccourcis = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'NOMAIA.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Startup')) 'NOMAIA.lnk'),
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\NOMAIA.lnk')
)

if (-not $Silent) {
    Write-Host "Desinstallation de NOMAIA..." -ForegroundColor Cyan
}

# --- 1. Arret du programme ----------------------------------------------------
Get-Process NOMAIA -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

# --- 2. Raccourcis et entree de registre --------------------------------------
foreach ($lien in $raccourcis) {
    if (Test-Path $lien) { Remove-Item $lien -Force }
}
if (Test-Path $cleRegistre) { Remove-Item $cleRegistre -Recurse -Force }

# --- 3. Donnees ---------------------------------------------------------------
if ($RemoveData) {
    if (Test-Path $donnees) {
        Remove-Item $donnees -Recurse -Force
        Write-Host "  donnees supprimees : $donnees" -ForegroundColor Yellow
    }
} else {
    Write-Host "  donnees conservees : $donnees"
    Write-Host "  (relance uninstall.ps1 -RemoveData pour les supprimer)"
}

# --- 4. Programme -------------------------------------------------------------
# Le script se trouve dans le dossier qu'il doit supprimer : il est copie dans
# le dossier temporaire, puis relance depuis la pour effacer l'original.
if ($PSScriptRoot -eq $programme) {
    $copie = Join-Path $env:TEMP 'nomaia_uninstall.ps1'
    Copy-Item $PSCommandPath $copie -Force
    $arguments = "-ExecutionPolicy Bypass -File `"$copie`" -Silent"
    if ($RemoveData) { $arguments += " -RemoveData" }
    Start-Process powershell -ArgumentList $arguments
    return
}

if (Test-Path $programme) {
    Remove-Item $programme -Recurse -Force
}

if (-not $Silent) {
    Write-Host ""
    Write-Host "NOMAIA est desinstalle." -ForegroundColor Green
}
