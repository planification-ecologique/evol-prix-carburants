"""Génère un petit flux XML synthétique (même structure que prix-carburants) pour tester build.py sans réseau.

Usage : python tests/make_fixture.py historique.xml [instantane_ruptures.xml]
Le second fichier, facultatif, imite le flux instantané avec ruptures (dernier prix de chaque station).
"""
import random
import sys
from datetime import datetime, timedelta

random.seed(1)
cps = ["75011", "69003", "13001", "20000", "20200", "33000", "59000", "01000"]
out = ['<?xml version="1.0" encoding="ISO-8859-1"?>', "<pdv_liste>"]
snap = []
start, end = datetime(2024, 12, 1), datetime(2026, 11, 2, 13, 0)
for i in range(40):
    cp = cps[i % len(cps)]
    lat, lon = 4300000 + random.randint(0, 700000), 100000 + random.randint(0, 500000)
    if i == 6:
        lon = 99900000  # coordonnées invalides : station comptée mais absente de la carte
    out.append(f'<pdv id="{1000000 + i}" latitude="{lat}" longitude="{lon}" cp="{cp}" pop="R">')
    out.append(f"<adresse>{i} rue de Test</adresse><ville>Ville {i}</ville>")
    t, p = start, 2.25 + random.random() * 0.1
    k = 0
    last = None
    while t < end:
        v = p
        if i == 1 and k == 50:
            v = p - 1.0  # faute de saisie corrigée ensuite (aller-retour)
        out.append(f'<prix nom="Gazole" id="1" maj="{t:%Y-%m-%dT%H:%M:%S}" valeur="{v:.3f}"/>')
        last = (t, v)
        out.append(f'<prix nom="SP95" id="2" maj="{t:%Y-%m-%dT%H:%M:%S}" valeur="{p + 0.1:.3f}"/>')
        t += timedelta(hours=random.randint(6, 96))
        p += random.uniform(-0.02, 0.019)
        k += 1
    if i == 2:  # faute de saisie non corrigée en fin de série
        out.append(f'<prix nom="Gazole" id="1" maj="{end:%Y-%m-%dT%H:%M:%S}" valeur="{p - 1.0:.3f}"/>')
    if i == 0:  # rupture de gazole du 1er au 10 octobre 2026
        out.append('<rupture id="1" nom="Gazole" debut="2026-10-01T08:00:00" fin="2026-10-10T08:00:00" type="temporaire"/>')
    if i == 3:  # fermeture définitive le 20 octobre 2026
        out.append('<fermeture type="D" debut="2026-10-20T00:00:00" fin=""/>')
    if i == 4:  # rupture sans date de fin : elle prend fin au prix suivant
        out.append('<rupture id="1" nom="Gazole" debut="2026-09-15T08:00:00" fin="" type="temporaire"/>')
    if i == 5:  # fermeture vide (cas le plus fréquent dans les flux) et rupture d'un autre carburant
        out.append('<fermeture/>')
        out.append('<rupture id="2" nom="SP95" debut="2026-10-01T08:00:00" fin="" type="temporaire"/>')
    out.append("</pdv>")
    if i == 7:
        continue  # absente du flux instantané : ne doit pas compter au dernier point
    snap.append(f'<pdv id="{1000000 + i}" latitude="{lat}" longitude="{lon}" cp="{cp}" pop="R"><ville>Ville {i}</ville>')
    if i == 8:  # rupture en cours depuis la veille
        snap.append('<rupture id="1" nom="Gazole" debut="2026-11-01 18:00:00" fin="" type="temporaire"/>')
    elif i == 2:
        snap.append(f'<prix nom="Gazole" id="1" maj="{end:%Y-%m-%d %H:%M:%S}" valeur="{p - 1.0:.3f}"/>')
    else:
        snap.append(f'<prix nom="Gazole" id="1" maj="{last[0]:%Y-%m-%d %H:%M:%S}" valeur="{last[1]:.3f}"/>')
    snap.append("</pdv>")
out.append("</pdv_liste>")
open(sys.argv[1], "w", encoding="iso-8859-1").write("\n".join(out))
if len(sys.argv) > 2:
    open(sys.argv[2], "w", encoding="iso-8859-1").write("\n".join(['<?xml version="1.0" encoding="ISO-8859-1"?>', "<pdv_liste>"] + snap + ["</pdv_liste>"]))
