"""Entfernt aus den gebauten Programmordnern, was nicht gebraucht wird:
- Tcl/Tk-Sprachdateien (msgs), Zeitzonen (tzdata) und fremde Zeichensätze
- Tesseract: Trainingsprogramme, Java-Dateien und alle DLLs, die tesseract.exe nicht braucht
Aufruf: python tools/verschlanken.py dist/BAB-O-BOX-Pro dist/BAB-O-BOX-Light"""
import shutil
import sys
from pathlib import Path

import pefile

ENCODINGS_BEHALTEN = {"ascii", "cp1252", "cp437", "cp850", "iso8859-1", "iso8859-15", "utf-8", "unicode", "identity"}


def dll_huelle(ordner: Path, start: str) -> set[str]:
    vorhanden = {p.name.lower(): p for p in ordner.glob("*.dll")}
    noetig, offen = set(), [start.lower()]
    while offen:
        name = offen.pop()
        pfad = vorhanden.get(name) or (ordner / name if (ordner / name).exists() else None)
        if name in noetig or pfad is None:
            continue
        noetig.add(name)
        pe = pefile.PE(str(pfad), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for eintrag in getattr(pe, attr, []):
                offen.append(eintrag.dll.decode(errors="ignore").lower())
        pe.close()
    return noetig


def groesse(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def verschlanken(basis: Path) -> None:
    vorher = groesse(basis)
    intern = basis / "_internal"
    for d in (intern / "_tcl_data" / "tzdata", intern / "_tcl_data" / "msgs"):
        shutil.rmtree(d, ignore_errors=True)
    for f in (intern / "_tk_data" / "msgs").glob("*.msg"):
        if f.stem not in ("de", "en"):
            f.unlink()
    for f in (intern / "_tcl_data" / "encoding").glob("*.enc"):
        if f.stem.lower() not in ENCODINGS_BEHALTEN:
            f.unlink()

    dnd = intern / "tkinterdnd2" / "tkdnd"
    if dnd.is_dir():  # tkdnd bringt Bibliotheken für alle Systeme mit – nur Windows 64 Bit behalten
        for d in dnd.iterdir():
            if d.is_dir() and d.name != "win-x64":
                shutil.rmtree(d, ignore_errors=True)

    tess = basis / "tesseract"
    if (tess / "tesseract.exe").exists():
        noetig = dll_huelle(tess, "tesseract.exe")
        for f in list(tess.iterdir()):
            if f.is_file() and f.suffix.lower() in (".dll", ".exe") and f.name.lower() not in noetig:
                f.unlink()
        for f in (tess / "tessdata").rglob("*.jar"):
            f.unlink()
        for f in (tess / "tessdata").glob("*.traineddata"):
            if f.stem != "deu":
                f.unlink()
        shutil.rmtree(tess / "doc", ignore_errors=True)
        print(f"{basis.name}: Tesseract braucht {len(noetig) - 1} DLLs")
    print(f"{basis.name}: {vorher / 1e6:.1f} MB -> {groesse(basis) / 1e6:.1f} MB")


if __name__ == "__main__":
    for a in sys.argv[1:]:
        verschlanken(Path(a))
