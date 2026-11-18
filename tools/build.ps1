# Construit l'executable autonome de NOMAIA.
#
#   powershell -ExecutionPolicy Bypass -File tools\build.ps1
#
# Produit dist\NOMAIA\ : un dossier contenant NOMAIA.exe et ses dependances.
# Aucune installation de Python n'est necessaire sur le poste de destination.

param(
    [switch]$Install
)

$ErrorActionPreference = 'Stop'

$projet = Split-Path -Parent $PSScriptRoot
Push-Location $projet

try {
    Write-Host "Verification des dependances de construction..." -ForegroundColor Cyan
    # Avec .venv-build (tools\build_bootloader.ps1), PyInstaller y embarque un
    # lanceur recompile sur ce poste : ne surtout pas le mettre a jour ici, la
    # version standard l'ecraserait.
    $venvPython = Join-Path $projet '.venv-build\Scripts\python.exe'
    if (Test-Path $venvPython) {
        $python = $venvPython
        Write-Host "Lanceur PyInstaller recompile (.venv-build)."
    } else {
        $python = 'python'
        Write-Host "Lanceur PyInstaller standard (voir tools\build_bootloader.ps1)." -ForegroundColor Yellow
        & $python -m pip install --quiet --upgrade pyinstaller
    }
    & $python -m pip install --quiet -r requirements.txt

    Write-Host "Regeneration des icones..."
    & $python tools\make_icon.py

    Write-Host "Construction de l'executable (2 a 3 minutes)..." -ForegroundColor Cyan
    if (Test-Path build) { Remove-Item build -Recurse -Force }
    if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }

    & $python -m PyInstaller nomaia.spec --noconfirm --log-level ERROR

    $executable = Join-Path $projet 'dist\NOMAIA\NOMAIA.exe'
    if (-not (Test-Path $executable)) {
        throw "La construction a echoue : $executable est absent."
    }

    $taille = [math]::Round((Get-ChildItem (Join-Path $projet 'dist\NOMAIA') -Recurse |
        Measure-Object Length -Sum).Sum / 1MB, 1)

    Write-Host ""
    Write-Host "Construction reussie : dist\NOMAIA ($taille Mo)" -ForegroundColor Green

    if ($Install) {
        & (Join-Path $PSScriptRoot 'install.ps1')
    } else {
        Write-Host "Pour installer : powershell -ExecutionPolicy Bypass -File tools\install.ps1"
    }
}
finally {
    Pop-Location
}
