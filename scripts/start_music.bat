@echo off
rem Local file server. The speaker fetches audio from MUSIC_PORT (18899).
set "ROOT=%~dp0.."
cd /d "%ROOT%"
if exist "%ROOT%\music_env.bat" call "%ROOT%\music_env.bat"
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\music_server.py" >> "%ROOT%\music_server.log" 2>&1
