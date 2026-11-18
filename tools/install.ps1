# Installe NOMAIA comme une application Windows ordinaire.
#
#   powershell -ExecutionPolicy Bypass -File tools\install.ps1
#
# Aucun droit administrateur : tout se fait dans le profil de l'utilisateur.
#
#   %LOCALAPPDATA%\Programs\NOMAIA    le programme, remplace a chaque mise a jour
#   %APPDATA%\NOMAIA                  les donnees, jamais touchees
#
# L'entree de desinstallation est ecrite dans HKCU, donc visible dans
# Parametres > Applications > Applications installees.

param(
    [switch]$NoLaunch,
    [switch]$NoStartup
)

$ErrorActionPreference = 'Stop'

$projet      = Split-Path -Parent $PSScriptRoot
$source      = Join-Path $projet 'dist\NOMAIA'
$programme   = Join-Path $env:LOCALAPPDATA 'Programs\NOMAIA'
$donnees     = Join-Path $env:APPDATA 'NOMAIA'
$executable  = Join-Path $programme 'NOMAIA.exe'
$icone       = Join-Path $programme '_internal\static\img\icon.ico'
$cleRegistre = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\NOMAIA'

$bureau      = [Environment]::GetFolderPath('Desktop')
$demarrage   = [Environment]::GetFolderPath('Startup')
$menu        = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'

if (-not (Test-Path $source)) {
    throw "Build introuvable dans $source. Lance d'abord : powershell -File tools\build.ps1"
}

Write-Host "Installation de NOMAIA..." -ForegroundColor Cyan

# --- 1. Arret de l'instance en cours ------------------------------------------
Get-Process NOMAIA -ErrorAction SilentlyContinue | Stop-Process -Force
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*tray.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# --- 2. Copie du programme ----------------------------------------------------
if (Test-Path $programme) {
    Remove-Item $programme -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $programme | Out-Null
Copy-Item "$source\*" $programme -Recurse -Force
Copy-Item (Join-Path $PSScriptRoot 'uninstall.ps1') $programme -Force
Write-Host "  programme    : $programme"

# --- 3. Donnees : reprise d'une installation de developpement -----------------
New-Item -ItemType Directory -Force -Path $donnees | Out-Null
$baseCible  = Join-Path $donnees 'nomaia.db'
$baseSource = Join-Path $projet 'nomaia.db'

if ((-not (Test-Path $baseCible)) -and (Test-Path $baseSource)) {
    Copy-Item $baseSource $baseCible -Force
    $uploadsSource = Join-Path $projet 'public\uploads'
    if (Test-Path $uploadsSource) {
        $uploadsCible = Join-Path $donnees 'public\uploads'
        New-Item -ItemType Directory -Force -Path $uploadsCible | Out-Null
        Copy-Item "$uploadsSource\*" $uploadsCible -Recurse -Force
    }
    Write-Host "  donnees      : reprises depuis le dossier de developpement"
} else {
    Write-Host "  donnees      : $donnees (conservees)"
}

# --- 4. Raccourcis ------------------------------------------------------------
$shell = New-Object -ComObject WScript.Shell

function New-Raccourci($chemin) {
    $lien = $shell.CreateShortcut($chemin)
    $lien.TargetPath       = $executable
    $lien.WorkingDirectory = $programme
    $lien.IconLocation     = "$icone,0"
    $lien.Description      = 'NOMAIA - assistant de taches'
    $lien.WindowStyle      = 7
    $lien.Save()
}

New-Raccourci (Join-Path $menu 'NOMAIA.lnk')
New-Raccourci (Join-Path $bureau 'NOMAIA.lnk')
Write-Host "  raccourcis   : menu Demarrer, bureau"

$lienDemarrage = Join-Path $demarrage 'NOMAIA.lnk'
if ($NoStartup) {
    if (Test-Path $lienDemarrage) { Remove-Item $lienDemarrage -Force }
    Write-Host "  demarrage    : desactive"
} else {
    New-Raccourci $lienDemarrage
    Write-Host "  demarrage    : lance a l'ouverture de session"
}

# --- 5. Entree dans Parametres > Applications ---------------------------------
$taille = [math]::Round((Get-ChildItem $programme -Recurse | Measure-Object Length -Sum).Sum / 1KB)

New-Item -Path $cleRegistre -Force | Out-Null
$valeurs = @{
    DisplayName     = 'NOMAIA'
    DisplayVersion  = '1.0.0'
    Publisher       = 'NOMAIA'
    InstallLocation = $programme
    DisplayIcon     = $icone
    UninstallString = "powershell -ExecutionPolicy Bypass -File `"$programme\uninstall.ps1`""
    NoModify        = 1
    NoRepair        = 1
    EstimatedSize   = $taille
}
foreach ($nom in $valeurs.Keys) {
    New-ItemProperty -Path $cleRegistre -Name $nom -Value $valeurs[$nom] -Force | Out-Null
}
Write-Host "  desinstall.  : visible dans Parametres > Applications"

# --- 6. Dependance externe ----------------------------------------------------
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "  ATTENTION : Claude Code est introuvable dans le PATH." -ForegroundColor Yellow
    Write-Host "  Le triage par IA ne fonctionnera pas tant qu'il n'est pas installe :"
    Write-Host "  npm install -g @anthropic-ai/claude-code"
}

Write-Host ""
Write-Host "NOMAIA est installe." -ForegroundColor Green

if (-not $NoLaunch) {
    Start-Process $executable
    Write-Host "Lance. Cherche l'icone dans la barre des taches."
}
