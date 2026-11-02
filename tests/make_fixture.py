"""Génère un petit flux XML synthétique (même structure que prix-carburants) pour tester build.py sans réseau."""
import random
import sys
from datetime import datetime, timedelta

random.seed(1)
cps = ["75011", "69003", "13001", "20000", "20200", "33000", "59000", "01000"]
out = ['<?xml version="1.0" encoding="ISO-8859-1"?>', "<pdv_liste>"]
start, end = datetime(2024, 12, 1), datetime(2026, 11, 2, 13, 0)
for i in range(40):
    cp = cps[i % len(cps)]
    lat, lon = 4300000 + random.randint(0, 700000), 100000 + random.randint(0, 500000)
    out.append(f'<pdv id="{1000000 + i}" latitude="{lat}" longitude="{lon}" cp="{cp}" pop="R">')
    out.append(f"<adresse>{i} rue de Test</adresse><ville>Ville {i}</ville>")
    t, p = start, 1.65 + random.random() * 0.1
    while t < end:
        out.append(f'<prix nom="Gazole" id="1" maj="{t:%Y-%m-%dT%H:%M:%S}" valeur="{p:.3f}"/>')
        out.append(f'<prix nom="SP95" id="2" maj="{t:%Y-%m-%dT%H:%M:%S}" valeur="{p + 0.1:.3f}"/>')
        t += timedelta(hours=random.randint(6, 96))
        p += random.uniform(-0.02, 0.019)
    out.append("</pdv>")
out.append("</pdv_liste>")
open(sys.argv[1], "w", encoding="iso-8859-1").write("\n".join(out))
