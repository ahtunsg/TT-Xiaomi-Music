@echo off
rem Headless entry point: file server first, then xiaogpt in the foreground.
set "ROOT=%~dp0.."
cd /d "%ROOT%"
if exist "%ROOT%\music_env.bat" call "%ROOT%\music_env.bat"
if not exist "%MUSIC_DIR%" mkdir "%MUSIC_DIR%"
start "" /b "%~dp0start_music.bat"
call "%~dp0start_xiaogpt.bat" >> "%ROOT%\xiaogpt.log" 2>&1
