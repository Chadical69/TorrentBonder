@echo off
setlocal
cd /d "%~dp0"

echo ======================================
echo  TorrentBond - aria2 Edition
echo ======================================

where aria2c.exe >nul 2>nul
if errorlevel 1 (
    where winget >nul 2>nul
    if errorlevel 1 (
        echo aria2 was not found and winget is unavailable.
        echo Install aria2 manually, then run this file again.
        echo Official releases: https://github.com/aria2/aria2/releases
        pause
        exit /b 1
    )
    echo aria2 was not found. Trying to install it with winget...
    winget install --id aria2.aria2 --exact --accept-source-agreements --accept-package-agreements
    echo.
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 torrent_bonder.py
) else (
    python torrent_bonder.py
)
if errorlevel 1 (
    echo.
    echo TorrentBond exited with an error. See the message above.
    pause
)
endlocal
