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

REM Default to flat filenames (example.html rather than example\index.html), because that
REM is the portable option: a browser opening the site over a UNC path or file:// will not
REM resolve a directory to its index.html, and a web server serves flat files perfectly
REM well. Pass --directory-urls for the tidier /example/ style once the destination is
REM definitely behind a web server such as nginx.
set "URLSTYLE=--no-directory-urls"
if not "%~3"=="" (
    if /I "%~3"=="--directory-urls" (
        set "URLSTYLE="
    ) else (
        echo ERROR: unknown argument "%~3"
        echo.
        goto :usage
    )
)

call python -m mkdocs build %URLSTYLE% -d "%~dp0site"
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
echo   build_docs.bat
echo       Serve the docs locally on http://127.0.0.1:8000
echo.
echo   build_docs.bat --share ^<path^>
echo       Build and mirror the site to ^<path^>. Any destination robocopy can
echo       write to: a UNC share, a mapped drive, or an nginx web root.
echo       Uses flat filenames, which work both over file:// and over http.
echo.
echo   build_docs.bat --share ^<path^> --directory-urls
echo       As above but with /example/ style URLs. Only use this when the
echo       destination is served by a web server -- these break over file://.
echo.
echo Note: the search box needs the site to be served over http, because the
echo search index is fetched by XHR. Everything else works either way.
exit /b 1

:end
endlocal
