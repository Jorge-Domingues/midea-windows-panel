@echo off
rem Painel do Ar Condicionado — a primeira partida prepara o ambiente sozinha
setlocal
cd /d "%~dp0"

set "PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
set "PYTHON=%~dp0.venv\Scripts\python.exe"

rem --- 1) venv já existe? acha um Python e cria na hora ---
if exist "%PYTHONW%" goto :deps
where py >nul 2>&1
if errorlevel 1 (
  echo Nenhum Python encontrado. Instale de https://www.python.org/downloads/
  echo Deixe marcado "Add python.exe to PATH" na instalacao.
  pause
  exit /b 1
)

echo Preparando o ambiente ^(executa so na primeira vez, ~1 minuto^)...
py -m venv .venv
if errorlevel 1 (
  echo Falha ao criar o ambiente virtual.
  pause
  exit /b 1
)
"%PYTHON%" -m pip install --upgrade pip -r requirements.txt
if errorlevel 1 (
  echo Falha ao instalar dependencias. Verifique sua conexao e rode de novo.
  pause
  exit /b 1
)

:deps
rem --- 2) componente de descoberta de rede (idempotente) ---
"%PYTHON%" -c "import psutil" >nul 2>&1
if errorlevel 1 (
  echo Instalando componente de descoberta de rede...
  "%PYTHON%" -m pip install "psutil>=5.9.0"
)

rem --- 3) partida silenciosa do painel ---
start "" "%PYTHONW%" "%~dp0web_panel.py"
exit /b 0
