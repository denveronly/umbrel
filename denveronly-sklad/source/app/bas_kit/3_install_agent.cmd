@echo off
rem Registers the Sklady -> BAS agent in Windows Task Scheduler (runs hidden at logon).
cd /d "%~dp0"
if not exist agent.json (
  copy agent.example.json agent.json >nul
  echo agent.json created - fill url, token, user and run this file again.
  notepad agent.json
  exit /b
)
schtasks /Create /TN "Sklady BAS agent" /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%~dp02_agent.ps1\"" /SC ONLOGON /RL LIMITED /F
schtasks /Run /TN "Sklady BAS agent"
echo.
echo Agent installed and started. Log: %~dp0agent.log
pause
