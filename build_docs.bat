@echo off
setlocal

REM initusd is a local environment script, so only call it when it is actually present --
REM a contributor working from a plain pip install will not have one.
where initusd >nul 2>nul && call initusd

REM mkdocstrings introspects the package, and griffe_chunks.py walks the chunk modules,
REM so the build needs the same discovery path a normal session has.
if not defined USD_CHUNK_DIRS set "USD_CHUNK_DIRS=%~dp0scripts"

if /I "%~1"=="--share" goto :share
if /I "%~1"=="-h" goto :usage
if /I "%~1"=="--help" goto :usage
if not "%~1"=="" goto :unknown

call python -m mkdocs serve
goto :end

:share
if "%~2"=="" (
    echo ERROR: --share needs a destination path.
    echo.
    goto :usage
)

REM Resolve to a full path before anything else, so the guards below cannot be sidestepped
REM by a relative path such as "." and so the messages name a real location.
for %%I in ("%~2") do set "DEST=%%~fI"

set "URLSTYLE=--no-directory-urls"
set "PURGE="
call :readflag "%~3" || goto :usage
call :readflag "%~4" || goto :usage

REM Refuse to publish into a source checkout. `--share .` once mirrored the built site over
REM this repository and deleted it, working tree and .git alike. A docs destination never
REM contains any of these.
if exist "%DEST%\.git" goto :refuse
if exist "%DEST%\build_docs.bat" goto :refuse
if exist "%DEST%\pyproject.toml" goto :refuse

REM Deletion is opt-in. Without --mirror this only adds and overwrites, so pointing at the
REM wrong directory costs you a stray copy of the docs rather than its contents. Checked
REM before building, so a destination we will refuse fails immediately.
if defined PURGE (
    REM Only purge somewhere we have published before, identified by the marker written at
    REM the end of a successful run, or somewhere empty.
    if not exist "%DEST%\.usdchunks-docs" (
        dir /b /a "%DEST%" 2>nul | findstr "^" >nul && goto :refusepurge
    )
)

call python -m mkdocs build %URLSTYLE% -d "%~dp0site"
if errorlevel 1 (
    echo ERROR: mkdocs build failed. Nothing was copied.
    exit /b 1
)

REM Build locally and copy, rather than pointing -d at the destination: a network hiccup
REM mid-build would otherwise leave a half-written site in place.
robocopy "%~dp0site" "%DEST%" /E %PURGE% /NFL /NDL /NJH /NP
REM robocopy reports 0-7 for success (1 just means files were copied). 8 and above are real
REM failures, so anything below 8 must not be treated as an error.
if errorlevel 8 (
    echo ERROR: robocopy failed writing to %DEST%
    exit /b 1
)

REM Marks the directory as ours, which is what makes a later --mirror safe.
echo Published by usd_chunks build_docs.bat. Safe to delete.> "%DEST%\.usdchunks-docs"

echo.
echo Published to %DEST%
goto :end

:readflag
if "%~1"=="" exit /b 0
if /I "%~1"=="--directory-urls" set "URLSTYLE=" & exit /b 0
if /I "%~1"=="--mirror" set "PURGE=/PURGE" & exit /b 0
echo ERROR: unknown argument "%~1"
echo.
exit /b 1

:refuse
echo ERROR: %DEST%
echo        looks like a source checkout, not a docs destination.
echo        Refusing: publishing into it would scatter the built site over your
echo        working tree, and with --mirror would delete it.
exit /b 1

:refusepurge
echo ERROR: %DEST%
echo        is not empty and was not published by this script, so --mirror will not
echo        delete its contents. Point somewhere empty, or clear it yourself first.
exit /b 1

:unknown
echo ERROR: unknown argument "%~1"
echo.

:usage
echo Usage:
echo   build_docs.bat
echo       Serve the docs locally on http://127.0.0.1:8000
echo.
echo   build_docs.bat --share ^<path^> [--directory-urls] [--mirror]
echo       Build the site and copy it to ^<path^> -- a UNC share, a mapped drive,
echo       or a web root. Adds and overwrites only.
echo.
echo       --directory-urls  Use /example/ style URLs instead of example.html.
echo                         Only for a destination served by a web server;
echo                         these break when opened over file://.
echo       --mirror          Also delete files at the destination that are no
echo                         longer part of the site. Refused unless the
echo                         destination is empty or was published by this script.
echo.
echo Note: the search box needs the site to be served over http, because the
echo search index is fetched by XHR. Everything else works either way.
exit /b 1

:end
endlocal
