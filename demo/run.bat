@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo === Имплант-Дент DEMO ===
echo Установка зависимостей...
pip install -r requirements.txt
echo.
echo Запуск веб-демо на http://localhost:8000
echo Открой в браузере http://localhost:8000
echo Для остановки нажми Ctrl+C
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
