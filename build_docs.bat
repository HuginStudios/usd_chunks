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
set "DEST=%~2"

REM --no-directory-urls emits example.html rather than example\index.html. A browser
REM opening the site from a UNC path or file:// will not resolve a directory to its
REM index.html, so the default directory URLs break navigation on a fileshare.
call python -m mkdocs build --no-directory-urls -d "%~dp0site"
if errorlevel 1 (
    echo ERROR: mkdocs build failed. Nothing was copied.
    exit /b 1
)

REM Build locally and mirror, rather than pointing -d at the share: a network hiccup
REM mid-build would otherwise leave a half-written site in place. /MIR also prunes pages
REM that have been deleted from the docs.
robocopy "%~dp0site" "%DEST%" /MIR /NFL /NDL /NJH /NP
REM robocopy reports 0-7 for success (1 just means files were copied). 8 and above are
REM real failures, so anything below 8 must not be treated as an error.
if errorlevel 8 (
    echo ERROR: robocopy failed writing to %DEST%
    exit /b 1
)

echo.
echo Published to %DEST%
goto :end

:unknown
echo ERROR: unknown argument "%~1"
echo.

:usage
echo Usage:
echo   build_docs.bat                   Serve the docs locally on http://127.0.0.1:8000
echo   build_docs.bat --share ^<path^>    Build and mirror the site to a fileshare
echo.
echo Note: browser search does not work when the site is opened over file://,
echo because the search index is fetched by XHR. Serving the share over http fixes it.
exit /b 1

:end
endlocal
