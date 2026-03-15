@echo off
setlocal
cd /d %~dp0
if not exist control mkdir control
python launcher.py
