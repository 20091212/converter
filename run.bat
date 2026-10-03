@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Hermes 等父进程可能注入 PYTHONPATH，会遮蔽本项目的虚拟环境，这里清空
set PYTHONPATH=
if not exist ".venv\Scripts\pythonw.exe" (
    echo [错误] 未找到 .venv，请先执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0
