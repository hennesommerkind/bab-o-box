@echo off
rem ============================================================
rem  Baut BAB-O-BOX.exe  (einmalig auf einem Windows-PC)
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

echo Baue Programm ...
pyinstaller --noconfirm --clean --windowed --name "BAB-O-BOX" ^
  --add-data "config_standard.toml;." --add-data "regeln_alt;regeln_alt" ^
  --collect-all pypdfium2 --collect-all pypdfium2_raw ^
  bab_gui.py || (pause & exit /b 1)

set "TESS=C:\Program Files\Tesseract-OCR"
set "ZIEL=dist\BAB-O-BOX\tesseract"
if exist "%TESS%\tesseract.exe" (
  echo Packe Tesseract dazu ...
  robocopy "%TESS%" "%ZIEL%" /E /XF *.traineddata /XD doc /NFL /NDL /NJH /NJS /NP >nul
  for %%L in (deu eng osd) do if exist "%TESS%\tessdata\%%L.traineddata" copy /Y "%TESS%\tessdata\%%L.traineddata" "%ZIEL%\tessdata\" >nul
  if not exist "%ZIEL%\tessdata\deu.traineddata" echo WARNUNG: deu.traineddata fehlt - Tesseract mit deutschem Sprachpaket neu installieren!
) else (
  echo WARNUNG: Tesseract nicht unter "%TESS%" gefunden - das Programm laeuft dann nur,
  echo          wenn Tesseract auf dem Ziel-PC installiert ist.
)

copy /Y LIESMICH_ANWENDER.md "dist\BAB-O-BOX\" >nul

echo.
echo Fertig:  dist\BAB-O-BOX\BAB-O-BOX.exe
echo Den ganzen Ordner dist\BAB-O-BOX weitergeben (z. B. als ZIP).
pause
