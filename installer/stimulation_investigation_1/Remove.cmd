@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Manage-Package.ps1" -Mode Remove %*
if errorlevel 1 (echo Removal did not complete.)
pause
