# Recompile le lanceur (bootloader) de PyInstaller sur ce poste.
#
#   powershell -ExecutionPolicy Bypass -File tools\build_bootloader.ps1
#
# Pourquoi : le lanceur fourni avec PyInstaller est le meme pour tous ceux qui
# l'utilisent, malwares compris. Les antivirus (Windows Defender en tete) le
# reconnaissent et signalent NOMAIA.exe a tort (ex. Trojan:Win32/Bearfoos.A!ml).
# Un lanceur compile ici a une empreinte propre a ce poste.
#
# Ce que fait le script, une seule fois (puis a chaque montee de PyInstaller) :
#   1. verifie les outils de compilation C++ de Visual Studio ;
#   2. cree l'environnement isole .venv-build\ (le Python global n'est pas touche),
#      avec les memes versions de bibliotheques que le Python global ;
#   3. recupere les sources de PyInstaller a la version voulue (build-cache\) ;
#   4. recompile le lanceur et l'installe avec PyInstaller dans .venv-build\ ;
#   5. verifie que le lanceur obtenu differe du lanceur standard.
#
# Ensuite, tools\build.ps1 construit automatiquement avec .venv-build\.
# Supprimer .venv-build\ revient au PyInstaller standard du Python global.

param(
    # Version de PyInstaller ; par defaut celle du Python global.
    [string]$Version
)

$ErrorActionPreference = 'Stop'

$projet = Split-Path -Parent $PSScriptRoot
$venv   = Join-Path $projet '.venv-build'
$python = Join-Path $venv 'Scripts\python.exe'
$cache  = Join-Path $projet 'build-cache'

function Invoke-Native {
    # Lance une commande externe et s'arrete si elle echoue : $ErrorActionPreference
    # ne couvre pas les executables, seulement les cmdlets.
    param([string]$Label, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Label a echoue (code $LASTEXITCODE)." }
}

function Get-RunwHash {
    # Empreinte du lanceur fenetre (runw.exe) d'un dossier PyInstaller.
    param([string]$PackageDir)
    $runw = Join-Path $PackageDir 'bootloader\Windows-64bit-intel\runw.exe'
    if (-not (Test-Path $runw)) { throw "Lanceur introuvable : $runw" }
    return (Get-FileHash $runw -Algorithm SHA256).Hash
}

# --- 1. Outils de compilation -----------------------------------------------
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsPath = $null
if (Test-Path $vswhere) {
    $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
}
if (-not $vsPath) {
    throw @"
Outils de compilation C++ absents. Installe-les une fois (PowerShell administrateur, ~6 Go) :
  winget install Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
puis relance ce script.
"@
}
$vcvars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "vcvars64.bat introuvable dans $vsPath." }
Write-Host "Compilateur : $vsPath" -ForegroundColor Cyan

# --- 2. Version et environnement isole --------------------------------------
if (-not $Version) {
    $Version = (& python -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Version) { throw "PyInstaller absent du Python global : precise -Version." }
}
Write-Host "PyInstaller $Version"

if (-not (Test-Path $python)) {
    Write-Host "Creation de .venv-build..."
    Invoke-Native 'Creation de .venv-build' { python -m venv $venv }
}
Invoke-Native 'Mise a jour de pip' { & $python -m pip install --quiet --upgrade pip wheel setuptools }

# --- 3. Sources de PyInstaller ----------------------------------------------
$sources = Join-Path $cache "pyinstaller-$Version"
if (-not (Test-Path $sources)) {
    New-Item -ItemType Directory -Force $cache | Out-Null
    Write-Host "Recuperation des sources (v$Version)..."
    Invoke-Native 'git clone' {
        git clone --quiet --depth 1 --branch "v$Version" https://github.com/pyinstaller/pyinstaller.git $sources
    }
}
# Le depot contient le lanceur standard deja compile : son empreinte sert de reference.
$standard = Get-RunwHash (Join-Path $sources 'PyInstaller')

# --- 4. Compilation et installation ------------------------------------------
Write-Host "Compilation du lanceur (1 a 2 minutes)..." -ForegroundColor Cyan
# vcvars64 prepare l'environnement du compilateur 64 bits pour waf. Passe par
# un fichier .cmd : les guillemets imbriques de cmd /c sont mal transmis par
# PowerShell 5.1, et le chemin de Visual Studio contient espaces et parentheses.
$script = Join-Path $cache 'compile_bootloader.cmd'
@"
@echo off
call "$vcvars" >nul || exit /b 1
cd /d "$(Join-Path $sources 'bootloader')" || exit /b 1
"$python" ./waf distclean all
"@ | Set-Content -Encoding ascii $script
Invoke-Native 'Compilation du lanceur' { cmd /c $script }
$compile = Get-RunwHash (Join-Path $sources 'PyInstaller')
if ($compile -eq $standard) { throw "Le lanceur n'a pas ete recompile (empreinte identique au standard)." }

# Memes versions que le Python global, celles qui tournent aujourd'hui :
# requirements.txt n'impose que des minimums, et un environnement neuf
# prendrait les dernieres versions (anthropic, huggingface_hub... en version
# majeure superieure), soit un exe different de celui qui a ete teste.
$contraintes = Join-Path $cache 'constraints.txt'
& python -m pip freeze --exclude-editable |
    Where-Object { $_ -match '^[A-Za-z0-9_.\-]+==' -and $_ -notmatch '^pyinstaller==' } |
    Set-Content -Encoding ascii $contraintes
if ($LASTEXITCODE -ne 0) { throw "Lecture des versions du Python global a echoue." }

Write-Host "Installation de PyInstaller dans .venv-build..."
Invoke-Native 'Installation de PyInstaller' { & $python -m pip install --quiet --force-reinstall --no-deps $sources }
Invoke-Native 'Dependances de PyInstaller' { & $python -m pip install --quiet $sources -c $contraintes }
Invoke-Native 'Dependances de NOMAIA' {
    & $python -m pip install --quiet -r (Join-Path $projet 'requirements.txt') -c $contraintes
}

# --- 5. Verification -----------------------------------------------------------
$installe = (& $python -c "import os, PyInstaller; print(os.path.dirname(PyInstaller.__file__))").Trim()
if ((Get-RunwHash $installe) -ne $compile) {
    throw "Le PyInstaller de .venv-build n'embarque pas le lanceur recompile."
}
"PyInstaller $Version, lanceur recompile le $(Get-Date -Format 'yyyy-MM-dd HH:mm') ($compile)" |
    Set-Content -Encoding utf8 (Join-Path $venv 'bootloader.txt')

Write-Host ""
Write-Host "Lanceur recompile et installe dans .venv-build." -ForegroundColor Green
Write-Host "Construire : powershell -ExecutionPolicy Bypass -File tools\build.ps1 -Install"
