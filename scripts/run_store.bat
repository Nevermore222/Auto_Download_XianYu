@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0.."
REM 可选：指定端口（避免端口被占用）
REM set STORE_PORT=8771
REM 可选：指定监听地址
REM set STORE_HOST=127.0.0.1

REM 启动前：自动杀掉占用端口的残留进程（避免 Errno 10048）
set "STORE_PORT_EFFECTIVE=%STORE_PORT%"
if "%STORE_PORT_EFFECTIVE%"=="" set "STORE_PORT_EFFECTIVE=8770"
echo [INFO] 检查端口占用：%STORE_PORT_EFFECTIVE%
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%STORE_PORT_EFFECTIVE% .*LISTENING"') do (
  if not "%%P"=="0" (
    echo [INFO] 发现占用端口的进程 PID=%%P，正在结束...
    taskkill /F /PID %%P >nul 2>nul
  )
)

python -m src.store.app
if errorlevel 1 (
  echo.
  echo [ERROR] 卖家工作台启动失败（请截图本窗口错误信息）
  pause
)
