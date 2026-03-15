@echo off
cd /d "%~dp0.."
echo 请先在一个终端运行: scripts\run_system.bat
echo 再在另一个终端运行: scripts\run_assistant.bat
echo 然后打开 http://127.0.0.1:8765 使用助手页面。
start cmd /k "scripts\run_system.bat"
timeout /t 3 /nobreak >nul
start cmd /k "scripts\run_assistant.bat"
