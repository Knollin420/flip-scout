@echo off
REM flip-scout fuer Windows: startet den Bot und startet ihn nach einem Absturz neu.
REM Fenster offen lassen (minimieren ist okay). Beenden: Fenster schliessen.
title flip-scout
cd /d "%~dp0\..\.."
if not exist data mkdir data
:loop
.venv\Scripts\python.exe -m flip_scout --log-file data\flip-scout.log
if %errorlevel%==3 goto end
if %errorlevel%==2 goto end
echo.
echo flip-scout wurde beendet - Neustart in 30 Sekunden (Strg+C zum Abbrechen)
timeout /t 30 /nobreak
goto loop
:end
pause
