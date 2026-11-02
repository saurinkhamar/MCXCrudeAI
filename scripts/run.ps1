# Launch the assistant API and Vite dev server as separate local PowerShell processes.
# Bind the API to loopback; do not expose this single-user app on a public interface.
$root=Split-Path -Parent $PSScriptRoot
Start-Process powershell -ArgumentList '-NoExit','-Command',"Set-Location '$root\backend'; & .\.venv\Scripts\python.exe -m uvicorn mcx_ai.main:app --host 127.0.0.1 --port 8001 --reload"
Start-Process powershell -ArgumentList '-NoExit','-Command',"Set-Location '$root\frontend'; npm run dev"
