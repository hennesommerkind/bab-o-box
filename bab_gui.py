"""
BAB-O-BOX – Windows-Oberfläche.

Start:  BAB-O-BOX.exe   (oder  python bab_gui.py)
        --minimiert       startet verkleinert (für den Windows-Autostart)
"""
from __future__ import annotations

import copy
import logging
import os
import queue
import sys
import threading
import traceback
from dataclasses import asdict
from pathlib import Path



def windows_meldung(text: str) -> None:
    """Meldung ohne Tk (funktioniert auch, wenn Tk fehlt)."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "BAB-O-BOX", 0x10)
    except Exception:
        print(text, file=sys.stderr)


def vollstaendig_entpackt() -> bool:
    """Prüft, ob die mitgelieferten Dateien da sind (häufigster Fehler: ZIP nicht fertig entpackt)."""
    if not getattr(sys, "frozen", False):
        return True
    basis = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    fehlend = [n for n in ("_tcl_data", "_tk_data", "config_standard.toml") if not (basis / n).exists()]
    if fehlend:
        windows_meldung(
            "Das Programm ist nicht vollständig entpackt – es fehlen Dateien:\n  " + ", ".join(fehlend)
            + f"\n\nOrdner: {basis.parent}\n\n"
            "So geht's: die ZIP-Datei mit Rechtsklick → „Alle extrahieren …“ vollständig entpacken, warten, "
            "bis Windows fertig ist, und dann BAB-O-BOX.exe im entpackten Ordner starten.\n\n"
            "Falls es danach wieder passiert, hat vermutlich der Virenschutz Dateien entfernt – dann bitte die IT fragen.")
        return False
    return True


if not vollstaendig_entpackt():
    raise SystemExit(1)

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402
from tkinter.scrolledtext import ScrolledText  # noqa: E402


def notfall_protokoll(fehler: str) -> list[str]:
    """Startfehler aufschreiben – auch wenn sonst noch nichts funktioniert."""
    orte = [Path(os.environ.get("APPDATA") or Path.home()) / "BAB-O-BOX" / "absturz.txt"]
    if getattr(sys, "frozen", False):
        orte.append(Path(sys.executable).resolve().parent / "absturz.txt")
    geschrieben = []
    for ort in orte:
        try:
            ort.parent.mkdir(parents=True, exist_ok=True)
            ort.write_text(fehler, encoding="utf-8")
            geschrieben.append(str(ort))
        except Exception:
            pass
    return geschrieben


def notfall_meldung(fehler: str) -> None:
    orte = notfall_protokoll(fehler)
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("BAB-O-BOX", "Das Programm konnte nicht starten.\n\n" + fehler[-1500:]
                             + ("\n\nDetails gespeichert in:\n" + "\n".join(orte) if orte else ""))
        root.destroy()
    except Exception:
        windows_meldung("Das Programm konnte nicht starten.\n\n" + fehler[-1200:]
                        + ("\n\nDetails gespeichert in:\n" + "\n".join(orte) if orte else ""))


try:
    import bab_kern as kern
except Exception:
    notfall_meldung(traceback.format_exc())
    raise SystemExit(1)

log = logging.getLogger("bab")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STATUS_TEXT = {"ok": "abgelegt", "pruefen": "prüfen", "fehler": "Fehler"}


# =========================================================================== kleine Helfer

def iso_zu_de(iso: str | None) -> str:
    if not iso or len(iso) != 10:
        return iso or ""
    j, m, t = iso.split("-")
    return f"{t}.{m}.{j}"


def autostart_aktiv() -> bool:
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, kern.APP_NAME)
            return True
    except OSError:
        return False


def autostart_umziehen() -> None:
    """Nach der Umbenennung: alten Autostart-Eintrag "BAB-Eingang" durch "BAB-O-BOX" ersetzen."""
    if os.name != "nt":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            winreg.QueryValueEx(key, kern.ALTER_APP_NAME)
            winreg.DeleteValue(key, kern.ALTER_APP_NAME)
            autostart_setzen(True)
    except OSError:
        pass


def autostart_setzen(an: bool) -> None:
    if os.name != "nt":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if an:
            if getattr(sys, "frozen", False):
                befehl = f'"{sys.executable}" --minimiert'
            else:
                py = Path(sys.executable).with_name("pythonw.exe")
                befehl = f'"{py if py.exists() else sys.executable}" "{Path(__file__).resolve()}" --minimiert'
            winreg.SetValueEx(key, kern.APP_NAME, 0, winreg.REG_SZ, befehl)
        else:
            try:
                winreg.DeleteValue(key, kern.APP_NAME)
            except OSError:
                pass


class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(("log", self.format(record)))


def text_fenster(eltern, titel: str, inhalt: str) -> None:
    w = tk.Toplevel(eltern)
    w.title(titel)
    w.geometry("760x480")
    t = ScrolledText(w, wrap="word", font=("Consolas", 10))
    t.pack(fill="both", expand=True, padx=8, pady=8)
    t.insert("1.0", inhalt)
    t.configure(state="disabled")
    ttk.Button(w, text="Schließen", command=w.destroy).pack(pady=(0, 8))


# =========================================================================== Hauptfenster

class App(tk.Tk):
    def __init__(self, minimiert: bool = False, selbsttest: bool = False):
        super().__init__()
        self.title("BAB-O-BOX")
        self.geometry("1060x700")
        self.minsize(860, 580)

        autostart_umziehen()
        self.q: queue.Queue = queue.Queue()
        self.k = kern.Konfiguration()
        handler = QueueHandler(self.q)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        kern.logging_einrichten(self.k.cfg, [handler])

        self.v: kern.Verarbeitung | None = None
        self.stopp = threading.Event()
        self.waechter: threading.Thread | None = None
        self.nach_stopp = None
        self.zaehler = {"ok": 0, "pruefen": 0, "fehler": 0}
        self.pruef_pfade: dict[str, Path] = {}
        self.zeilen: dict[str, dict] = {}

        self.var_test = tk.BooleanVar(value=bool(self.k.einstellungen.get("testlauf", True)))

        stil = ttk.Style(self)
        stil.configure("Gross.TLabel", font=("Segoe UI", 13, "bold"))
        stil.configure("Hinweis.TLabel", foreground="#666")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._tab_uebersicht()
        self._tab_pruefen()
        self._tab_analyse()
        self._tab_einstellungen()

        self._verarbeitung_neu()
        self._protokoll_laden()
        self._pruefen_aktualisieren()
        self.after(200, self._queue_abholen)
        self.protocol("WM_DELETE_WINDOW", self._beenden)

        if self.k.migriert and not selbsttest:
            self.after(300, lambda: messagebox.showinfo("Regeldatei aktualisiert", self.k.migriert))
        if self.k.einstellungen["ueberwachung"].get("beim_start_starten"):
            self.after(800, self.start)
        if minimiert:
            self.iconify()

    # ------------------------------------------------------------------ Übersicht
    def _tab_uebersicht(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Übersicht")

        oben = ttk.Frame(f)
        oben.pack(fill="x")
        self.lbl_status = ttk.Label(oben, text="Überwachung gestoppt", style="Gross.TLabel")
        self.lbl_status.pack(side="left")
        self.btn_start = ttk.Button(oben, text="▶ Überwachung starten", command=self._start_stopp)
        self.btn_start.pack(side="right")
        ttk.Button(oben, text="Jetzt verarbeiten", command=self._jetzt).pack(side="right", padx=6)

        zeile = ttk.Frame(f)
        zeile.pack(fill="x", pady=(8, 4))
        ttk.Checkbutton(zeile, text="Testlauf – nichts an Ninox senden", variable=self.var_test,
                        command=self._testlauf_umgeschaltet).pack(side="left")
        for name, key in [("Fehler", "fehler"), ("Prüfen", "pruefen"), ("Verarbeitet", "verarbeitet"), ("Eingang", "eingang")]:
            ttk.Button(zeile, text=f"📂 {name}", command=lambda k=key: self._ordner(k)).pack(side="right", padx=2)

        self.lbl_zaehler = ttk.Label(f, text="", style="Hinweis.TLabel")
        self.lbl_zaehler.pack(anchor="w", pady=(0, 6))

        spalten = ("zeit", "status", "ergebnis", "kategorie", "grund")
        titel = ("Zeit", "Status", "Datei", "Art", "Hinweis")
        breiten = (120, 65, 300, 110, 200)
        teiler = ttk.PanedWindow(f, orient="horizontal")
        teiler.pack(fill="both", expand=True)

        links = ttk.Frame(teiler)
        liste_kopf = ttk.Frame(links)
        liste_kopf.pack(fill="x", pady=(0, 4))
        ttk.Label(liste_kopf, text="Verarbeitete Dokumente").pack(side="left")
        ttk.Button(liste_kopf, text="Liste leeren …", command=self._liste_leeren).pack(side="right")
        ttk.Button(liste_kopf, text="Auswahl entfernen", command=self._auswahl_entfernen).pack(side="right", padx=4)
        rahmen = ttk.Frame(links)
        rahmen.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(rahmen, columns=spalten, show="headings", height=12)
        for sp, t, b in zip(spalten, titel, breiten):
            self.tree.heading(sp, text=t)
            self.tree.column(sp, width=b, anchor="w", stretch=(sp in ("ergebnis", "grund")))
        sb = ttk.Scrollbar(rahmen, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("ok", foreground="#1b7a2f")
        self.tree.tag_configure("pruefen", foreground="#b25e00")
        self.tree.tag_configure("fehler", foreground="#b00020")
        self.tree.bind("<Double-1>", self._eintrag_oeffnen)
        self.tree.bind("<<TreeviewSelect>>", self._details_zeigen)
        self.tree.bind("<Delete>", lambda _e: self._auswahl_entfernen())
        teiler.add(links, weight=3)

        rechts = ttk.LabelFrame(teiler, text="Ausgelesene Angaben", padding=8)
        self.txt_details = tk.Text(rechts, width=42, wrap="word", font=("Segoe UI", 10), relief="flat",
                                   background=self.cget("background"), state="disabled", cursor="arrow")
        self.txt_details.tag_configure("feld", foreground="#666")
        self.txt_details.tag_configure("titel", font=("Segoe UI", 10, "bold"))
        self.txt_details.tag_configure("warn", foreground="#b25e00")
        self.txt_details.pack(fill="both", expand=True)
        ttk.Button(rechts, text="PDF öffnen", command=lambda: self._eintrag_oeffnen(None)).pack(anchor="w", pady=(6, 0))
        teiler.add(rechts, weight=2)
        self._details_schreiben([("", "Ein Dokument in der Liste anklicken.", "feld")])

        ttk.Label(f, text="Protokoll").pack(anchor="w", pady=(8, 2))
        self.txt_log = ScrolledText(f, height=6, font=("Consolas", 9), state="disabled")
        self.txt_log.pack(fill="x")

    def _eintrag_einfuegen(self, z: dict, oben: bool = True):
        iid = self.tree.insert("", 0 if oben else "end", values=(
            z.get("zeit", ""), STATUS_TEXT.get(z.get("status"), z.get("status")), z.get("ergebnis", ""),
            z.get("kategorie", ""), z.get("grund", "") or z.get("hinweise", "")), tags=(z.get("status"),))
        self.zeilen[iid] = z

    def _protokoll_laden(self):
        for z in reversed(kern.protokoll_lesen(Path(self.k.cfg["ordner"]["protokoll"]))):
            self._eintrag_einfuegen(z, oben=False)
        self._zaehler_neu()

    def _zaehler_neu(self):
        heute = kern.datetime.now().strftime("%Y-%m-%d")
        self.zaehler = {"ok": 0, "pruefen": 0, "fehler": 0}
        for z in self.zeilen.values():
            if z.get("zeit", "").startswith(heute) and z.get("status") in self.zaehler:
                self.zaehler[z["status"]] += 1
        self._zaehler_anzeigen()

    def _zaehler_anzeigen(self):
        z = self.zaehler
        modus = "TESTLAUF (ohne Ninox)" if self.var_test.get() else "Live mit Ninox"
        self.lbl_zaehler.configure(
            text=f"Heute: {z['ok']} abgelegt · {z['pruefen']} zu prüfen · {z['fehler']} Fehler      Modus: {modus}")

    # ------------------------------------------------------------------ Details & Liste
    def _details_schreiben(self, teile: list[tuple[str, str, str]]):
        t = self.txt_details
        t.configure(state="normal")
        t.delete("1.0", "end")
        for feld, wert, tag in teile:
            if feld:
                t.insert("end", f"{feld}\n", "feld")
            t.insert("end", f"{wert}\n\n", tag)
        t.configure(state="disabled")

    def _details_zeigen(self, _evt=None):
        sel = self.tree.selection()
        if not sel or sel[0] not in self.zeilen:
            return
        z = self.zeilen[sel[0]]
        de = lambda w: iso_zu_de(w) if w else "–"  # noqa: E731
        teile = [("", f"{STATUS_TEXT.get(z.get('status'), z.get('status'))} · {z.get('ergebnis', '')}", "titel")]
        if z.get("grund"):
            teile.append(("Grund", z["grund"], "warn"))
        if z.get("hinweise") and z.get("hinweise") != z.get("grund"):
            teile.append(("Hinweise", z["hinweise"].replace("; ", "\n"), "warn"))
        teile += [
            ("Name (aus dem Bescheid)", z.get("name") or "–", ""),
            ("Kundennummer", z.get("kundennummer") or "–", ""),
            ("Geburtsdatum", de(z.get("geburtsdatum")), ""),
            ("Haus", z.get("haus") or "–", ""),
            ("Antrag gestellt am", de(z.get("antragsdatum")), ""),
            ("Art", z.get("kategorie") or "–", ""),
            ("Bescheiddatum", de(z.get("bescheiddatum")), ""),
            ("Bewilligt", f"{de(z.get('zeitraum_von'))} – {de(z.get('zeitraum_bis'))}", ""),
            ("BAB monatlich (aktuell)", f"{z['betrag']} €" if z.get("betrag") else "–", ""),
        ]
        if z.get("zeitraeume"):
            teile.append(("Bewilligungsabschnitte", z["zeitraeume"].replace(" | ", "\n"), ""))
        if z.get("notiz"):
            teile.append(("Notiz zum Bescheid", z["notiz"], ""))
        if z.get("antragsart_hinweis"):
            teile.append(("Text deutet auf Antragsart", z["antragsart_hinweis"], ""))
        teile += [("Erkannt durch", {"regeln": "Regeln", "regeln+ki": "Regeln + KI", "manuell": "manuelle Eingabe"}
                   .get(z.get("quelle"), z.get("quelle") or "–"), ""),
                  ("Eingangsdatei · Zeit", f"{z.get('eingang', '')} · {z.get('zeit', '')}", "feld")]
        self._details_schreiben(teile)

    def _auswahl_entfernen(self):
        sel = [i for i in self.tree.selection() if i in self.zeilen]
        if not sel:
            messagebox.showinfo("Entfernen", "Bitte zuerst Einträge in der Liste auswählen (mehrere mit Strg).")
            return
        weg = [self.zeilen[i] for i in sel]
        try:
            kern.protokoll_entfernen(Path(self.k.cfg["ordner"]["protokoll"]), weg)
        except Exception as ex:
            messagebox.showerror("Entfernen", str(ex))
            return
        for i in sel:
            self.tree.delete(i)
            self.zeilen.pop(i, None)
        self._zaehler_neu()
        self._details_schreiben([("", f"{len(sel)} Eintrag/Einträge aus der Liste entfernt.", "feld")])

    def _liste_leeren(self):
        if not self.zeilen:
            return
        if not messagebox.askyesno("Liste leeren", f"Alle {len(self.zeilen)} Einträge aus der Liste entfernen?\n\n"
                                   "Die PDFs in den Ordnern und die Daten in Ninox bleiben unverändert. "
                                   "Die bisherige Liste wird als Sicherung im Datenordner aufbewahrt."):
            return
        try:
            kern.protokoll_leeren(Path(self.k.cfg["ordner"]["protokoll"]))
        except Exception as ex:
            messagebox.showerror("Liste leeren", str(ex))
            return
        self.tree.delete(*self.tree.get_children())
        self.zeilen.clear()
        self._zaehler_neu()
        self._details_schreiben([("", "Liste geleert.", "feld")])

    def _eintrag_oeffnen(self, _evt):
        sel = self.tree.selection()
        if not sel or sel[0] not in self.zeilen:
            return
        z = self.zeilen[sel[0]]
        ordner = {"ok": "verarbeitet", "pruefen": "pruefen", "fehler": "fehler"}.get(z.get("status"), "verarbeitet")
        pfad = Path(self.k.cfg["ordner"][ordner]) / z.get("ergebnis", "")
        if pfad.is_file():
            kern.ordner_oeffnen(pfad)
        else:
            messagebox.showinfo("Nicht gefunden", f"Die Datei liegt nicht mehr dort:\n{pfad}")

    def _ordner(self, key: str):
        p = Path(self.k.cfg["ordner"][key])
        p.mkdir(parents=True, exist_ok=True)
        kern.ordner_oeffnen(p)

    # ------------------------------------------------------------------ Prüfen
    def _tab_pruefen(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Prüfen")
        self.tab_pruefen = f

        links = ttk.Frame(f)
        links.pack(side="left", fill="both", expand=True)
        kopf = ttk.Frame(links)
        kopf.pack(fill="x")
        ttk.Label(kopf, text="Dokumente, die nicht eindeutig erkannt wurden").pack(side="left")
        ttk.Button(kopf, text="Aktualisieren", command=self._pruefen_aktualisieren).pack(side="right")
        self.tree_p = ttk.Treeview(links, columns=("datei", "grund"), show="headings")
        self.tree_p.heading("datei", text="Datei")
        self.tree_p.heading("grund", text="Grund")
        self.tree_p.column("datei", width=200)
        self.tree_p.column("grund", width=280)
        self.tree_p.pack(fill="both", expand=True, pady=(6, 0))
        self.tree_p.bind("<<TreeviewSelect>>", self._pruefen_auswahl)
        self.tree_p.bind("<Double-1>", lambda _e: self._pruefen_pdf())

        rechts = ttk.LabelFrame(f, text="Angaben korrigieren", padding=10)
        rechts.pack(side="right", fill="y", padx=(10, 0))
        self.lbl_grund = ttk.Label(rechts, text="Links ein Dokument auswählen.", wraplength=300, style="Hinweis.TLabel")
        self.lbl_grund.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.p_vars: dict[str, tk.StringVar] = {}
        felder = [("name", "Name (nur Dateiname)"), ("kundennummer", "Kundennummer"), ("geburtsdatum", "Geburtsdatum"),
                  ("antragsdatum", "Antrag gestellt am"), ("kategorie", "Kategorie"),
                  ("bescheiddatum", "Bescheiddatum"), ("zeitraum_von", "Bewilligt von"), ("zeitraum_bis", "Bewilligt bis"),
                  ("betrag", "Betrag mtl. (€)")]
        for i, (key, text) in enumerate(felder, start=1):
            ttk.Label(rechts, text=text).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            self.p_vars[key] = var
            if key == "kategorie":
                self.cmb_kat = ttk.Combobox(rechts, textvariable=var, state="readonly", width=24)
                self.cmb_kat.grid(row=i, column=1, sticky="ew", pady=3)
            else:
                ttk.Entry(rechts, textvariable=var, width=26).grid(row=i, column=1, sticky="ew", pady=3)
        ttk.Label(rechts, text="Datum als TT.MM.JJJJ, Betrag z. B. 812,00", style="Hinweis.TLabel").grid(
            row=len(felder) + 1, column=0, columnspan=2, sticky="w", pady=(2, 10))

        knopf = ttk.Frame(rechts)
        knopf.grid(row=len(felder) + 2, column=0, columnspan=2, sticky="ew")
        ttk.Button(knopf, text="PDF ansehen", command=self._pruefen_pdf).pack(fill="x", pady=2)
        ttk.Button(knopf, text="✔ Mit diesen Angaben ablegen", command=self._pruefen_uebernehmen).pack(fill="x", pady=2)
        ttk.Button(knopf, text="Erneut automatisch versuchen", command=self._pruefen_erneut).pack(fill="x", pady=2)
        ttk.Label(rechts, text="Tipp: Fehlt die Kundennummer in Ninox, dort nachtragen und\n"
                               "dann „Erneut automatisch versuchen“. Ohne Kundennummer\n"
                               "wird über das Geburtsdatum gesucht.", style="Hinweis.TLabel").grid(
            row=len(felder) + 3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._kategorien_setzen()

    def _kategorien_setzen(self):
        self.cmb_kat.configure(values=[k["name"] for k in self.k.cfg.get("kategorien", [])]
                               + [u["name"] for u in self.k.cfg.get("unterarten", [])])

    def _pruefen_aktualisieren(self):
        self.tree_p.delete(*self.tree_p.get_children())
        self.pruef_pfade.clear()
        faelle = self.v.pruef_faelle() if self.v else []
        for pfad, info in faelle:
            iid = self.tree_p.insert("", "end", values=(pfad.name, info.get("grund", "")))
            self.pruef_pfade[iid] = pfad
        self.nb.tab(self.tab_pruefen, text=f"Prüfen ({len(faelle)})" if faelle else "Prüfen")

    def _pruefen_gewaehlt(self) -> Path | None:
        sel = self.tree_p.selection()
        if not sel:
            messagebox.showinfo("Prüfen", "Bitte zuerst links ein Dokument auswählen.")
            return None
        return self.pruef_pfade.get(sel[0])

    def _pruefen_auswahl(self, _evt=None):
        sel = self.tree_p.selection()
        if not sel:
            return
        pfad = self.pruef_pfade[sel[0]]
        info = {}
        for p, i in (self.v.pruef_faelle() if self.v else []):
            if p == pfad:
                info = i
        self.lbl_grund.configure(text=info.get("grund", "kein Grund gespeichert"))
        erkannt = info.get("erkannt", {})
        for key, var in self.p_vars.items():
            wert = erkannt.get(key) or ""
            var.set(iso_zu_de(wert) if key in ("bescheiddatum", "zeitraum_von", "zeitraum_bis", "geburtsdatum", "antragsdatum") else wert)

    def _pruefen_pdf(self):
        pfad = self._pruefen_gewaehlt()
        if pfad and pfad.exists():
            kern.ordner_oeffnen(pfad)

    def _pruefen_uebernehmen(self):
        pfad = self._pruefen_gewaehlt()
        if not pfad:
            return
        w = {k: v.get().strip() for k, v in self.p_vars.items()}
        vorgaben: dict = {}
        kdnr = w["kundennummer"].replace(" ", "").upper()
        if kdnr and not kern.KDNR_FORMAT.match(kdnr):
            if not messagebox.askyesno("Kundennummer", f"„{kdnr}“ hat nicht das übliche Format 123A456789.\nTrotzdem verwenden?"):
                return
        vorgaben["kundennummer"] = kdnr
        vorgaben["name"] = w["name"]
        vorgaben["kategorie"] = w["kategorie"]
        for key in ("bescheiddatum", "zeitraum_von", "zeitraum_bis", "geburtsdatum", "antragsdatum"):
            if w[key]:
                iso = kern.datum_iso(w[key])
                if not iso:
                    messagebox.showerror("Datum", f"„{w[key]}“ ist kein gültiges Datum (TT.MM.JJJJ).")
                    return
                vorgaben[key] = iso
        if w["betrag"]:
            b = w["betrag"].replace("€", "").replace(" ", "")
            if not kern.BETRAG_FORMAT.match(b):
                messagebox.showerror("Betrag", "Bitte den Betrag im Format 812,00 eingeben.")
                return
            vorgaben["betrag"] = b
        self._hintergrund(lambda: self.v.datei(pfad, vorgaben), self._pruefen_fertig, "Lege ab …")

    def _pruefen_erneut(self):
        pfad = self._pruefen_gewaehlt()
        if pfad:
            self._hintergrund(lambda: self.v.datei(pfad), self._pruefen_fertig, "Verarbeite erneut …")

    def _pruefen_fertig(self, ergebnis, fehler):
        self._pruefen_aktualisieren()
        for var in self.p_vars.values():
            var.set("")
        if fehler:
            messagebox.showerror("Fehler", str(fehler))
        elif ergebnis == "ok":
            self.lbl_grund.configure(text="Abgelegt. Nächstes Dokument auswählen.")
        elif ergebnis == "pruefen":
            messagebox.showwarning("Noch nicht abgelegt", "Das Dokument liegt weiter unter „Prüfen“ – der neue Grund steht in der Liste.")
        else:
            messagebox.showerror("Fehler", "Technischer Fehler – Details im Protokoll auf der Übersicht.")

    # ------------------------------------------------------------------ Analyse
    def _tab_analyse(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Analyse")
        kopf = ttk.Frame(f)
        kopf.pack(fill="x")
        ttk.Label(kopf, text="Zum Einstellen der Suchmuster: zeigt erkannten Text und Felder – verändert nichts.").pack(side="left")
        ttk.Button(kopf, text="PDF auswählen …", command=self._analyse).pack(side="right")
        self.txt_analyse = ScrolledText(f, wrap="word", font=("Consolas", 10))
        self.txt_analyse.pack(fill="both", expand=True, pady=(8, 0))

    def _analyse(self):
        datei = filedialog.askopenfilename(title="Bescheid auswählen", filetypes=[("PDF", "*.pdf")])
        if not datei:
            return
        self.txt_analyse.delete("1.0", "end")
        self.txt_analyse.insert("1.0", "Texterkennung läuft …")
        cfg = self.k.cfg
        self._hintergrund(lambda: kern.analysieren(Path(datei), cfg), self._analyse_fertig, "Analysiere …")

    def _analyse_fertig(self, ergebnis, fehler):
        self.txt_analyse.delete("1.0", "end")
        if fehler:
            self.txt_analyse.insert("1.0", f"Fehler: {fehler}")
            return
        text, e, mit_ki = ergebnis
        aus = ["ERKANNT (Regeln)", "-" * 40]
        aus += [f"{k:15} {v or '–'}" for k, v in asdict(e).items()]
        fehlend = e.fehlend(self.k.cfg["regeln"]["pflichtfelder"])
        aus.append(f"\nPflichtfelder fehlen: {', '.join(fehlend)}" if fehlend else "\nAlle Pflichtfelder erkannt ✔")
        if mit_ki:
            aus += ["", "MIT KI ERGÄNZT", "-" * 40] + [f"{k:15} {v or '–'}" for k, v in asdict(mit_ki).items()]
            e = mit_ki
        try:
            aus += ["", "WÜRDE IN NINOX ANGELEGT", "-" * 40, kern.plan_text(kern.ablage_plan(self.k.cfg, e, "(Dateiname)"))]
        except Exception as ex:
            aus += ["", f"Ninox-Zuordnung fehlerhaft (config.toml prüfen): {ex}"]
        aus += ["", "=" * 70, "ERKANNTER TEXT", "=" * 70, text]
        self.txt_analyse.insert("1.0", "\n".join(aus))

    # ------------------------------------------------------------------ Einstellungen
    def _tab_einstellungen(self):
        aussen = ttk.Frame(self.nb)
        self.nb.add(aussen, text="Einstellungen")
        canvas = tk.Canvas(aussen, highlightthickness=0)
        sb = ttk.Scrollbar(aussen, orient="vertical", command=canvas.yview)
        f = ttk.Frame(canvas, padding=10)
        f.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=f, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
                        if self.nb.index("current") == 3 else None)

        e = self.k.einstellungen
        self.e_vars: dict[tuple, tk.Variable] = {}

        def feld(rahmen, zeile, text, pfad, geheim=False, wahl=None, hinweis=None):
            ttk.Label(rahmen, text=text).grid(row=zeile, column=0, sticky="w", pady=3, padx=(0, 8))
            wert = e
            for p in pfad:
                wert = wert.get(p, "") if isinstance(wert, dict) else ""
            var = tk.StringVar(value=str(wert))
            self.e_vars[pfad] = var
            ttk.Entry(rahmen, textvariable=var, width=60, show="•" if geheim else "").grid(row=zeile, column=1, sticky="ew", pady=3)
            if wahl == "ordner":
                ttk.Button(rahmen, text="…", width=3, command=lambda: self._wahl_ordner(var)).grid(row=zeile, column=2, padx=4)
            elif wahl == "datei":
                ttk.Button(rahmen, text="…", width=3, command=lambda: self._wahl_datei(var)).grid(row=zeile, column=2, padx=4)
            if hinweis:
                ttk.Label(rahmen, text=hinweis, style="Hinweis.TLabel").grid(row=zeile, column=3, sticky="w", padx=4)

        def schalter(rahmen, zeile, text, pfad):
            wert = e
            for p in pfad:
                wert = wert.get(p, False)
            var = tk.BooleanVar(value=bool(wert))
            self.e_vars[pfad] = var
            ttk.Checkbutton(rahmen, text=text, variable=var).grid(row=zeile, column=0, columnspan=3, sticky="w", pady=3)

        r = ttk.LabelFrame(f, text="Ordner", padding=8)
        r.pack(fill="x", pady=4)
        feld(r, 0, "Scan-Eingang", ("ordner", "eingang"), wahl="ordner", hinweis="hierhin scannt der Scanner")
        feld(r, 1, "Verarbeitet", ("ordner", "verarbeitet"), wahl="ordner")
        feld(r, 2, "Prüfen", ("ordner", "pruefen"), wahl="ordner")
        feld(r, 3, "Fehler", ("ordner", "fehler"), wahl="ordner")
        feld(r, 4, "Original-Archiv", ("ordner", "archiv_original"), wahl="ordner", hinweis="leer = Originale löschen")

        r = ttk.LabelFrame(f, text="Ninox", padding=8)
        r.pack(fill="x", pady=4)
        feld(r, 0, "API-Adresse", ("ninox", "base_url"), hinweis="Private Cloud: https://NAME.ninoxdb.com/v1")
        feld(r, 1, "API-Schlüssel", ("ninox", "api_schluessel"), geheim=True)
        feld(r, 2, "Team-ID", ("ninox", "team_id"), hinweis="leer lassen → „Testen“ zeigt die Teams")
        feld(r, 3, "Datenbank-ID", ("ninox", "datenbank_id"), hinweis="leer lassen → „Testen“ zeigt die Datenbanken")
        knoepfe = ttk.Frame(r)
        knoepfe.grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Button(knoepfe, text="Verbindung testen", command=self._ninox_testen).pack(side="left")
        ttk.Button(knoepfe, text="Stammdaten laden (Häuser, Kostensätze)", command=self._stammdaten_laden).pack(side="left", padx=6)
        self.lbl_stamm = ttk.Label(r, text=self._stammdaten_text(), style="Hinweis.TLabel")
        self.lbl_stamm.grid(row=5, column=1, sticky="w")

        r = ttk.LabelFrame(f, text="Texterkennung (lokal)", padding=8)
        r.pack(fill="x", pady=4)
        schalter(r, 0, "Texterkennung verwenden (aus, wenn der Scanner schon durchsuchbare PDFs liefert)", ("ocr", "aktiv"))
        feld(r, 1, "Sprache", ("ocr", "sprache"), hinweis="deu")
        feld(r, 2, "tesseract.exe", ("ocr", "tesseract"), wahl="datei", hinweis="leer = mitgelieferte Version")
        try:
            gefunden = kern.tesseract_finden(self.k.cfg)
        except RuntimeError:
            gefunden = "NICHT GEFUNDEN"
        self.lbl_tess = ttk.Label(r, text=f"Verwendet: {gefunden}", style="Hinweis.TLabel")
        self.lbl_tess.grid(row=3, column=1, sticky="w")

        r = ttk.LabelFrame(f, text="Lokale KI (optional, über Ollama)", padding=8)
        r.pack(fill="x", pady=4)
        schalter(r, 0, "KI nutzen, wenn die Regeln nicht alles finden", ("ki", "aktiv"))
        feld(r, 1, "Ollama-Adresse", ("ki", "url"))
        feld(r, 2, "Modell", ("ki", "modell"))
        ttk.Button(r, text="KI testen", command=self._ki_testen).grid(row=3, column=1, sticky="w", pady=(6, 0))

        r = ttk.LabelFrame(f, text="Allgemein", padding=8)
        r.pack(fill="x", pady=4)
        feld(r, 0, "Prüfintervall (Sek.)", ("ueberwachung", "intervall_s"))
        schalter(r, 1, "Überwachung beim Programmstart automatisch starten", ("ueberwachung", "beim_start_starten"))
        self.var_autostart = tk.BooleanVar(value=autostart_aktiv())
        cb = ttk.Checkbutton(r, text="Mit Windows starten (verkleinert)", variable=self.var_autostart)
        cb.grid(row=2, column=0, columnspan=3, sticky="w", pady=3)
        if os.name != "nt":
            cb.state(["disabled"])

        knopf = ttk.Frame(f)
        knopf.pack(fill="x", pady=(10, 0))
        ttk.Button(knopf, text="💾 Speichern", command=self._speichern).pack(side="left")
        ttk.Button(knopf, text="Eigene Regeln bearbeiten", command=lambda: kern.ordner_oeffnen(self.k.eigen_pfad)).pack(side="left", padx=6)
        ttk.Button(knopf, text="Standardregeln ansehen", command=lambda: kern.ordner_oeffnen(self.k.ansicht_pfad)).pack(side="left")
        ttk.Button(knopf, text="Konfiguration neu laden", command=self._neu_laden).pack(side="left")
        ttk.Button(knopf, text="Datenordner öffnen", command=lambda: kern.ordner_oeffnen(self.k.ordner)).pack(side="left", padx=6)

    def _wahl_ordner(self, var):
        p = filedialog.askdirectory(initialdir=var.get() or None)
        if p:
            var.set(str(Path(p)))

    def _wahl_datei(self, var):
        p = filedialog.askopenfilename(filetypes=[("Programm", "*.exe"), ("Alle", "*.*")])
        if p:
            var.set(str(Path(p)))

    def _formular_lesen(self) -> dict:
        e = copy.deepcopy(self.k.einstellungen)
        for pfad, var in self.e_vars.items():
            ziel = e
            for p in pfad[:-1]:
                ziel = ziel.setdefault(p, {})
            wert = var.get()
            if pfad == ("ueberwachung", "intervall_s"):
                try:
                    wert = max(5, int(wert))
                except ValueError:
                    wert = 30
            elif isinstance(wert, str):
                wert = wert.strip()
            ziel[pfad[-1]] = wert
        e["testlauf"] = bool(self.var_test.get())
        return e

    def _formular_cfg(self) -> dict:
        return kern.tief_mischen(self.k.cfg, self._formular_lesen())

    def _stammdaten_text(self) -> str:
        st = self.k.cfg.get("_stammdaten") or {}
        if not st:
            return "Stammdaten: noch nicht geladen"
        haeuser = ", ".join(h.get("kuerzel") or h.get("name") or "?" for h in st.get("haeuser", [])[:8])
        return (f"Stammdaten vom {st.get('geladen', '?')}: {len(st.get('haeuser', []))} Häuser ({haeuser}), "
                f"{len(st.get('kostensaetze', []))} Kostensätze")

    def _stammdaten_laden(self):
        cfg = self._formular_cfg()

        def laden():
            return kern.Ninox(cfg).stammdaten_laden(self.k.ordner)

        def fertig(daten, fehler):
            if fehler:
                messagebox.showerror("Stammdaten", f"Konnte nicht laden:\n{fehler}\n\nTipp: „Verbindung testen“ zeigt, "
                                                   "ob Tabellen- und Feldnamen stimmen.")
                return
            self.k.laden()
            self._konfiguration_anwenden()
            self.lbl_stamm.configure(text=self._stammdaten_text())
            ohne_adresse = [h.get("kuerzel") or h.get("name") for h in daten["haeuser"]
                            if not ((h.get("strasse") and h.get("plz")) or h.get("anschrift"))]
            ohne_haus = sum(1 for k in daten["kostensaetze"] if k.get("haus_id") is None)
            text = self._stammdaten_text()
            if ohne_adresse:
                text += f"\n\nOhne vollständige Anschrift (Haus kann nicht erkannt werden): {', '.join(ohne_adresse)}"
            if ohne_haus:
                text += f"\n\n{ohne_haus} Kostensätze ohne Haus-Verknüpfung."
            messagebox.showinfo("Stammdaten", text)

        self._hintergrund(laden, fertig, "Lade Stammdaten …")

    def _ninox_testen(self):
        cfg = self._formular_cfg()
        self._hintergrund(lambda: kern.Ninox(cfg).verbindung_testen(),
                          lambda erg, f: text_fenster(self, "Ninox", erg) if not f else
                          messagebox.showerror("Ninox", f"Verbindung fehlgeschlagen:\n{f}"), "Teste Ninox …")

    def _ki_testen(self):
        cfg = self._formular_cfg()
        self._hintergrund(lambda: kern.ki_testen(cfg),
                          lambda erg, f: messagebox.showinfo("KI", erg) if not f else
                          messagebox.showerror("KI", f"Ollama nicht erreichbar:\n{f}"), "Teste KI …")

    def _speichern(self):
        try:
            self.k.speichern(self._formular_lesen())
            autostart_setzen(self.var_autostart.get())
        except Exception as ex:
            messagebox.showerror("Speichern", str(ex))
            return
        self._konfiguration_anwenden()
        messagebox.showinfo("Gespeichert", "Einstellungen gespeichert.")

    def _neu_laden(self):
        try:
            self.k.laden()
        except Exception as ex:
            messagebox.showerror("config.toml", f"Die Regeldatei enthält einen Fehler:\n{ex}")
            return
        self._konfiguration_anwenden()
        messagebox.showinfo("Neu geladen", "Konfiguration neu geladen.")

    def _konfiguration_anwenden(self):
        self._kategorien_setzen()
        try:
            self.lbl_tess.configure(text=f"Verwendet: {kern.tesseract_finden(self.k.cfg)}")
        except RuntimeError:
            self.lbl_tess.configure(text="Verwendet: NICHT GEFUNDEN")
        if self._laeuft():
            self.nach_stopp = lambda: (self._verarbeitung_neu(), self.start())
            self.stop()
        else:
            self._verarbeitung_neu()

    # ------------------------------------------------------------------ Verarbeitung & Überwachung
    def _verarbeitung_neu(self):
        try:
            self.v = kern.Verarbeitung(self.k.cfg, testlauf=self.var_test.get(),
                                       melden=lambda z: self.q.put(("eintrag", z)))
        except Exception as ex:
            self.v = None
            messagebox.showerror("Einstellungen", f"Die Ordner konnten nicht angelegt werden:\n{ex}\n\nBitte unter „Einstellungen“ prüfen.")
        self._zaehler_anzeigen()
        self._pruefen_aktualisieren()

    def _testlauf_umgeschaltet(self):
        if not self.var_test.get():
            n = self.k.cfg["ninox"]
            if not (n.get("api_schluessel") and n.get("team_id") and n.get("datenbank_id")):
                messagebox.showwarning("Ninox", "Bitte erst unter „Einstellungen“ den Ninox-Zugang eintragen.")
                self.var_test.set(True)
                return
        e = copy.deepcopy(self.k.einstellungen)
        e["testlauf"] = bool(self.var_test.get())
        self.k.speichern(e)
        self._konfiguration_anwenden()

    def _laeuft(self) -> bool:
        return bool(self.waechter and self.waechter.is_alive())

    def _start_stopp(self):
        self.stop() if self._laeuft() else self.start()

    def start(self):
        if not self.v:
            messagebox.showerror("Start", "Bitte zuerst die Einstellungen prüfen.")
            return
        if self._laeuft():
            return
        self.stopp.clear()
        v, intervall = self.v, self.k.cfg["ueberwachung"].get("intervall_s", 30)

        def wache():
            while not self.stopp.is_set():
                try:
                    v.durchlauf()
                except Exception:
                    log.exception("Fehler im Durchlauf")
                self.stopp.wait(intervall)
            self.q.put(("gestoppt", None))

        self.waechter = threading.Thread(target=wache, daemon=True)
        self.waechter.start()
        self.lbl_status.configure(text=f"● Überwachung läuft  ({self.v.eingang})", foreground="#1b7a2f")
        self.btn_start.configure(text="■ Überwachung stoppen")
        log.info("Überwachung gestartet")

    def stop(self):
        self.stopp.set()
        self.lbl_status.configure(text="Wird beendet …", foreground="")

    def _jetzt(self):
        if self.v:
            self._hintergrund(self.v.durchlauf, lambda n, f: log.info("%s Datei(en) verarbeitet", n) if not f else None,
                              "Verarbeite …")

    def _hintergrund(self, fn, fertig=None, status: str | None = None):
        if status:
            self.config(cursor="watch")

        def lauf():
            try:
                self.q.put(("fertig", (fertig, fn(), None)))
            except Exception as ex:
                log.exception("Fehler")
                self.q.put(("fertig", (fertig, None, ex)))

        threading.Thread(target=lauf, daemon=True).start()

    def _queue_abholen(self):
        try:
            while True:
                art, daten = self.q.get_nowait()
                if art == "log":
                    self.txt_log.configure(state="normal")
                    self.txt_log.insert("end", daten + "\n")
                    if int(self.txt_log.index("end-1c").split(".")[0]) > 2000:
                        self.txt_log.delete("1.0", "500.0")
                    self.txt_log.see("end")
                    self.txt_log.configure(state="disabled")
                elif art == "eintrag":
                    self._eintrag_einfuegen(daten)
                    self._zaehler_neu()
                    self._pruefen_aktualisieren()
                elif art == "fertig":
                    self.config(cursor="")
                    cb, erg, fehler = daten
                    if cb:
                        cb(erg, fehler)
                elif art == "gestoppt":
                    self.lbl_status.configure(text="Überwachung gestoppt", foreground="")
                    self.btn_start.configure(text="▶ Überwachung starten")
                    log.info("Überwachung gestoppt")
                    if self.nach_stopp:
                        aktion, self.nach_stopp = self.nach_stopp, None
                        aktion()
        except queue.Empty:
            pass
        self.after(200, self._queue_abholen)

    def _beenden(self):
        if self._laeuft() and not messagebox.askyesno(
                "Beenden", "Die Überwachung läuft noch.\nWirklich beenden? Neue Scans werden dann nicht mehr verarbeitet."):
            return
        self.stopp.set()
        self.destroy()


# =========================================================================== Start

def selbsttest(app: "App", ziel: Path) -> None:
    """Für den automatischen Test beim Bauen: alle Reiter einmal anzeigen, dann beenden."""
    try:
        for i in range(len(app.nb.tabs())):
            app.nb.select(i)
            app.update()
        kinder = app.tree.get_children()
        if kinder:
            app.tree.selection_set(kinder[0])
            app._details_zeigen()
        app._stammdaten_text()
        app.update()
        ziel.write_text(f"OK\nEinträge: {len(kinder)}\nDatenordner: {app.k.ordner}\nMeldung: {app.k.migriert or '-'}\n",
                        encoding="utf-8")
    except Exception:
        ziel.write_text("FEHLER\n" + traceback.format_exc(), encoding="utf-8")
    app.stopp.set()
    app.destroy()


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    test_ziel = None
    if "--selbsttest" in sys.argv:
        i = sys.argv.index("--selbsttest")
        test_ziel = Path(sys.argv[i + 1]) if i + 1 < len(sys.argv) else Path("selbsttest.txt")
    try:
        app = App(minimiert="--minimiert" in sys.argv, selbsttest=bool(test_ziel))
        if test_ziel:
            app.after(1500, lambda: selbsttest(app, test_ziel))
        app.mainloop()
    except Exception:
        fehler = traceback.format_exc()
        if test_ziel:
            test_ziel.write_text("FEHLER\n" + fehler, encoding="utf-8")
        notfall_meldung(fehler) if not test_ziel else notfall_protokoll(fehler)


if __name__ == "__main__":
    main()
