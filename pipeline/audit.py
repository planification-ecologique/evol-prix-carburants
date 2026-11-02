"""Audit de la qualité des données gazole, à lancer après build.py (dans GitHub Actions).

Relit les flux bruts, refait les calculs de façon indépendante et compare avec les JSON produits,
puis avec l'export officiel de data.economie.gouv.fr. Écrit un rapport Markdown sur la sortie standard.

Usage : python pipeline/audit.py --data _site/data --cache .cache
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import warnings
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import build as B  # noqa: E402
from geo import DEP_TO_REG, departement  # noqa: E402

warnings.simplefilter("ignore", RuntimeWarning)
API_CSV = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "prix-des-carburants-en-france-flux-instantane-v2/exports/csv?delimiter=%3B"
)


def eur(m):  # millièmes -> texte
    return "–" if m is None or (isinstance(m, float) and np.isnan(m)) else f"{m / 1000:.3f} €"


def p(s=""):
    print(s, flush=True)


class Raw:
    """Lecture brute de tous les flux, en gardant la trace de tout ce qui est rejeté."""

    def __init__(self, now_m: int):
        self.now_m = now_m
        self.since_m = B.minutes(B.HISTORY_START)
        self.ev = []  # (pid, t, v) après lecture, avant filtre de plage
        self.meta = {}  # pid -> dict(cp, lat, lon, ville, adresse)
        self.src = {}
        self.fuel_names = Counter()
        self.ruptures = {}  # pid -> liste de (debut, fin, type) pour le gazole (flux instantané avec ruptures)
        self.fermetures = {}  # pid -> liste de (type, debut, fin)

    def read(self, stream, label, status=False):
        s = self.src[label] = Counter()
        ex = self.src[label + ":ex"] = defaultdict(list)
        for _, el in ET.iterparse(stream, events=("end",)):
            if el.tag != "pdv":
                continue
            s["pdv"] += 1
            try:
                pid = int(el.get("id"))
            except (TypeError, ValueError):
                s["pdv_sans_id"] += 1
                el.clear()
                continue
            m = self.meta.setdefault(pid, {})
            m["cp"] = el.get("cp") or m.get("cp", "")
            m["pop"] = el.get("pop") or m.get("pop", "")
            lat, lon = B.parse_coord(el.get("latitude")), B.parse_coord(el.get("longitude"))
            if lat is not None and lon is not None:
                m["lat"], m["lon"] = lat, lon
            for tag in ("ville", "adresse"):
                t = el.findtext(tag)
                if t:
                    m[tag] = t.strip()
            for pr in el.iter("prix"):
                self.fuel_names[(pr.get("id"), pr.get("nom"))] += 1
                if pr.get("nom") != "Gazole" and pr.get("id") != "1":
                    continue
                s["gazole"] += 1
                dt, v = B.parse_maj(pr.get("maj")), B.parse_price(pr.get("valeur"))
                if dt is None:
                    s["date_illisible"] += 1
                    ex["date_illisible"].append((pid, pr.get("maj"), pr.get("valeur")))
                    continue
                if v is None:
                    s["prix_illisible"] += 1
                    ex["prix_illisible"].append((pid, pr.get("maj"), pr.get("valeur")))
                    continue
                t = B.minutes(dt)
                if t < self.since_m:
                    s["avant_dec_2024"] += 1
                    continue
                if t > self.now_m + 60:
                    s["date_future"] += 1
                    ex["date_future"].append((pid, pr.get("maj"), pr.get("valeur")))
                if v < B.PRICE_MIN:
                    s["prix_<1"] += 1
                    ex["prix_<1"].append((pid, pr.get("maj"), pr.get("valeur")))
                elif v > B.PRICE_MAX:
                    s["prix_>3"] += 1
                    ex["prix_>3"].append((pid, pr.get("maj"), pr.get("valeur")))
                self.ev.append((pid, t, v))
            for r in el.iter("rupture"):
                s["rupture_elements"] += 1
                if r.get("nom") == "Gazole" or r.get("id") == "1":
                    s["rupture_gazole"] += 1
                    ex["rupture_gazole"].append((pid, r.get("debut"), r.get("fin"), r.get("type")))
            for f in el.iter("fermeture"):
                s["fermeture_elements"] += 1
                ex["fermeture"].append((pid, f.get("type"), f.get("debut"), f.get("fin")))
            if status:
                for r in el.iter("rupture"):
                    if r.get("nom") == "Gazole" or r.get("id") == "1":
                        self.ruptures.setdefault(pid, []).append((r.get("debut"), r.get("fin"), r.get("type")))
                for f in el.iter("fermeture"):
                    self.fermetures.setdefault(pid, []).append((f.get("type"), f.get("debut"), f.get("fin")))
            el.clear()


def ongoing(debut, fin, now):
    d, f = B.parse_maj(debut or ""), B.parse_maj(fin or "")
    return (d is None or d <= now) and (f is None or f > now)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="_site/data")
    ap.add_argument("--cache", default=".cache")
    args = ap.parse_args()
    data, cache = Path(args.data), Path(args.cache)
    meta = json.loads((data / "meta.json").read_text())
    series = json.loads((data / "series.json").read_text())
    stations_js = json.loads((data / "stations.json").read_text())
    now = datetime.fromisoformat(meta["data_until"])
    now_m = B.minutes(now)

    p("# Audit des données gazole")
    p(f"Données jusqu'au {meta['data_until']}, généré le {meta['generated_at']}.")
    p()

    # ------------------------------------------------------------------ 1. lecture brute
    R = Raw(now_m)
    year = now.year
    for y in range(B.HISTORY_START.year, year):
        R.read(B.xml_from_zip(B.fetch(f"{B.BASE}/annee/{y}", cache, keep=True)), f"annee/{y}")
    R.read(B.xml_from_zip(B.fetch(f"{B.BASE}/annee", cache, keep=False)), "annee")
    for d in range(3, 0, -1):
        day = (now - timedelta(days=d)).strftime("%Y%m%d")
        try:
            R.read(B.xml_from_zip(B.fetch(f"{B.BASE}/jour/{day}", cache, keep=False)), f"jour/{day}")
        except Exception as e:  # noqa: BLE001
            p(f"(jour/{day} indisponible : {e})")
    R.read(B.xml_from_zip(B.fetch(f"{B.BASE}/jour", cache, keep=False)), "jour")
    R.read(B.xml_from_zip(B.fetch(f"{B.BASE}/instantane", cache, keep=False)), "instantane")
    R.read(B.xml_from_zip(B.fetch(f"{B.BASE}/instantane_ruptures", cache, keep=False)), "instantane_ruptures", status=True)

    p("## 1. Lecture des flux")
    p("| Source | Stations | Prix gazole | Avant déc. 2024 | Date illisible | Prix illisible | Date future | < 1 € | > 3 € |")
    p("|---|---|---|---|---|---|---|---|---|")
    for k, s in R.src.items():
        if k.endswith(":ex"):
            continue
        p(f"| {k} | {s['pdv']} | {s['gazole']} | {s['avant_dec_2024']} | {s['date_illisible']} | {s['prix_illisible']} | {s['date_future']} | {s['prix_<1']} | {s['prix_>3']} |")
    p()
    p("Éléments rupture / fermeture par source : " + ", ".join(f"{k}: {s['rupture_elements']} ruptures dont {s['rupture_gazole']} gazole, {s['fermeture_elements']} fermetures" for k, s in R.src.items() if not k.endswith(":ex")))
    p()
    p("Carburants rencontrés (id, nom) : " + ", ".join(f"{k}={v}" for k, v in sorted(R.fuel_names.items(), key=lambda x: -x[1])))
    p()
    for k, ex in R.src.items():
        if not k.endswith(":ex"):
            continue
        for kind, rows in ex.items():
            vals = Counter(r[2] for r in rows)
            p(f"- {k[:-3]} / {kind} : {len(rows)} cas. Valeurs les plus fréquentes : {vals.most_common(8)}. Exemples : {rows[:4]}")
    p()

    # ------------------------------------------------------------------ 2. nettoyage identique à build.py
    ev = np.asarray(R.ev, dtype=np.int64)
    sid, ts, pr = ev[:, 0], ev[:, 1], ev[:, 2]
    ok = (pr >= B.PRICE_MIN) & (pr <= B.PRICE_MAX)
    sid, ts, pr = sid[ok], ts[ok], pr[ok]
    order = np.lexsort((ts, sid))
    sid, ts, pr = sid[order], ts[order], pr[order]
    same = (sid[1:] == sid[:-1]) & (ts[1:] == ts[:-1])
    conflicts = int(np.sum(same & (pr[1:] != pr[:-1])))
    keep = np.ones(len(sid), bool)
    keep[:-1] = ~same
    sid, ts, pr = sid[keep], ts[keep], pr[keep]
    p("## 2. Nettoyage")
    p(f"- Prix gazole lus (après déc. 2024) : {len(R.ev)} ; hors plage 1–3 € écartés : {int(np.sum(~ok))}.")
    p(f"- Doublons exacts (même station, même horodatage) supprimés : {int(np.sum(same))}, dont {conflicts} avec des prix différents.")
    p(f"- Prix retenus : {len(sid)}.")

    # stations retenues par build.py
    st_ids = np.asarray(stations_js["id"], dtype=np.int64)
    sel = np.isin(sid, st_ids)
    excl_ids = set(np.unique(sid[~sel]).tolist())
    reasons = Counter()
    for pid in excl_ids:
        m = R.meta.get(pid, {})
        cp = m.get("cp", "")
        if cp.startswith(("97", "98")):
            reasons["outre-mer (code postal 97/98)"] += 1
        elif departement(cp) is None:
            reasons[f"code postal invalide ({cp!r})"] += 1
        elif not ("lat" in m and 41 < m["lat"] < 52 and -6 < m["lon"] < 10):
            reasons["coordonnées absentes ou hors métropole"] += 1
        else:
            reasons["autre"] += 1
    recent_excl = {int(x) for x in np.unique(sid[~sel & (ts >= now_m - 30 * 1440)])}
    p(f"- Stations avec des prix gazole mais exclues du calcul : {len(excl_ids)} (dont {len(recent_excl)} avec un prix dans les 30 derniers jours). Raisons : {dict(reasons)}.")
    if recent_excl:
        p(f"  Exemples récents : {[(i, R.meta.get(i, {}).get('cp'), R.meta.get(i, {}).get('ville'), R.meta.get(i, {}).get('lat'), R.meta.get(i, {}).get('lon')) for i in list(recent_excl)[:8]]}")
    outre_mer = [i for i, m in R.meta.items() if m.get("cp", "").startswith(("97", "98"))]
    p(f"- Points de vente outre-mer présents dans les flux (toutes années lues) : {len(outre_mer)}.")
    sid, ts, pr = sid[sel], ts[sel], pr[sel]
    p()

    # ------------------------------------------------------------------ 3. volume par jour
    day = (ts // 1440).astype(np.int64)
    days, counts = np.unique(day, return_counts=True)
    med = np.median(counts)
    full = np.arange(days.min(), days.max() + 1)
    missing = sorted(set(full.tolist()) - set(days.tolist()))
    low = [(B.from_minutes(d * 1440).date().isoformat(), int(c)) for d, c in zip(days, counts) if c < 0.4 * med]
    p("## 3. Couverture dans le temps")
    p(f"- Jours couverts : {len(days)} du {B.from_minutes(days.min() * 1440).date()} au {B.from_minutes(days.max() * 1440).date()} ; changements de prix par jour : médiane {int(med)}, min {int(counts.min())}, max {int(counts.max())}.")
    p(f"- Jours sans aucun changement de prix : {[B.from_minutes(d * 1440).date().isoformat() for d in missing]}")
    p(f"- Jours anormalement creux (< 40 % de la médiane) : {low}")
    p(f"- 10 derniers jours : {[(B.from_minutes(d * 1440).date().isoformat(), int(c)) for d, c in zip(days[-10:], counts[-10:])]}")
    for key in ("fev26", "2025"):
        n = np.asarray(series[key]["FR"]["n"])
        pts = meta["periods"][key]["points"]
        jumps = [(pts[i], int(n[i - 1]), int(n[i])) for i in range(1, len(n)) if abs(n[i] - n[i - 1]) > 0.03 * max(n[i - 1], 1)]
        p(f"- Stations actives ({key}) : min {n.min()} ({pts[int(n.argmin())]}), max {n.max()} ; variations de plus de 3 % d'un point à l'autre : {jumps[:10]}")
    p()

    # ------------------------------------------------------------------ 4. recalcul indépendant et cohérence avec series.json
    deps = np.asarray(stations_js["dep"])
    pts_m = np.asarray([B.minutes(datetime.fromisoformat(t)) for t in meta["periods"]["fev26"]["points"]], dtype=np.int64)
    mat = B.price_matrix(sid, ts, pr, st_ids, pts_m)  # en €
    fr_med = np.nanmedian(mat, axis=0)
    fr_mean = np.nanmean(mat, axis=0)
    js_med = np.asarray([np.nan if x is None else x for x in series["fev26"]["FR"]["median"]])
    js_mean = np.asarray([np.nan if x is None else x for x in series["fev26"]["FR"]["mean"]])
    p("## 4. Recalcul indépendant (période depuis février 2026, France)")
    p(f"- Écart maximal entre le recalcul et series.json : médiane {np.nanmax(np.abs(fr_med - js_med)):.5f} €, moyenne {np.nanmax(np.abs(fr_mean - js_mean)):.5f} €.")
    worst = []
    for d in sorted(set(deps.tolist())):
        sub = mat[deps == d]
        js = np.asarray([np.nan if x is None else x for x in series["fev26"][f"D{d}"]["median"]])
        worst.append((float(np.nanmax(np.abs(np.nanmedian(sub, axis=0) - js))), d))
    p(f"- Écart maximal par département (médiane) : {max(worst)}")
    last = mat[:, -1]
    act = ~np.isnan(last)
    p(f"- Dernier point ({meta['periods']['fev26']['points'][-1]}) : {int(act.sum())} stations actives, médiane {np.nanmedian(last):.3f} €, moyenne {np.nanmean(last):.3f} €.")
    pc = np.nanpercentile(last, [0, 1, 5, 25, 50, 75, 95, 99, 100])
    p(f"- Distribution des prix au dernier point (min, p1, p5, p25, p50, p75, p95, p99, max) : {[round(float(x), 3) for x in pc]}")
    p()

    # ------------------------------------------------------------------ 5. valeurs suspectes
    p("## 5. Valeurs suspectes")
    # 5a. aller-retour : saut >= 0,30 € annulé dans les 72 h (faute de saisie probable)
    same3 = (sid[1:-1] == sid[:-2]) & (sid[2:] == sid[1:-1])
    up = pr[1:-1] - pr[:-2]
    back = pr[2:] - pr[:-2]
    dt_back = ts[2:] - ts[1:-1]
    spike = same3 & (np.abs(up) >= 300) & (np.abs(back) <= 50) & (dt_back <= 72 * 60)
    idx = np.where(spike)[0] + 1
    p(f"- Allers-retours (saut d'au moins 0,30 € annulé en moins de 72 h) : {len(idx)}.")
    for i in idx[np.argsort(-np.abs(pr[idx] - pr[idx - 1]))][:10]:
        m = R.meta.get(int(sid[i]), {})
        p(f"  - station {sid[i]} ({m.get('cp')} {m.get('ville')}) : {eur(pr[i - 1])} → {eur(pr[i])} le {B.from_minutes(ts[i])} → {eur(pr[i + 1])} le {B.from_minutes(ts[i + 1])}")
    big = (sid[1:] == sid[:-1]) & (np.abs(pr[1:] - pr[:-1]) >= 500)
    p(f"- Changements d'au moins 0,50 € d'un coup (tous) : {int(big.sum())}.")
    # 5b. écarts à la médiane nationale du jour
    devs = np.abs(mat - fr_med[None, :])
    for thr in (0.30, 0.40, 0.50):
        n_sd = int(np.nansum(devs > thr))
        n_st = int(np.sum(np.nanmax(devs, axis=1) > thr))
        p(f"- Écart de plus de {thr:.2f} € à la médiane nationale du jour : {n_sd} station-jours, {n_st} stations concernées au moins un jour.")
    out_last = np.where(np.abs(last - fr_med[-1]) > 0.40)[0]
    p(f"- Au dernier point, {len(out_last)} stations s'écartent de plus de 0,40 € de la médiane :")
    for i in out_last[:15]:
        pid = int(st_ids[i])
        m = R.meta.get(pid, {})
        p(f"  - {pid} {m.get('cp')} {m.get('ville')} : {last[i]:.3f} €")
    # 5c. impact d'une exclusion de ces valeurs sur les agrégats France
    for thr in (0.40, 0.50):
        clean = np.where(devs > thr, np.nan, mat)
        dm = np.nanmax(np.abs(np.nanmedian(clean, axis=0) - fr_med))
        da = np.nanmax(np.abs(np.nanmean(clean, axis=0) - fr_mean))
        p(f"- Si l'on écartait les écarts > {thr:.2f} € : la médiane France bougerait au plus de {dm:.4f} €, la moyenne de {da:.4f} €.")
    p()

    # ------------------------------------------------------------------ 6. ancienneté, ruptures, fermetures
    p("## 6. Ancienneté des prix, ruptures et fermetures (dernier point)")
    li = np.searchsorted(sid, st_ids, side="right") - 1
    has = (li >= 0) & (sid[np.clip(li, 0, None)] == st_ids)
    age_d = np.where(has, (now_m - ts[np.clip(li, 0, None)]) / 1440, np.nan)
    a = age_d[act]
    p(f"- Âge du dernier prix des stations actives : médiane {np.median(a):.1f} j ; > 2 j : {int(np.sum(a > 2))} ; > 7 j : {int(np.sum(a > 7))} ; > 14 j : {int(np.sum(a > 14))}.")
    for days_ in (7, 14):
        alt = np.where(age_d <= days_, last, np.nan)
        p(f"- Avec un seuil d'activité de {days_} j au lieu de 30 : {int(np.sum(~np.isnan(alt)))} stations, médiane {np.nanmedian(alt):.3f} €, moyenne {np.nanmean(alt):.3f} €.")
    rup_types = Counter()
    rup_ids = set()
    for pid, lst in R.ruptures.items():
        for d, f, t in lst:
            if ongoing(d, f, now):
                rup_types[t] += 1
                rup_ids.add(pid)
    ferm = Counter()
    ferm_ids = set()
    for pid, lst in R.fermetures.items():
        for t, d, f in lst:
            if ongoing(d, f, now):
                ferm[t] += 1
                ferm_ids.add(pid)
    in_act = lambda ids: np.isin(st_ids, list(ids)) & act  # noqa: E731
    p(f"- Ruptures de gazole en cours (flux instantané) : {len(rup_ids)} stations, par type {dict(rup_types)} ; dont actives dans notre calcul : {int(in_act(rup_ids).sum())}.")
    p(f"- Fermetures en cours : {len(ferm_ids)} stations, par type {dict(ferm)} ; dont actives dans notre calcul : {int(in_act(ferm_ids).sum())}.")
    ex = in_act(rup_ids | ferm_ids)
    alt = np.where(ex, np.nan, last)
    p(f"- En les excluant : {int(np.sum(~np.isnan(alt)))} stations, médiane {np.nanmedian(alt):.3f} € (au lieu de {np.nanmedian(last):.3f}), moyenne {np.nanmean(alt):.3f} € (au lieu de {np.nanmean(last):.3f}).")
    if rup_ids:
        r_last = last[in_act(rup_ids)]
        p(f"- Prix (dernier connu) des stations en rupture : médiane {np.nanmedian(r_last):.3f} €.")
    p()

    # ------------------------------------------------------------------ 7. géographie
    p("## 7. Géographie")
    cnt = Counter(deps[act].tolist())
    small = sorted((v, k) for k, v in cnt.items())[:8]
    p(f"- Départements avec le moins de stations actives : {small}")
    p(f"- Corse : 2A = {cnt.get('2A', 0)}, 2B = {cnt.get('2B', 0)}.")
    lat = np.asarray(stations_js["lat"])
    lon = np.asarray(stations_js["lon"])
    coords = Counter(zip(np.round(lat[act], 4).tolist(), np.round(lon[act], 4).tolist()))
    dup = [(k, v) for k, v in coords.items() if v > 1]
    p(f"- Stations actives partageant exactement les mêmes coordonnées : {sum(v for _, v in dup)} ({len(dup)} positions).")
    try:
        from shapely.geometry import Point, shape
        from shapely.strtree import STRtree

        feats = json.loads((data / "departements.geojson").read_text())["features"]
        geoms = [shape(f["geometry"]) for f in feats]
        codes = [f["properties"]["code"] for f in feats]
        tree = STRtree(geoms)
        mism = []
        for i in np.where(act)[0]:
            pt = Point(lon[i], lat[i])
            hits = [codes[j] for j in tree.query(pt) if geoms[j].covers(pt)]
            if hits and deps[i] not in hits:
                mism.append((int(st_ids[i]), str(deps[i]), hits[0], stations_js["ville"][i]))
            elif not hits:
                # hors de tout polygone : côte, frontière ou coordonnées fausses
                d = min(range(len(geoms)), key=lambda j: geoms[j].distance(pt))
                if geoms[d].distance(pt) > 0.05 or codes[d] != deps[i]:
                    mism.append((int(st_ids[i]), str(deps[i]), f"hors polygone, proche {codes[d]} à {geoms[d].distance(pt):.2f}°", stations_js["ville"][i]))
        p(f"- Stations actives dont la position ne tombe pas dans le département de leur code postal : {len(mism)}.")
        for m in mism[:20]:
            p(f"  - {m}")
    except Exception as e:  # noqa: BLE001
        p(f"- Contrôle géographique impossible : {e}")
    p()

    # ------------------------------------------------------------------ 8. contre-vérification avec l'API officielle
    p("## 8. Comparaison avec l'export officiel data.economie.gouv.fr (flux instantané v2)")
    try:
        raw = B.fetch(API_CSV, None, keep=False).decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(raw), delimiter=";"))
        cols = list(rows[0].keys()) if rows else []
        p(f"- {len(rows)} lignes ; colonnes gazole : {[c for c in cols if 'gazole' in c.lower()]}")
        idc = next(c for c in cols if c.lower() == "id")
        cpc = next(c for c in cols if c.lower() in ("cp", "code_postal"))
        prc = next(c for c in cols if c.lower() == "gazole_prix")
        mjc = next(c for c in cols if c.lower() == "gazole_maj")
        api = {}
        for r in rows:
            if not r[prc]:
                continue
            dt = B.parse_maj(r[mjc][:19])
            api[int(r[idc])] = (r[cpc], float(r[prc]), dt)
        ours = {int(i): (pp, mj) for i, pp, mj in zip(stations_js["id"], stations_js["prix"], stations_js["maj"])}
        both = [i for i in api if i in ours and ours[i][0] is not None]
        diff = [(i, api[i][1], ours[i][0], api[i][2], ours[i][1]) for i in both if abs(api[i][1] - ours[i][0]) > 0.0015]
        p(f"- Stations avec un prix gazole dans l'API : {len(api)} ; présentes chez nous avec un prix : {len(both)} ; prix différents : {len(diff)}.")
        for d in diff[:10]:
            p(f"  - {d}")
        cutoff = now - timedelta(days=B.STALE_DAYS)
        vals = [v for (cp, v, dt) in api.values() if departement(cp) and dt and dt >= cutoff and 1 <= v <= 3]
        p(f"- API, métropole, prix de moins de 30 jours : {len(vals)} stations, médiane {np.median(vals):.3f} €, moyenne {np.mean(vals):.3f} € (nous : {int(act.sum())} stations, médiane {np.nanmedian(last):.3f} €, moyenne {np.nanmean(last):.3f} €).")
        missing_api = [i for i in st_ids[act].tolist() if i not in api]
        p(f"- Stations actives chez nous mais sans prix gazole dans l'API : {len(missing_api)}.")
    except Exception as e:  # noqa: BLE001
        p(f"- Comparaison impossible : {e!r}")
    p()

    # ------------------------------------------------------------------ 10. compléments
    p("## 10. Compléments")
    vals, cnts = np.unique(np.round(last[act], 3), return_counts=True)
    top = sorted(zip(cnts.tolist(), vals.tolist()), reverse=True)[:6]
    p(f"- Prix les plus fréquents au dernier point (nombre de stations, prix) : {top}")
    pts_f = meta["periods"]["fev26"]["points"]
    share = np.nansum(np.isclose(mat, 2.25), axis=0) / np.sum(~np.isnan(mat), axis=0)
    p("- Part des stations actives à exactement 2,250 € (un point par semaine) : " + ", ".join(f"{pts_f[i][:10]} {share[i]:.1%}" for i in range(0, len(pts_f), 7)) + f", dernier point {share[-1]:.1%}")
    # écarts signés à la médiane, par type de route (A = autoroute)
    pop = np.asarray([R.meta.get(int(i), {}).get("pop", "") for i in st_ids])
    sdev = mat - fr_med[None, :]
    for lab, msk in (("autoroute", pop == "A"), ("route", pop != "A")):
        sub = sdev[msk]
        p(f"- Écarts signés à la médiane ({lab}, {int(msk.sum())} stations) : < −0,50 € : {int(np.nansum(sub < -0.5))} station-jours ; < −0,30 € : {int(np.nansum(sub < -0.3))} ; > +0,30 € : {int(np.nansum(sub > 0.3))} ; > +0,50 € : {int(np.nansum(sub > 0.5))} ; > +0,70 € : {int(np.nansum(sub > 0.7))}")
    lows = sdev[~np.isnan(sdev) & (sdev < -0.5)]
    p(f"- Écarts < −0,50 € : quantiles 5/50/95 % = {np.round(np.percentile(lows, [5, 50, 95]), 3).tolist() if lows.size else []}")
    p("- Au dernier point, écarts > 0,40 € avec type de route et prix précédent :")
    for i in out_last[:15]:
        pid = int(st_ids[i])
        a_, b_ = np.searchsorted(sid, [pid, pid + 1])
        hist = [(str(B.from_minutes(t))[:16], round(float(v) / 1000, 3)) for t, v in zip(ts[a_:b_][-4:], pr[a_:b_][-4:])]
        p(f"  - {pid} {R.meta.get(pid, {}).get('ville')} pop={R.meta.get(pid, {}).get('pop')} : derniers prix {hist}")
    # allers-retours qui touchent un point de calcul (minuit)
    mid_hit = 0
    for i in idx:
        a0, b0 = ts[i] // 1440, ts[i + 1] // 1440
        mid_hit += int(b0 > a0)
    p(f"- Allers-retours qui couvrent au moins un minuit (donc visibles dans les séries journalières) : {mid_hit} sur {len(idx)}.")
    # doublons de coordonnées
    shown = 0
    for (la, lo), v in dup[:6]:
        ids_ = [int(st_ids[i]) for i in np.where(act & (np.round(lat, 4) == la) & (np.round(lon, 4) == lo))[0]]
        p(f"  - ({la}, {lo}) : " + "; ".join(f"{j} {R.meta.get(j, {}).get('adresse')} {R.meta.get(j, {}).get('ville')} prix {stations_js['prix'][stations_js['id'].index(j)]} maj {stations_js['maj'][stations_js['id'].index(j)]}" for j in ids_))
        shown += 1
    # moyennes et médianes journalières autour des semaines publiées par la DGEC
    p("- Valeurs journalières France (à minuit) du 7 septembre au 12 octobre 2026 :")
    for t, mn, md in zip(pts_f, fr_mean, fr_med):
        if "2026-09-07" <= t[:10] <= "2026-10-12":
            p(f"  - {t[:10]} : moyenne {mn:.3f} €, médiane {md:.3f} €")
    p()

    # ------------------------------------------------------------------ 9. moyennes hebdomadaires (pour comparer à une référence publiée)
    p("## 9. Moyennes hebdomadaires France (lundi → dimanche, moyenne des valeurs journalières à minuit)")
    pts = meta["periods"]["fev26"]["points"]
    weeks = defaultdict(list)
    for t, mn, md in zip(pts, fr_mean, fr_med):
        dtt = datetime.fromisoformat(t)
        monday = (dtt - timedelta(days=dtt.weekday())).date().isoformat()
        weeks[monday].append((mn, md))
    for wk in sorted(weeks)[-8:]:
        v = np.asarray(weeks[wk])
        p(f"- semaine du {wk} : moyenne {np.nanmean(v[:, 0]):.3f} €, médiane {np.nanmean(v[:, 1]):.3f} € ({len(v)} points)")


if __name__ == "__main__":
    main()
