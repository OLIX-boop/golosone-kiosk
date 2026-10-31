# Esegui con Passepartout aperto in modalita KIOSK e un carrello caricato.
# Crea controlli_passepartout.txt con l'albero dei controlli: da li' prendi auto_id / title per config.json.
import json, os
from pywinauto import Desktop

base = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(base, "config.json"), encoding="utf-8"))
w = Desktop(backend="uia").window(title_re=cfg["window_title_re"])
out = os.path.join(base, "controlli_passepartout.txt")
w.print_control_identifiers(filename=out)
print("Salvato:", out)
