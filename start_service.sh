#!/bin/bash
cd "$(dirname "$0")"
python -c "import flask" 2>/dev/null || pip install flask
python my_service.py
