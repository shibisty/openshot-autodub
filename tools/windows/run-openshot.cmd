@echo off
rem Run openshot-qt from source with MSYS2 UCRT64 Python + libopenshot
rem (built by build-libopenshot-msys2.sh). Extra args go to launch.py.
rem   run-openshot.cmd --debug     verbose log
rem   run-openshot.cmd --trace     capture Python tracebacks into openshot-crash.txt
rem Set OPENSHOT_QT_DIR if openshot-qt is not next to the openshot-autodub folder.
setlocal
if "%MSYS2_ROOT%"=="" set "MSYS2_ROOT=C:\msys64"
if "%OPENSHOT_QT_DIR%"=="" set "OPENSHOT_QT_DIR=%~dp0..\..\..\openshot-qt"
for %%I in ("%OPENSHOT_QT_DIR%") do set "OPENSHOT_QT_DIR=%%~fI"
set "MINGW=%MSYS2_ROOT%\ucrt64"
if not exist "%MINGW%\bin\python.exe" (
  echo %MINGW%\bin\python.exe not found - run build-libopenshot-msys2.sh in the MSYS2 UCRT64 shell first.
  pause
  exit /b 1
)
if not exist "%OPENSHOT_QT_DIR%\src\launch.py" (
  echo openshot-qt not found at %OPENSHOT_QT_DIR% - set OPENSHOT_QT_DIR.
  pause
  exit /b 1
)
rem UCRT64 DLLs first, so other Qt/FFmpeg copies on PATH are not picked up
set "PATH=%MINGW%\bin;%PATH%"
set "QT_PLUGIN_PATH=%MINGW%\share\qt6\plugins"
set "OPENSHOT_QT_API=pyqt6"
set "PYTHONUTF8=1"
set "PYTHONHOME="
set "PYTHONPATH="
cd /d "%OPENSHOT_QT_DIR%"
if /i "%~1"=="--trace" (
  "%MINGW%\bin\python.exe" -X faulthandler "%~dp0debug-openshot.py" %2 %3 %4 %5 %6 %7 %8 %9
) else (
  "%MINGW%\bin\python.exe" src\launch.py %*
)
if errorlevel 1 pause
