@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Preparando o ambiente pela primeira vez...
  python -m venv .venv
  if errorlevel 1 goto sem_python
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Nao foi possivel instalar as bibliotecas. Verifique a internet e tente de novo.
  pause
  exit /b 1
)

start "" /min cmd /c "timeout /t 3 >nul & start http://localhost:5000"
".venv\Scripts\python.exe" main.py
pause
exit /b 0

:sem_python
echo.
echo Nao foi possivel iniciar. Instale o Python 3 (python.org) marcando "Add Python to PATH".
pause
exit /b 1
