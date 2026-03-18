@echo off
set /p subject="Enter category (Images, Documents, Videos, Code, Installers, Archives): "
start "" "C:\Program Files\Everything\Everything.exe" -search "path:%USERPROFILE%\Downloads\%subject%"