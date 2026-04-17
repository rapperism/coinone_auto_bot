#!/bin/bash
set -e
source venv/Scripts/activate
python -c "import requests" 2>/dev/null || pip install -r requirements.txt
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
python -X utf8 bot.py
