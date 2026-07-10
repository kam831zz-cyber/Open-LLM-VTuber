@echo off
setlocal

cd /d C:\home-ai\Open-LLM-VTuber

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:12393/' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  start "" "http://127.0.0.1:12393"
  echo Open-LLM-VTuber is already running.
  exit /b 0
)

if exist ".venv\Lib\site-packages\onnxruntime\capi" (
  set "PATH=C:\home-ai\Open-LLM-VTuber\.venv\Lib\site-packages\onnxruntime\capi;%PATH%"
)

start "" "http://127.0.0.1:12393"

uv run run_server.py

echo.
echo Open-LLM-VTuber stopped or failed.
pause
