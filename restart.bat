@echo off
setlocal EnableExtensions EnableDelayedExpansion
title doubao2api restart

:: ============================================================
:: doubao2api one-click restart (Python + Playwright)
:: Port:   read from .env (DOUBAO_PORT, default 9090)
:: Env:    .venv\Scripts\python.exe  (auto-created via install.bat)
:: Flow:   stop old -> check venv -> start in background -> wait /health
::
:: Usage:
::   restart.bat          normal restart
::   restart.bat stop     stop the running instance only
::
:: Port policy (local machine runs many projects):
::   - port free                -> start
::   - port used by doubao2api  -> kill it, then start (this script's job)
::   - port used by OTHER app   -> warn + show owner cmdline, DO NOT kill,
::                                  exit 1 (use DOUBAO_PORT to pick another)
::
:: Keep this file ASCII-only: cmd misparses UTF-8 on a GBK codepage.
:: Chinese output comes from the Python server itself, not from here.
:: ============================================================

set "PROJECT_DIR=%~dp0"
set "LOG_DIR=%PROJECT_DIR%logs"
set "LOGFILE=%LOG_DIR%\server.log"
set "ERRFILE=%LOG_DIR%\server.err.log"

set "STOP_ONLY=0"
if /i "%~1"=="stop" set "STOP_ONLY=1"

:: ---------- read DOUBAO_PORT / DOUBAO_HOST / DOUBAO_POOL_SIZE from .env ----------
set "PORT=9090"
set "HOST_BIND=127.0.0.1"
set "POOL_SIZE="
if exist "%PROJECT_DIR%.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%PROJECT_DIR%.env") do (
        set "K=%%a"
        set "V=%%b"
        rem strip inline comment (" # ...") and surrounding spaces
        for /f "tokens=1 delims=#" %%c in ("!V!") do set "V=%%c"
        set "V=!V: =!"
        if /i "!K!"=="DOUBAO_PORT"       set "PORT=!V!"
        if /i "!K!"=="DOUBAO_HOST"       set "HOST_BIND=!V!"
        if /i "!K!"=="DOUBAO_POOL_SIZE"  set "POOL_SIZE=!V!"
    )
)
set "PORT=!PORT: =!"

:: ---------- venv must exist (install.bat creates it) ----------
if not exist "%PROJECT_DIR%.venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run install.bat first.
    goto :fail
)
set "PYEXE=%PROJECT_DIR%.venv\Scripts\python.exe"

echo.
echo ========================================
echo   doubao2api  Restart
echo   Port: %PORT%   Pool: %POOL_SIZE%
echo ========================================
echo.

echo [1/5] Checking port %PORT% ...
set "PID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    set "PID=%%a"
    goto :found_pid
)
:found_pid

if "%PID%"=="" (
    echo       Port is free.
    if "%STOP_ONLY%"=="1" goto :nothing_to_stop
    goto :do_start
)

:: ---------- identify the owner of the port ----------
echo       Port is used by PID %PID%. Validating process ...
set "IS_OURS=0"
set "CMDLINE="
set "CHAIN="
rem -- single-line join so multi-line command lines survive the for /f capture;
rem -- also walk up to 3 parents: uvicorn re-execs, so the listener is often a
rem -- system-python child of OUR venv parent. Ours = doubao2api keyword in
rem -- the process chain OR an ancestor whose command line mentions this .venv.
for /f "usebackq delims=" %%b in (`powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter 'ProcessId=%PID%'; $lines = @(); for ($i=0; $i -lt 4 -and $p; $i++) { $cl = $p.CommandLine; if ($cl) { $lines += $cl -replace \"[\r\n]+\",\" \" }; $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $p.ParentProcessId) }; $lines -join ' || '" 2^>nul`) do set "CMDLINE=%%b"

if not defined CMDLINE (
    echo       [WARN] Cannot read the command line of PID %PID% - treating it as a foreign program.
    goto :conflict
)

rem ours = chain mentions "python -m doubao2api" anywhere
echo !CMDLINE! | findstr /i /c:"-m doubao2api" >nul 2>&1 && set "IS_OURS=1"

