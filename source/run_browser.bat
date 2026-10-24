@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if "%~1"=="" (
  echo Usage: run_browser.bat GRAPH_NODE_ID
  exit /b 2
)
python "%SCRIPT_DIR%chunk_browser.py" --db "%SCRIPT_DIR%cohesive_ledger.sqlite" --graph-node-id "%~1"
endlocal
