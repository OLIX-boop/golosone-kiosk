@echo off
cd /d %~dp0
pip install -r requirements.txt
pyinstaller --noconsole --onefile --name GolosoneKiosk --icon golosone.ico --add-data "ui;ui" ^
  --collect-all comtypes --collect-submodules pywinauto app.py
if exist config.json (copy /Y config.json dist\config.json) else (copy /Y config.example.json dist\config.json)
echo.
echo Fatto: dist\GolosoneKiosk.exe  (config.json deve stare nella stessa cartella)
pause
