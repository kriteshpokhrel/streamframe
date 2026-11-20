@echo off
setlocal
pushd "%~dp0" || exit /b 1
wsl.exe --exec bash scripts/start-wsl.sh check || goto :failed
if not exist "frontend\node_modules\.bin\vite.cmd" (
    call npm.cmd --prefix frontend ci || goto :failed
)
call npm.cmd --prefix frontend run build || goto :failed
wsl.exe --exec bash scripts/start-wsl.sh
set "result=%ERRORLEVEL%"
popd
exit /b %result%

:failed
popd
exit /b 1
