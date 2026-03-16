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
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = [int]$env:STORE_PORT_EFFECTIVE; $pids = @(); try { $pids = (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique) } catch { $pids = (netstat -ano | Select-String (':'+$p+'\\s') | ForEach-Object { ($_.ToString() -split '\\s+')[-1] } | Sort-Object -Unique) }; foreach ($pid in $pids) { if ($pid -and $pid -ne 0) { try { Stop-Process -Id $pid -Force -ErrorAction Stop; Write-Host (\"[INFO] 已结束占用端口的进程 PID=${pid}\") } catch { Write-Host (\"[WARN] 结束进程失败 PID=${pid}: $($_.Exception.Message)\") } } }"

python -m src.store.app
if errorlevel 1 (
  echo.
  echo [ERROR] 卖家工作台启动失败（请截图本窗口错误信息）
  pause
)
