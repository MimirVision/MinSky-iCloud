@echo off
chcp 65001 >nul
title Min Sky til iCloud - installasjon
rem Installerer Min Sky til iCloud paa denne PC-en. Dobbeltklikk for aa kjore.
rem Trygt aa kjore flere ganger (oppdaterer til nyeste versjon).
cd /d "%~dp0.."
echo.
echo == Min Sky til iCloud: installasjon ==
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo Installerer Python fra Microsoft...
  winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
  echo.
  echo Python er installert. Lukk dette vinduet og dobbeltklikk Installer.bat en gang til.
  pause
  exit /b 0
)

echo Installerer programmet og det det trenger...
py -3 -m pip install --upgrade pip >nul
py -3 -m pip install --upgrade .
if errorlevel 1 (
  echo.
  echo Noe gikk galt under installasjonen. Se meldingene over.
  pause
  exit /b 1
)

py -3 -m minsky_icloud snarvei
echo.
echo Ferdig! Det ligger naa en snarvei "Min Sky til iCloud" paa skrivebordet.
echo.
echo Husk: installer "iCloud" fra Microsoft Store og sla paa "Bilder" foer du starter.
echo.
echo Starter programmet...
py -3 -m minsky_icloud app
