# Starts backend (FastAPI, :8000) and frontend dev server (Vite, :5173) in separate windows.
$root = $PSScriptRoot

Start-Process powershell -ArgumentList '-NoExit', '-Command', "Set-Location '$root\backend'; & '.\.venv\Scripts\uvicorn.exe' app.main:app --port 8000 --reload"

Start-Process powershell -ArgumentList '-NoExit', '-Command', "Set-Location '$root\frontend'; npm run dev"

Write-Host 'Backend:  http://localhost:8000  (API docs: http://localhost:8000/docs)'
Write-Host 'Frontend: http://localhost:5173'
