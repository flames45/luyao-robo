@echo off
chcp 65001 >nul
title Robo Web
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo 请先安装 Python 3.12，并勾选 Add Python to PATH。
    pause
    exit /b 1
)
python -c "import fastapi, uvicorn, dotenv, requests, psutil, PIL, edge_tts" >nul 2>&1
if errorlevel 1 (
    echo 首次运行，正在安装网页依赖……
    python -m pip install -r requirements.web.txt
    if errorlevel 1 (
        echo 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)
python start_web.py
if errorlevel 1 pause
