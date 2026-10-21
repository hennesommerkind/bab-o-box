"""BAB-O-BOX Light – Startpunkt. Keine Ninox-Anbindung: jedes Dokument wird im Reiter „Prüfen“ von Hand freigegeben."""
import bab_edition

bab_edition.EDITION = "light"

import bab_gui  # noqa: E402

if __name__ == "__main__":
    bab_gui.main()
