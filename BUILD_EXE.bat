@echo off
cd /d "%~dp0"
py -3 -m pip install --disable-pip-version-check -q pillow tkinterdnd2 pyinstaller
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name "Tool RECVX PS2 Texts" ^
  --collect-all tkinterdnd2 ^
  --icon "recvx_claire.ico" ^
  --add-data "recvx_claire.ico;." ^
  --add-data "ENG.tbl;." ^
  --add-data "FRA.tbl;." ^
  --add-data "SPA.tbl;." ^
  --add-data "Custom TBL AR.tbl;." ^
  --add-data "GER.tbl;." ^
  --add-data "ARA.tbl;." ^
  --add-data "JPN.tbl;." ^
  recvx_text_studio.pyw
if exist "dist\Tool RECVX PS2 Texts.exe" explorer /select,"%cd%\dist\Tool RECVX PS2 Texts.exe"
