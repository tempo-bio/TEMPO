@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Manage-Package.ps1" -Mode Install %*
if errorlevel 1 (echo Installation did not complete.)
pause
