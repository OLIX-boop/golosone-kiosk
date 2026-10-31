@echo off
cd /d %~dp0
echo Creazione collegamento "Cassa Golosone" sul desktop...
echo.
set /p ADM=Serve avviarlo come amministratore? (S/N, se non sai premi N): 
if /i "%ADM%"=="S" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0crea_collegamento.ps1" -Admin
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0crea_collegamento.ps1"
)
echo.
pause
