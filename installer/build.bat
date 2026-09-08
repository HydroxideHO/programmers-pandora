@echo off
REM Rebuilds ProgrammersPandora-Setup.exe from the current source files:
REM   1. PyInstaller compiles every tool script into a standalone .exe
REM      (installer\dist\ProgrammersPandora\), so the target PC needs no
REM      Python install at all.
REM   2. Inno Setup packages that compiled folder into one Setup.exe.
REM Run this any time you change the .py files or the installer scripts.

setlocal

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH - it's needed here to run PyInstaller
    echo ^(only for building; the resulting Setup.exe won't need it^).
    exit /b 1
)

python -m PyInstaller --noconfirm --distpath "%~dp0dist" --workpath "%~dp0build" "%~dp0ProgrammersPandora.spec"
if errorlevel 1 (
    echo PyInstaller build failed.
    exit /b 1
)

set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"

if not exist "%ISCC%" (
    for %%P in (
        "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
        "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    ) do (
        if exist %%P set "ISCC=%%~P"
    )
)

if not exist "%ISCC%" (
    echo Inno Setup's ISCC.exe was not found.
    echo Install it from https://jrsoftware.org/isdl.php, or run:
    echo     winget install JRSoftware.InnoSetup
    exit /b 1
)

"%ISCC%" "%~dp0ProgrammersPandora.iss"
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Build succeeded: %~dp0Output\ProgrammersPandora-Setup.exe
