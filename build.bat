@echo off
rem ============================================================
rem  Baut BAB-O-BOX Pro und Light  (einmalig auf einem Windows-PC)
rem  Voraussetzungen: Python 3.11+ und Tesseract (UB Mannheim,
rem  mit deutschem Sprachpaket) – siehe README
rem ============================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul || (echo Python wurde nicht gefunden. Bitte von python.org installieren. & pause & exit /b 1)

if not exist .venv (
  echo Erstelle Python-Umgebung ...
  python -m venv .venv || (pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller || (pause & exit /b 1)

call :baue BAB-O-BOX-Pro bab_gui.py "deu eng osd" LIESMICH_ANWENDER.md || (pause & exit /b 1)
call :baue BAB-O-BOX-Light bab_light.py "deu" LIESMICH_LIGHT.md || (pause & exit /b 1)

echo.
echo Fertig:  dist\BAB-O-BOX-Pro\BAB-O-BOX-Pro.exe  und  dist\BAB-O-BOX-Light\BAB-O-BOX-Light.exe
echo Jeweils den ganzen Ordner weitergeben (z. B. als ZIP).
pause
exit /b 0

:baue
echo Baue %1 ...
pyinstaller --noconfirm --clean --windowed --name "%1" ^
  --add-data "config_standard.toml;." --add-data "regeln_alt;regeln_alt" ^
  --collect-all pypdfium2 --collect-all pypdfium2_raw ^
  %2 || exit /b 1
set "TESS=C:\Program Files\Tesseract-OCR"
set "ZIEL=dist\%1\tesseract"
if exist "%TESS%\tesseract.exe" (
  robocopy "%TESS%" "%ZIEL%" /E /XF *.traineddata /XD doc /NFL /NDL /NJH /NJS /NP >nul
  for %%L in (%~3) do if exist "%TESS%\tessdata\%%L.traineddata" copy /Y "%TESS%\tessdata\%%L.traineddata" "%ZIEL%\tessdata\" >nul
  if not exist "%ZIEL%\tessdata\deu.traineddata" echo WARNUNG: deu.traineddata fehlt - Tesseract mit deutschem Sprachpaket neu installieren!
) else (
  echo WARNUNG: Tesseract nicht unter "%TESS%" gefunden.
)
copy /Y %4 "dist\%1\" >nul
exit /b 0
