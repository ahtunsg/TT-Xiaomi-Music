@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"

if exist "%ROOT%\secrets.bat" call "%ROOT%\secrets.bat"
if exist "%ROOT%\music_env.bat" call "%ROOT%\music_env.bat"

if "%REPUBLIC_OPENAI_API_KEY%"=="" (
  echo Missing REPUBLIC_OPENAI_API_KEY. Copy examples\secrets.bat.example to secrets.bat.
  exit /b 1
)

rem Managed Python builds often have no system CA bundle; point OpenSSL at certifi.
set "SSL_CERT_FILE=%ROOT%\.venv\Lib\site-packages\certifi\cacert.pem"
if not exist "%SSL_CERT_FILE%" (
  echo Missing CA bundle: %SSL_CERT_FILE%
  exit /b 1
)

rem Let the patched xiaogpt import music_bridge when started via xiaogpt.exe.
set "PYTHONPATH=%ROOT%"

rem Latency patches. See README.md.
set "XIAOGPT_NO_HINT=1"
set "XIAOGPT_POLL_INTERVAL=0.4"
set "XIAOGPT_FAST_STOP=1"
set "XIAOGPT_SKIP_PLAY_CHECK=1"

rem Anything said after this word goes to the local library instead of the LLM.
set "XIAOGPT_MUSIC_PREFIX=小爱音乐"

if not exist "%USERPROFILE%\.mi.token" (
  if "%MI_USER%"=="" (
    echo First login needs MI_USER and MI_PASS in secrets.bat. Run first_login.bat.
    exit /b 1
  )
  if "%MI_PASS%"=="" (
    echo First login needs MI_USER and MI_PASS in secrets.bat. Run first_login.bat.
    exit /b 1
  )
)

rem Idempotent patch. Re-apply after any xiaogpt upgrade.
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\patch_xiaogpt.py" >nul 2>&1

echo Starting xiaogpt with local music support.
echo Keep this window open.
"%ROOT%\.venv\Scripts\xiaogpt.exe" --config "%ROOT%\xiao_config.yaml" -v
