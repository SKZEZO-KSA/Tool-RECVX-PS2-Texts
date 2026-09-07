@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -c "import PIL, tkinterdnd2" >nul 2>nul
  if errorlevel 1 py -3 -m pip install --disable-pip-version-check -q -r requirements.txt
  start "" pyw -3 "%~dp0recvx_text_studio.pyw"
  exit /b
)
where python >nul 2>nul
if %errorlevel%==0 (
  python -c "import PIL, tkinterdnd2" >nul 2>nul
  if errorlevel 1 python -m pip install --disable-pip-version-check -q -r requirements.txt
  start "" pythonw "%~dp0recvx_text_studio.pyw"
  exit /b
)
echo Python 3 is required.
pause
