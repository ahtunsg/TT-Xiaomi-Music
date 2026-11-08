@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"

if exist "%ROOT%\secrets.bat" call "%ROOT%\secrets.bat"
set "SSL_CERT_FILE=%ROOT%\.venv\Lib\site-packages\certifi\cacert.pem"
if not exist "%SSL_CERT_FILE%" (
  echo Missing CA bundle: %SSL_CERT_FILE%
  exit /b 1
)
if "%MI_USER%"=="" (
  echo MI_USER is empty. Fill it in secrets.bat first.
  exit /b 1
)
if "%MI_PASS%"=="" (
  echo MI_PASS is empty. Fill it in secrets.bat first.
  exit /b 1
)

echo Logging in to the Xiaomi account. A verification code may be requested.
echo Listing speakers bound to this account:
echo.
"%ROOT%\.venv\Scripts\miservice.exe" mina
echo.
if exist "%USERPROFILE%\.mi.token" (
  echo Login OK. Token saved at %USERPROFILE%\.mi.token
  echo You can now clear MI_USER and MI_PASS in secrets.bat.
) else (
  echo No token was created. Check the account, password, and code prompt above.
)
