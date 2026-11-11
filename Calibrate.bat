@echo off
rem Opens the full-screen calibration editor. Esc saves and starts the overlay.
start "" pythonw "%~dp0burnfix.py" calibrate