rem ours = chain mentions this project's venv python
if "!IS_OURS!"=="0" (
    echo !CMDLINE! | findstr /i /c:"%PROJECT_DIR%.venv" >nul 2>&1 && set "IS_OURS=1"
)

if "!IS_OURS!"=="0" goto :conflict
goto :kill_ours

:conflict
echo.
echo ================================================ [CONFLICT] ================
echo   Port %PORT% is occupied by ANOTHER program (PID %PID%).
echo   It does not look like a doubao2api instance, so it was NOT killed.
echo.
echo   CommandLine: !CMDLINE!
echo.
echo   Options:
echo     1. Edit .env  DOUBAO_PORT=[free port]  and run restart.bat again
echo     2. Stop that program yourself, then run restart.bat
echo ========================================================================
echo.
goto :fail

:kill_ours
echo       Confirmed doubao2api instance. Killing PID %PID% ...
taskkill /PID %PID% /T /F >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Failed to kill PID=%PID%. Try running as Administrator,
    echo         or run repair.bat for full residue cleanup.
    goto :fail
)

rem ---------- wait until the port is actually released ----------
set /a WAIT=0
:wait_port
timeout /t 1 /nobreak >nul 2>&1
set "PORT_FREE=1"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING" 2^>nul') do set "PORT_FREE=0"
if "!PORT_FREE!"=="1" goto :port_released
set /a WAIT+=1
if !WAIT! LSS 10 goto :wait_port
echo [WARNING] Port %PORT% not released within 10s. Continuing anyway.
:port_released

if "%STOP_ONLY%"=="1" (
    echo       Stopped.
    goto :done
)

:do_start
echo [2/5] Environment OK ^(venv ready^).
echo [3/5] Starting new instance in background ...
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1

rem Kill window-title chrome zombies from previous runs (repair.bat logic,
rem keeps orphaned Playwright windows from holding the profile lock)
taskkill /f /im "chrome.exe"  /fi "WINDOWTITLE eq *doubao*" >nul 2>nul
taskkill /f /im "msedge.exe" /fi "WINDOWTITLE eq *doubao*" >nul 2>nul

rem Launch fully detached; stdout -> logs\server.log, stderr -> server.err.log
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath '%PYEXE%' -ArgumentList '-m','doubao2api' -WorkingDirectory '%PROJECT_DIR%' -WindowStyle Hidden -RedirectStandardOutput '%LOGFILE%' -RedirectStandardError '%ERRFILE%'"
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Failed to launch the service.
    goto :fail
)
echo       Log: %LOGFILE%

echo [4/5] Waiting for http://127.0.0.1:%PORT%/health/live ...
set /a WAITED=0
:check_start
timeout /t 2 /nobreak >nul 2>&1
curl.exe -s -f -o nul --max-time 3 http://127.0.0.1:%PORT%/health/live
if !ERRORLEVEL! EQU 0 goto :started_ok
set /a WAITED+=1
if !WAITED! LSS 45 goto :check_start

echo [WARNING] Service did not answer /health/live within 90s.
echo           Browser startup can be slow on first run; check the log:
echo           %LOGFILE%
goto :fail

:started_ok
echo [5/5] Service is up.
set "READY="
for /f "usebackq delims=" %%r in (`curl.exe -s --max-time 5 http://127.0.0.1:%PORT%/health`) do set "READY=%%r"

echo.
echo ========================================
echo   doubao2api is running
echo   Base URL : http://127.0.0.1:%PORT%
echo   Admin UI : http://127.0.0.1:%PORT%/admin
echo   Log file : %LOGFILE%
echo   Health   : !READY!
echo   Stop     : restart.bat stop
echo ========================================
echo.

echo !READY! | findstr /i /c:"not_ready" >nul 2>&1 && (
    echo [NOTE] API server is alive, but the browser session is not logged in.
    echo        Open the Admin UI above to scan the QR code or import cookies.
    echo.
)
goto :done

:nothing_to_stop
echo       Nothing to stop on port %PORT%.
goto :done

:done
echo Done.
echo.
pause
endlocal
exit /b 0

:fail
echo.
pause
endlocal
exit /b 1
