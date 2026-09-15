@echo off
title WebBot - 拉取真实账单数据
cd /d "%~dp0"

echo ==========================================
echo  WebBot - 绑定 VPS 真实账单数据
echo ==========================================
echo.

echo [1/4] 检查运行环境...
python -c "import flask" >nul 2>&1
if errorlevel 1 goto envfail

echo [2/4] 关闭旧的 WebBot 服务(如有)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8080 ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>&1

echo [3/4] 从 VPS 拉取最新账单数据(需要输入服务器密码)...
if not exist "%~dp0vps_data" mkdir "%~dp0vps_data"
scp "root@[2a02:4780:5e:74bf::1]:/opt/ledgerbot/data/*" "%~dp0vps_data"
if errorlevel 1 echo     拉取失败(检查网络/密码/VPS是否开机), 将使用 vps_data 里的旧数据。

echo [4/4] 启动 WebBot...
ping -n 3 127.0.0.1 >nul
set "WEB_DATA_DIR=%~dp0vps_data"
start "" http://127.0.0.1:8080
python app.py

echo.
echo 服务已退出, 窗口可以关闭。
pause
exit /b

:envfail
echo     这台电脑的 python 还没装 flask, 无法启动。
echo     请先打开命令行窗口运行一次:  pip install flask
echo     然后再重新双击本脚本。
pause
