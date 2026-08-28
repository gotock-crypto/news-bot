@echo off
cd /d %~dp0
call .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m py_compile main.py newsbot\app.py newsbot\db.py newsbot\config.py newsbot\telegram\collector.py newsbot\llm\adapter.py newsbot\max\publisher.py
