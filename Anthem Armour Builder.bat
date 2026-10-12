@echo off
rem Starts the Anthem Armour Builder window. Installs Python first if it is missing;
rem the builder installs its own Python packages and finds Anthem and Bforartists/Blender itself.
set BUILDER=%~dp0anthem_builder.py

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%BUILDER%"
    exit /b
)
where python >nul 2>nul
if %errorlevel%==0 (
    python "%BUILDER%"
    exit /b
)
echo Python was not found. Installing Python 3.13 with winget ...
winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
echo Python installed. Run this file again.
pause
