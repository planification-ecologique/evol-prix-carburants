"""Audit de la qualité des données gazole, à lancer après « build.py --dump-events --snapshot » (GitHub Actions).

Le script relit les flux bruts et refait toute la chaîne de calcul avec une implémentation
indépendante et volontairement simple (boucles Python plutôt que calcul vectoriel) : lecture,
doublons, fautes de saisie, ruptures et fermetures, prix de chaque station à chaque instant,
agrégats par zone. Il compare le résultat aux fichiers publiés par build.py, puis à l'export
officiel de data.economie.gouv.fr et aux prix hebdomadaires publiés par le ministère (DGEC).
Le rapport Markdown est écrit sur la sortie standard, la progression sur la sortie d'erreur.

Usage : python pipeline/audit.py --data _site/data --cache .cache --events events.npz --snapshot flux
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import statistics
import sys
import time
import traceback
import warnings
import xml.etree.ElementTree as ET
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import build as B  # noqa: E402  (téléchargement et constantes ; le calcul vectoriel ne sert qu'aux analyses d'impact)
from geo import DEP_TO_REG, REGIONS  # noqa: E402

warnings.simplefilter("ignore", RuntimeWarning)

API_CSV = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "prix-des-carburants-en-france-flux-instantane-v2/exports/csv?delimiter=%3B"
)
EPOCH = datetime(1970, 1, 1)
DAY = 1440
STALE = B.STALE_DAYS * DAY
INF = 1 << 40  # même convention que build.FAR_FUTURE pour une exclusion sans fin

# Prix moyens hebdomadaires du gazole publiés par le ministère de la Transition écologique (DGEC),
# en €/L TTC : prix relevés le vendredi, pondérés par les volumes des réseaux de distribution.
# Sources : note de la DGEC du 28 août 2026 (5 juin → 28 août) ; note du 5 octobre 2026, citée par Kelwatt
# (18 septembre → 2 octobre) ; note du 23 octobre 2026, citée par Selectra (16 et 23 octobre).
DGEC = {
    "2026-06-05": 2.0144, "2026-06-12": 1.9953, "2026-06-19": 1.8898, "2026-06-26": 1.8691,
    "2026-07-03": 1.8630, "2026-07-10": 1.9479, "2026-07-17": 2.0664, "2026-07-24": 2.1685,
    "2026-07-31": 2.2158, "2026-08-07": 2.1690, "2026-08-14": 2.1993, "2026-08-21": 2.2312,
    "2026-08-28": 2.2085, "2026-09-18": 2.383, "2026-09-25": 2.371, "2026-10-02": 2.356,
    "2026-10-16": 2.3458, "2026-10-23": 2.3515,
}

LINES: list[str] = []
CHECKS: list[tuple[str, str]] = []


def p(s=""):
    LINES.append(str(s))


def check(ok, text):
    """ok : True (conforme), False (anomalie à corriger), None (point d'attention)."""
    CHECKS.append(("✅" if ok is True else "❌" if ok is False else "⚠️", text))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def fr(x, nd=3, unit=" €"):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    return f"{x:.{nd}f}".replace(".", ",") + unit


def sg(x, nd=3):
    """Écart signé : « +0,020 € » ou « −0,005 € »."""
    return "–" if x is None else ("+" if x >= 0 else "−") + fr(abs(x), nd)


def pct(a, b):
    return "–" if not b else f"{100 * a / b:.1f} %".replace(".", ",")


def nf(x):
    return f"{x:,}".replace(",", " ")


def mins(dt: datetime) -> int:
    return int((dt - EPOCH).total_seconds() // 60)


def dt_of(m) -> datetime:
    return EPOCH + timedelta(minutes=int(m))


def ts_txt(m) -> str:
    return "∞" if m >= INF else dt_of(m).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- lecture brute (indépendante de build.py)

DT_RE = re.compile(r"\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")


def parse_dt(s):
    m = DT_RE.match(s or "")
    if not m:
        return None
    try:
        return datetime(*(int(x) for x in m.groups(default="0")))
    except ValueError:
        return None


def parse_val(s):
    """Prix en millièmes d'euro (anciens fichiers : « 1749 », récents : « 1.749 »)."""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return int(round(v * 1000)) if v < 100 else int(round(v))


def parse_coord(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v / 100000 if abs(v) > 1000 else v


def dep_from_cp(cp):
    cp = (cp or "").strip()
    if len(cp) == 4:
        cp = "0" + cp
    if len(cp) != 5 or not cp.isdigit():
        return None
    if cp[:2] == "20":
        return "2A" if int(cp) < 20200 else "2B"
    return cp[:2] if cp[:2] in METRO else None


METRO = {f"{i:02d}" for i in range(1, 96) if i != 20} | {"2A", "2B"}


class Raw:
    """Lecture brute de tous les flux, en gardant la trace de tout ce qui est rejeté."""

    def __init__(self, since_m: int, now_m: int):
        self.since_m, self.now_m = since_m, now_m
        self.ev = []  # (station, minute, millièmes) dans l'ordre de lecture
        self.meta = {}
        self.src = {}
        self.examples = defaultdict(list)
        self.fuel_names = Counter()
        self.rupt = []  # (station, début, fin | None, type, en cours d'après le flux instantané)
        self.ferm = []  # (station, type, début, fin | None)
        self.priced_now = set()  # stations affichant un prix gazole dans le flux instantané
        self.snapshot_read = False

    def read(self, stream, label, current=False, snapshot=False):
        s = self.src[label] = Counter()
        self.snapshot_read |= snapshot
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
            if el.get("cp"):
                m["cp"] = el.get("cp")
            if el.get("pop"):
                m["pop"] = el.get("pop")
            lat, lon = parse_coord(el.get("latitude")), parse_coord(el.get("longitude"))
            if lat and lon and 41 < lat < 52 and -6 < lon < 10:
                m["lat"], m["lon"] = lat, lon
            else:
                m["coord_brutes"] = (el.get("latitude"), el.get("longitude"))
            for tag in ("ville", "adresse"):
                t = el.findtext(tag)
                if t:
                    m[tag] = t.strip()
            for pr in el.iter("prix"):
                self.fuel_names[(pr.get("id"), pr.get("nom"))] += 1
                if pr.get("nom") != "Gazole" and pr.get("id") != "1":
                    continue
                s["gazole"] += 1
                d, v = parse_dt(pr.get("maj")), parse_val(pr.get("valeur"))
                if d is None or v is None:
                    k = "date illisible" if d is None else "prix illisible"
                    s[k] += 1
                    self.examples[k].append((label, pid, pr.get("maj"), pr.get("valeur")))
                    continue
                if snapshot:
                    self.priced_now.add(pid)
                t = mins(d)
                if t < self.since_m:
                    s["avant"] += 1
                    continue
                if t > self.now_m + 60:
                    s["date future"] += 1
                    self.examples["date future"].append((label, pid, pr.get("maj"), pr.get("valeur")))
                if v < B.PRICE_MIN or v > B.PRICE_MAX:
                    k = "< 1 €" if v < B.PRICE_MIN else "> 3 €"
                    s[k] += 1
                    self.examples[k].append((label, pid, pr.get("maj"), pr.get("valeur")))
                self.ev.append((pid, t, v))
            for r in el.iter("rupture"):
                s["ruptures"] += 1
                if r.get("nom") != "Gazole" and r.get("id") != "1":
                    continue
                s["ruptures gazole"] += 1
                d, f = parse_dt(r.get("debut")), parse_dt(r.get("fin"))
                if d is None:
                    s["ruptures sans début"] += 1
                    continue
                s["ruptures gazole sans fin"] += f is None
                self.rupt.append((pid, mins(d), None if f is None else mins(f), r.get("type") or "", current))
            for f_el in el.iter("fermeture"):
                s["fermetures"] += 1
                d, f = parse_dt(f_el.get("debut")), parse_dt(f_el.get("fin"))
                if d is None:
                    s["fermetures vides"] += 1
                    continue
                self.ferm.append((pid, f_el.get("type") or "", mins(d), None if f is None else mins(f)))
            el.clear()


# --------------------------------------------------------------------------- chaîne de calcul indépendante


def dedup(ev):
    """Filtre de plage puis dédoublonnage : pour une même station et un même horodatage, le dernier lu l'emporte."""
    last, n_out, n_dup, n_conf = {}, 0, 0, 0
    for pid, t, v in ev:
        if v < B.PRICE_MIN or v > B.PRICE_MAX:
            n_out += 1
            continue
        k = (pid, t)
        old = last.get(k)
        if old is not None:
            n_dup += 1
            n_conf += old != v
        last[k] = v
    return last, n_out, n_dup, n_conf


def find_spikes(evs):
    """Indices des fautes de saisie corrigées ensuite, en un passage : un prix (ou deux prix successifs) à au
    moins 0,30 € du prix précédent, dans le même sens, suivi en moins de 72 h d'un prix plus proche du prix
    précédent que du dernier prix erroné."""
    out = set()
    for k in range(1, len(evs)):
        prev = evs[k - 1][1]
        for run in (1, 2):
            m = k + run - 1
            if m + 1 >= len(evs):
                break
            gaps = [evs[j][1] - prev for j in range(k, m + 1)]
            if not (all(g >= B.SPIKE_JUMP for g in gaps) or all(g <= -B.SPIKE_JUMP for g in gaps)):
                continue
            nxt = evs[m + 1][1]
            if abs(nxt - prev) < abs(nxt - evs[m][1]) and evs[m + 1][0] - evs[k][0] <= B.SPIKE_HOURS * 60:
                out.update(range(k, m + 1))
    return sorted(out)


def remove_spikes(evs):
    """Retire les fautes de saisie corrigées ensuite, passage après passage, jusqu'à ce qu'il n'y en ait plus."""
    removed = []
    while True:
        bad = find_spikes(evs)
        if not bad:
            return evs, removed
        removed += [(evs[k - 1] if k else None, evs[k], evs[k + 1] if k + 1 < len(evs) else None) for k in bad]
        bad = set(bad)
        evs = [e for k, e in enumerate(evs) if k not in bad]


def daily_medians(by_station, day0, now_m):
    """Médiane nationale à minuit, chaque jour, sans exclusion (référence du filtre des prix trop bas)."""
    days = list(range(day0, now_m + 1, DAY))
    per_day = [[] for _ in days]
    for evs in by_station.values():
        i, n = -1, len(evs)
        for k, d in enumerate(days):
            while i + 1 < n and evs[i + 1][0] <= d:
                i += 1
            if i >= 0 and d - evs[i][0] <= STALE:
                per_day[k].append(evs[i][1] / 1000.0)
    return days, [statistics.median(x) if x else None for x in per_day]


def build_intervals(R, clean, station_dep, now_m):
    """Intervalles [début, fin) sans vente de gazole, d'après les ruptures et fermetures des flux."""
    times = {pid: [t for t, _ in evs] for pid, evs in clean.items()}

    def next_after(pid, t):
        tl = times.get(pid, [])
        i = bisect_right(tl, t)
        return tl[i] if i < len(tl) else None

    out = defaultdict(list)
    recs = defaultdict(list)
    for pid, d, f, typ, cur in R.rupt:
        recs[(pid, d)].append((f, typ, cur))
    for (pid, d), lst in recs.items():
        if pid not in station_dep:
            continue
        cur = any(c for _, _, c in lst)
        fins = [f for f, _, c in lst if f is not None and not c]
        if cur:
            e = INF
        elif fins:
            e = fins[-1]
        else:
            nx = next_after(pid, d)
            e = nx if nx is not None else now_m
        if e > d:
            out[pid].append((d, e, f"rupture {lst[0][1] or '?'}" + (" en cours" if cur else "")))
    frecs = defaultdict(list)
    for pid, typ, d, f in R.ferm:
        frecs[(pid, typ, d)].append(f)
    for (pid, typ, d), fs in frecs.items():
        if pid not in station_dep:
            continue
        known = [f for f in fs if f is not None]
        if known:
            e = known[-1]
        else:
            nx = next_after(pid, d)
            e = nx if nx is not None else INF
        if e > d:
            out[pid].append((d, e, f"fermeture {typ or '?'}"))
    if R.snapshot_read:
        covered = {pid for pid, lst in out.items() for s, e, _ in lst if s <= now_m < e}
        for pid in station_dep:
            evs = clean.get(pid)
            if evs and now_m - evs[-1][0] <= STALE and pid not in R.priced_now and pid not in covered:
                out[pid].append((now_m, INF, "absente du flux instantané"))
    return out


class Prices:
    """Prix d'une station à un instant : dernier prix publié, s'il a au plus 30 jours et hors exclusion."""

    def __init__(self, by_station, intervals=None):
        self.t = {pid: [t for t, _ in evs] for pid, evs in by_station.items()}
        self.v = {pid: [v for _, v in evs] for pid, evs in by_station.items()}
        self.iv = intervals or {}

    def at(self, pid, t):
        tl = self.t.get(pid)
        if not tl:
            return None
        i = bisect_right(tl, t) - 1
        if i < 0 or t - tl[i] > STALE:
            return None
        for s, e, _ in self.iv.get(pid, ()):
            if s <= t < e:
                return None
        return self.v[pid][i]


def agg(vals):
    xs = [v / 1000 for v in vals if v is not None]
    if not xs:
        return 0, None, None
    return len(xs), statistics.median(xs), math.fsum(xs) / len(xs)


def to_arrays(by_station, ids):
    sid, ts, pr = [], [], []
    for pid in ids:
        for t, v in by_station.get(pid, ()):
            sid.append(pid)
            ts.append(t)
            pr.append(v)
    return np.asarray(sid, dtype=np.int64), np.asarray(ts, dtype=np.int64), np.asarray(pr, dtype=np.int64)


def excl_arrays(intervals, ids):
    row = {pid: i for i, pid in enumerate(ids)}
    rows, starts, ends = [], [], []
    for pid, lst in intervals.items():
        for s, e, _ in lst:
            rows.append(row[pid])
            starts.append(s)
            ends.append(e)
    return np.asarray(rows, dtype=np.int64), np.asarray(starts, dtype=np.int64), np.asarray(ends, dtype=np.int64)


# --------------------------------------------------------------------------- sections du rapport


def run(title, fn, C):
    p(f"## {title}")
    log(title)
    try:
        fn(C)
    except Exception:  # noqa: BLE001
        p("```")
        p(traceback.format_exc())
        p("```")
        check(False, f"La section « {title} » s'est interrompue sur une erreur.")
    p()


def s_lecture(C):
    R = C.R
    p("| Source | Stations | Prix gazole | Avant déc. 2024 | Date illisible | Prix illisible | Date future | < 1 € | > 3 € | Ruptures gazole (sans fin) | Fermetures datées |")
    p("|---|---|---|---|---|---|---|---|---|---|---|")
    tot = Counter()
    for k, s in R.src.items():
        tot.update(s)
        n_ferm = s["fermetures"] - s["fermetures vides"]
        p(f"| {k} | {s['pdv']} | {s['gazole']} | {s['avant']} | {s['date illisible']} | {s['prix illisible']} | {s['date future']} | {s['< 1 €']} | {s['> 3 €']} | {s['ruptures gazole']} ({s['ruptures gazole sans fin']}) | {n_ferm} |")
    p()
    p("Carburants rencontrés (id, nom) : " + ", ".join(f"{k}={v}" for k, v in sorted(R.fuel_names.items(), key=lambda x: -x[1])))
    for k, rows in R.examples.items():
        p(f"- {k} : {len(rows)} cas, par exemple {rows[:5]}")
    bad = sum(tot[k] for k in ("date illisible", "prix illisible", "date future", "< 1 €", "> 3 €"))
    check(bad == 0, f"Lecture des flux : {nf(sum(s['gazole'] for s in R.src.values()))} prix gazole lus, {bad} date ou prix illisible, futur ou hors de la plage 1–3 €.")


def s_nettoyage(C):
    R = C.R
    p(f"- Prix gazole lus après novembre 2024 : {nf(C.n_ev)} ; hors plage 1–3 € écartés : {C.n_out}.")
    p(f"- Doublons exacts (même station, même horodatage) supprimés : {nf(C.n_dup)}, dont {C.n_conf} avec des prix différents (le dernier lu est gardé).")
    # stations
    st_ids = C.st_ids
    same_ids = st_ids == sorted(C.station_dep)
    same_dep = same_ids and all(C.station_dep[i] == d for i, d in zip(st_ids, C.js["dep"]))
    p(f"- Stations de métropole (code postal valide) : {nf(len(C.station_dep))} ; liste et départements identiques à stations.json : {'oui' if same_dep else 'NON'}.")
    check(same_dep, f"Liste des stations et département de chacune : identiques au recalcul ({nf(len(st_ids))} stations).")
    with_ev = {pid for (pid, _t) in C.last}
    reasons = Counter()
    ex = defaultdict(list)
    for pid in with_ev - set(C.station_dep):
        cp = R.meta.get(pid, {}).get("cp", "")
        k = "outre-mer" if cp.startswith(("97", "98")) else f"code postal invalide ({cp!r})"
        reasons[k] += 1
        ex[k].append(pid)
    p(f"- Stations avec des prix gazole mais hors périmètre : {sum(reasons.values())} {dict(reasons)}.")
    no_coord = [i for i, la in zip(st_ids, C.js["lat"]) if la is None]
    act_last = {pid for pid in st_ids if C.full.at(pid, C.now_m) is not None}
    p(f"- Stations sans coordonnées valides (comptées dans les statistiques, absentes de la carte) : {len(no_coord)}, dont {len(set(no_coord) & act_last)} actives au dernier point : "
      + "; ".join(f"{i} {R.meta.get(i, {}).get('cp')} {R.meta.get(i, {}).get('ville')} {R.meta.get(i, {}).get('coord_brutes')}" for i in no_coord if i in act_last))
    # fautes de saisie
    st = C.build_stats
    p(f"- Fautes de saisie écartées (recalcul) : {len(C.spikes)} prix corrigés ensuite et {len(C.lows)} prix anormalement bas ; build.py : {st.get('spikes')} et {st.get('low')}.")
    check(len(C.spikes) == st.get("spikes") and len(C.lows) == st.get("low"),
          f"Fautes de saisie : {len(C.spikes)} prix corrigés ensuite et {len(C.lows)} prix trop bas écartés, comme dans le calcul publié.")
    # événements nettoyés : comparaison exacte avec build.py
    ours = {(pid, t, v) for pid, evs in C.clean.items() for t, v in evs}
    theirs = set(zip(C.npz["sid"].tolist(), C.npz["ts"].tolist(), C.npz["pr"].tolist()))
    d1, d2 = ours - theirs, theirs - ours
    p(f"- Prix après nettoyage : recalcul {nf(len(ours))}, build.py {nf(len(theirs))} ; présents d'un seul côté : {len(d1)} / {len(d2)}. Exemples : {sorted(d1)[:5]} / {sorted(d2)[:5]}")
    check(not d1 and not d2, f"Prix nettoyés : les {nf(len(ours))} prix retenus sont identiques à ceux du calcul publié.")
    # intervalles d'exclusion
    o = Counter((pid, s, e) for pid, lst in C.intervals.items() for s, e, _ in lst)
    rows, starts, ends = C.npz["excl_rows"], C.npz["excl_starts"], C.npz["excl_ends"]
    t_ = Counter((int(C.npz_stations[r]), int(s), int(min(e, INF))) for r, s, e in zip(rows, starts, ends))
    e1, e2 = o - t_, t_ - o
    p(f"- Intervalles de rupture ou fermeture : recalcul {nf(sum(o.values()))}, build.py {nf(sum(t_.values()))} ; différences : {sum(e1.values())} / {sum(e2.values())}. Exemples : {list(e1)[:4]} / {list(e2)[:4]}")
    check(not e1 and not e2, f"Ruptures et fermetures : les {nf(sum(o.values()))} périodes d'exclusion sont identiques à celles du calcul publié.")


def s_couverture(C):
    day_counts = Counter(t // DAY for (_pid, t) in C.last)
    days = sorted(day_counts)
    counts = [day_counts[d] for d in days]
    med = statistics.median(counts)
    full = set(range(days[0], days[-1] + 1))
    missing = sorted(full - set(days))
    low = [(dt_of(d * DAY).date().isoformat(), day_counts[d]) for d in days if day_counts[d] < 0.4 * med]
    p(f"- Jours couverts : {len(days)} du {dt_of(days[0] * DAY).date()} au {dt_of(days[-1] * DAY).date()} ; changements de prix par jour : médiane {int(med)}, min {min(counts)}, max {max(counts)}.")
    p(f"- Jours sans aucun changement de prix : {[dt_of(d * DAY).date().isoformat() for d in missing]}")
    p(f"- Jours creux (< 40 % de la médiane, surtout des dimanches) : {len(low)} ; les plus creux : {sorted(low, key=lambda x: x[1])[:6]}")
    p(f"- 10 derniers jours : {[(dt_of(d * DAY).date().isoformat(), day_counts[d]) for d in days[-10:]]}")
    check(not missing, f"Couverture : aucun jour sans données du {dt_of(days[0] * DAY).date()} au {dt_of(days[-1] * DAY).date()}.")
    last_ev = max(t for (_pid, t) in C.last)
    p(f"- Dernier prix reçu : {ts_txt(last_ev)} ; dernier point du calcul : {ts_txt(C.now_m)}.")
    for key in ("fev26", "2025"):
        n = C.naive[key]["FR"]["n"]
        pts = C.meta["periods"][key]["points"]
        jumps = [(pts[i], n[i - 1], n[i]) for i in range(1, len(n)) if abs(n[i] - n[i - 1]) > 0.03 * max(n[i - 1], 1)]
        p(f"- Stations actives ({key}) : min {min(n)} ({pts[n.index(min(n))]}), max {max(n)} ; variations de plus de 3 % d'un point à l'autre : {jumps[:10]}")


def s_recalcul(C):
    n_cmp = n_bad = 0
    worst = []
    for per, zs in C.naive.items():
        for z, a in zs.items():
            js = C.series[per][z]
            for k in range(len(a["n"])):
                n_cmp += 1
                bad = a["n"][k] != js["n"][k]
                for key in ("median", "mean"):
                    x, y = a[key][k], js[key][k]
                    if (x is None) != (y is None) or (x is not None and abs(x - y) > 6e-5):
                        bad = True
                if bad:
                    n_bad += 1
                    if len(worst) < 8:
                        worst.append((per, z, C.meta["periods"][per]["points"][k], a["n"][k], js["n"][k], a["median"][k], js["median"][k], a["mean"][k], js["mean"][k]))
    p(f"- Valeurs comparées (nombre de stations, médiane, moyenne) pour chaque période, chaque point et chaque zone (France, 13 régions, 96 départements) : {nf(n_cmp)} points ; écarts : {n_bad}.")
    for w in worst:
        p(f"  - {w}")
    check(n_bad == 0, f"Agrégats publiés (series.json) : identiques au recalcul indépendant sur {nf(n_cmp)} points × 3 valeurs (5 périodes, France, régions, départements).")
    # écarts par station (carte et tableau des baisses)
    n_d = bad_d = 0
    for per in C.meta["periods"]:
        first, last = C.vals[per][0], C.vals[per][-1]
        for i, js in enumerate(C.js["delta"][per]):
            n_d += 1
            mine = None if first[i] is None or last[i] is None else (last[i] - first[i]) / 10
            if (mine is None) != (js is None) or (mine is not None and abs(mine - js) > 0.051):
                bad_d += 1
    p(f"- Écarts par station (carte et tableau des baisses) : {nf(n_d)} valeurs comparées, {bad_d} différences.")
    check(bad_d == 0, f"Évolution de chaque station sur chaque période (carte, tableau des baisses) : identique au recalcul ({nf(n_d)} valeurs).")
    # dernier prix publié par station
    bad_p = 0
    for i, pid in enumerate(C.st_ids):
        evs = C.clean.get(pid)
        mine = (round(evs[-1][1] / 1000, 3), dt_of(evs[-1][0]).strftime("%Y-%m-%dT%H:%M")) if evs else (None, None)
        if mine != (C.js["prix"][i], C.js["maj"][i]):
            bad_p += 1
    p(f"- Dernier prix et date de chaque station : {bad_p} différences sur {nf(len(C.st_ids))}.")
    check(bad_p == 0, "Dernier prix et date de mise à jour de chaque station : identiques au recalcul.")
    fr_ = C.naive["fev26"]["FR"]
    p(f"- Dernier point ({ts_txt(C.now_m)}) : {nf(fr_['n'][-1])} stations actives, médiane {fr(fr_['median'][-1])}, moyenne {fr(fr_['mean'][-1])}.")
    last = sorted(v / 1000 for v in C.vals["fev26"][-1] if v is not None)
    q = [last[min(len(last) - 1, int(round(x * (len(last) - 1))))] for x in (0, 0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99, 1)]
    p(f"- Distribution des prix au dernier point (min, p1, p5, p25, p50, p75, p95, p99, max) : {[round(x, 3) for x in q]}")
    # tableau des baisses, France
    for per in ("7j", "3m"):
        first, lastv = C.vals[per][0], C.vals[per][-1]
        d = [(b - a) for a, b in zip(first, lastv) if a is not None and b is not None]
        cnt = [sum(1 for x in d if x <= -k) for k in (50, 100, 150, 200)]
        p(f"- Tableau des baisses, France, {C.meta['periods'][per]['label']} : {nf(len(d))} stations avec un prix au début et à la fin ; baisse ≥ 0,05 € : {nf(cnt[0])} ({pct(cnt[0], len(d))}), ≥ 0,10 € : {nf(cnt[1])} ({pct(cnt[1], len(d))}), ≥ 0,15 € : {nf(cnt[2])} ({pct(cnt[2], len(d))}), ≥ 0,20 € : {nf(cnt[3])} ({pct(cnt[3], len(d))}).")


def s_fautes(C):
    R = C.R
    p(f"- Prix corrigés ensuite (à au moins 0,30 € du prix précédent, corrigés en moins de 72 h) écartés : {len(C.spikes)}. Les plus récents :")
    for pid, a, b, c in sorted(C.spikes, key=lambda x: -x[2][0])[:10]:
        m = R.meta.get(pid, {})
        p(f"  - {pid} ({m.get('cp')} {m.get('ville')}) : {fr(a[1] / 1000)} → {fr(b[1] / 1000)} le {ts_txt(b[0])} → {fr(c[1] / 1000)} le {ts_txt(c[0])}")
    tol = Counter("retour à 0,05 € près" if abs(c[1] - a[1]) <= 50 else "retour à plus de 0,05 €" for _pid, a, b, c in C.spikes)
    p(f"  - écart entre le prix de retour et le prix d'avant la faute : {dict(tol)}.")
    covered = sum(1 for _pid, _a, b, c in C.spikes if c[0] // DAY > b[0] // DAY)
    p(f"  - dont {covered} couvraient au moins un minuit (visibles dans les séries journalières).")
    p(f"- Prix inférieurs de plus de 0,50 € à la médiane nationale du jour écartés : {len(C.lows)}. Écart à la médiane : quantiles 5/50/95 % = {np.round(np.percentile([v / 1000 - r for _, _, v, r in C.lows], [5, 50, 95]), 3).tolist() if C.lows else []}. Les plus récents :")
    for pid, t, v, ref in sorted(C.lows, key=lambda x: -x[1])[:8]:
        m = R.meta.get(pid, {})
        p(f"  - {pid} ({m.get('cp')} {m.get('ville')}) : {fr(v / 1000)} le {ts_txt(t)} (médiane du jour {fr(ref)})")
    # contrôles résiduels après nettoyage
    resid_list = [(pid, k) for pid, evs in C.clean.items() for k in find_spikes(evs)]
    resid = len(resid_list)
    def ctx(pid, k, a=2, b=2):
        evs = C.clean[pid]
        return " → ".join(f"{fr(v / 1000)} ({ts_txt(t)})" for t, v in evs[max(0, k - a):k + b + 1])
    p(f"- Après nettoyage, prix corrigés ensuite restants : {resid}.")
    for pid, k in resid_list[:6]:
        m = R.meta.get(pid, {})
        p(f"  - {pid} ({m.get('cp')} {m.get('ville')}) : {ctx(pid, k)}")
    bigs = Counter()
    ex_big = defaultdict(list)
    for pid, evs in C.clean.items():
        for k in range(1, len(evs)):
            if abs(evs[k][1] - evs[k - 1][1]) < 500:
                continue
            back = next((j for j in range(k + 1, min(len(evs), k + 8)) if abs(evs[j][1] - evs[k - 1][1]) <= 50), None)
            gap = (evs[k][0] - evs[k - 1][0]) / DAY
            if back is None:
                lab = "durable, après plus de 30 jours sans mise à jour" if gap > 30 else "durable"
            else:
                lab = "annulé en moins de 7 jours" if evs[back][0] - evs[k][0] <= 7 * DAY else "annulé après plus de 7 jours"
            bigs[lab] += 1
            ex_big[lab].append((evs[k][0], pid, k))
    p(f"- Après nettoyage, changements d'au moins 0,50 € d'un coup : {sum(bigs.values())} {dict(bigs)}.")
    for lab, lst in ex_big.items():
        for _t, pid, k in sorted(lst, reverse=True)[:4]:
            m = R.meta.get(pid, {})
            p(f"  - {lab} : {pid} ({m.get('cp')} {m.get('ville')}, {'autoroute' if m.get('pop') == 'A' else 'route'}) : {ctx(pid, k, 1, 2)}")
    pts = C.meta["periods"]["fev26"]["points"]
    med = C.naive["fev26"]["FR"]["median"]
    pop = [R.meta.get(pid, {}).get("pop", "") for pid in C.st_ids]
    cnt = Counter()
    for k, row in enumerate(C.vals["fev26"]):
        for i, v in enumerate(row):
            if v is None:
                continue
            dv = v / 1000 - med[k]
            lab = "autoroute" if pop[i] == "A" else "route"
            if dv < -0.5:
                cnt[("< −0,50", lab)] += 1
            elif dv < -0.4:
                cnt[("−0,50 à −0,40", lab)] += 1
            if dv > 0.5:
                cnt[("> +0,50", lab)] += 1
    p(f"- Après nettoyage, écarts à la médiane nationale (stations × jours, depuis février 2026) : {dict(cnt)}.")
    hi = Counter()
    for k, row in enumerate(C.vals["fev26"]):
        for i, v in enumerate(row):
            if v is not None and pop[i] != "A" and v / 1000 - med[k] > 0.5:
                hi[i] += 1
    p("- Stations hors autoroute le plus souvent à plus de 0,50 € au-dessus de la médiane (jours, prix au dernier point) : "
      + "; ".join(f"{C.st_ids[i]} {R.meta.get(C.st_ids[i], {}).get('ville')} {n} j {fr(None if C.vals['fev26'][-1][i] is None else C.vals['fev26'][-1][i] / 1000)}" for i, n in hi.most_common(10)))
    check(cnt[("< −0,50", "route")] + cnt[("< −0,50", "autoroute")] == 0 and resid == 0,
          f"Après nettoyage : {resid} faute de saisie corrigée ensuite restante et {cnt[('< −0,50', 'route')] + cnt[('< −0,50', 'autoroute')]} prix à plus de 0,50 € sous la médiane nationale.")
    last = C.vals["fev26"][-1]
    out = [(i, v) for i, v in enumerate(last) if v is not None and abs(v / 1000 - med[-1]) > 0.40]
    p(f"- Au dernier point, {len(out)} stations s'écartent de plus de 0,40 € de la médiane ({fr(med[-1])}) :")
    for i, v in out[:15]:
        pid = C.st_ids[i]
        m = R.meta.get(pid, {})
        hist = [(ts_txt(t), v_ / 1000) for t, v_ in C.clean[pid][-3:]]
        p(f"  - {pid} {m.get('cp')} {m.get('ville')} ({'autoroute' if m.get('pop') == 'A' else 'route'}) : {fr(v / 1000)} ; derniers prix {hist}")
    _ = pts


def s_impact(C):
    """Effet de chaque étape du nettoyage sur les chiffres France (médiane, moyenne, nombre de stations)."""
    ids = np.asarray(C.st_ids, dtype=np.int64)
    raw = to_arrays(C.by, C.st_ids)
    typo = to_arrays(C.clean, C.st_ids)
    excl = excl_arrays(C.intervals, C.st_ids)
    p("| Période | Variante | Stations (dernier point) | Médiane (dernier point) | Moyenne (dernier point) | Écart max. médiane | Écart max. moyenne |")
    p("|---|---|---|---|---|---|---|")
    for per in ("48h", "3m", "fev26", "2025"):
        pts = np.asarray([mins(datetime.fromisoformat(t)) for t in C.meta["periods"][per]["points"]], dtype=np.int64)
        res = {}
        for lab, arr, ex in (("brut", raw, None), ("sans fautes de saisie", typo, None), ("publié (+ ruptures et fermetures)", typo, excl)):
            m = B.price_matrix(*arr, ids, pts, ex)
            res[lab] = (np.nanmedian(m, axis=0), np.nanmean(m, axis=0), np.sum(~np.isnan(m), axis=0))
        ref = res["publié (+ ruptures et fermetures)"]
        for lab, (md, mn, n) in res.items():
            dm = np.nanmax(np.abs(md - ref[0]))
            da = np.nanmax(np.abs(mn - ref[1]))
            p(f"| {per} | {lab} | {nf(int(n[-1]))} | {fr(float(md[-1]))} | {fr(float(mn[-1]))} | {fr(float(dm))} | {fr(float(da))} |")
            if lab == "brut" and per in ("fev26", "2025"):
                C.notes.append(f"{per} : écart max. médiane le {C.meta['periods'][per]['points'][int(np.nanargmax(np.abs(md - ref[0])))]}, "
                               f"moyenne le {C.meta['periods'][per]['points'][int(np.nanargmax(np.abs(mn - ref[1])))]}")
        if per == "fev26":
            nt, nr = res["sans fautes de saisie"][2], ref[2]
            top = np.argsort(-(nt - nr))[:6]
            C.notes.append("jours avec le plus de stations exclues (rupture, fermeture) : " + ", ".join(
                f"{C.meta['periods'][per]['points'][int(i)]} {int(nt[i] - nr[i])} (médiane {fr(float(res['sans fautes de saisie'][0][i]))} → {fr(float(ref[0][i]))}, moyenne {fr(float(res['sans fautes de saisie'][1][i]))} → {fr(float(ref[1][i]))})" for i in top))
        if per == "fev26":
            C.impact = {k: (float(np.nanmax(np.abs(v[0] - ref[0]))), float(np.nanmax(np.abs(v[1] - ref[1]))), float(np.mean(v[2] - ref[2]))) for k, v in res.items()}
    p()
    for note in C.notes:
        p(f"- {note}")
    b, t = C.impact["brut"], C.impact["sans fautes de saisie"]
    p(f"- Depuis février 2026, le nettoyage complet déplace la médiane France d'au plus {fr(b[0])} et la moyenne d'au plus {fr(b[1])} ; "
      f"les ruptures et fermetures retirent en moyenne {t[2]:.0f} stations par jour.")
    check(None, f"Effet du nettoyage depuis février 2026 : médiane France déplacée d'au plus {fr(b[0])}, moyenne d'au plus {fr(b[1])}.")


def s_ruptures(C):
    R = C.R
    kinds = Counter(k for lst in C.intervals.values() for _, _, k in lst)
    p(f"- Périodes d'exclusion par nature : {dict(kinds)}.")
    cur = {pid for pid, _d, _f, _t, c in R.rupt if c}
    cur_sel = cur & set(C.station_dep)
    would = {pid for pid in cur_sel if C.typo_only.at(pid, C.now_m) is not None}
    still = {pid for pid in cur_sel if C.full.at(pid, C.now_m) is not None}
    types = Counter(t for pid, _d, _f, t, c in R.rupt if c and pid in cur_sel)
    p(f"- Ruptures de gazole en cours (flux instantané) : {len(cur_sel)} stations de métropole {dict(types)} ; "
      f"{len(would)} auraient été comptées avec leur dernier prix ; encore comptées après correction : {len(still)}.")
    check(not still, f"Stations en rupture de gazole en ce moment : {len(cur_sel)}, toutes exclues du dernier point ({len(would)} étaient comptées avec leur ancien prix avant correction).")
    # prix publiés pendant une période d'exclusion (incohérences des flux)
    n_in = Counter()
    st_in = defaultdict(set)
    for pid, lst in C.intervals.items():
        tl = C.full.t.get(pid, [])
        for s, e, k in lst:
            n = bisect_right(tl, e - 1) - bisect_right(tl, s)
            if n:
                n_in[k] += n
                st_in[k].add(pid)
    p(f"- Prix publiés pendant une rupture ou une fermeture (incohérence des flux, la période d'exclusion prime) : {dict(n_in)} prix, stations concernées : { {k: len(v) for k, v in st_in.items()} }.")
    shown = Counter()
    for pid, lst in C.intervals.items():
        tl = C.full.t.get(pid, [])
        for s, e, k in lst:
            i, j = bisect_right(tl, s), bisect_right(tl, e - 1)
            if j > i and shown[k] < 3:
                shown[k] += 1
                p(f"  - {k} : {pid} ({R.meta.get(pid, {}).get('cp')} {R.meta.get(pid, {}).get('ville')}) du {ts_txt(s)} au {ts_txt(e)}, {j - i} prix publiés entre le {ts_txt(tl[i])} et le {ts_txt(tl[j - 1])}")
    # règle alternative : un prix publié met fin à la rupture ou à la fermeture
    alt = defaultdict(list)
    for pid, lst in C.intervals.items():
        tl = C.full.t.get(pid, [])
        for s, e, k in lst:
            i = bisect_right(tl, s)
            alt[pid].append((s, min(e, tl[i]) if i < len(tl) else e, k))
    ids = np.asarray(C.st_ids, dtype=np.int64)
    arr = to_arrays(C.clean, C.st_ids)
    pts = np.asarray([mins(datetime.fromisoformat(t)) for t in C.meta["periods"]["fev26"]["points"]], dtype=np.int64)
    m1 = B.price_matrix(*arr, ids, pts, excl_arrays(C.intervals, C.st_ids))
    m2 = B.price_matrix(*arr, ids, pts, excl_arrays(alt, C.st_ids))
    p(f"- Si un prix publié mettait fin à toute rupture ou fermeture : médiane France modifiée d'au plus {fr(float(np.nanmax(np.abs(np.nanmedian(m1, 0) - np.nanmedian(m2, 0)))))}, moyenne d'au plus {fr(float(np.nanmax(np.abs(np.nanmean(m1, 0) - np.nanmean(m2, 0)))))} depuis février 2026.")
    # effet de l'alignement sur le flux instantané au dernier point
    gone = [pid for pid, lst in C.intervals.items() for s, e, k in lst if k == "absente du flux instantané"]
    no_snap = {pid: [iv for iv in lst if iv[2] != "absente du flux instantané"] for pid, lst in C.intervals.items()}
    without = Prices(C.clean, no_snap)
    v_without = [without.at(pid, C.now_m) for pid in C.st_ids]
    v_with = C.vals["fev26"][-1]
    n0, md0, mn0 = agg(v_without)
    n1, md1, mn1 = agg(v_with)
    ruptured_open = sum(1 for pid in gone if any(k.startswith("rupture") and e == C.now_m for s, e, k in C.intervals[pid]))
    p(f"- Dernier point : {len(gone)} stations avec un prix de moins de 30 jours n'affichent pas de prix gazole dans le flux instantané (dont {ruptured_open} avec une rupture sans date de fin dans les flux de la veille) ; "
      f"elles ne sont pas comptées. Sans cette règle : {nf(n0)} stations, médiane {fr(md0)}, moyenne {fr(mn0)} ; avec : {nf(n1)}, {fr(md1)}, {fr(mn1)}.")
    long_ = Counter(k for lst in C.intervals.values() for s, e, k in lst if e < INF and e - s > 30 * DAY)
    p(f"- Périodes d'exclusion terminées de plus de 30 jours : {dict(long_)} ; sans fin connue (en cours) : {dict(Counter(k for lst in C.intervals.values() for s, e, k in lst if e >= INF))}.")
    # ancienneté des prix au dernier point
    ages = []
    for pid in C.st_ids:
        if C.full.at(pid, C.now_m) is not None:
            tl = C.full.t[pid]
            ages.append((C.now_m - tl[bisect_right(tl, C.now_m) - 1]) / DAY)
    p(f"- Âge du dernier prix des stations actives : médiane {statistics.median(ages):.1f} j ; > 2 j : {sum(a > 2 for a in ages)} ; > 7 j : {sum(a > 7 for a in ages)} ; > 14 j : {sum(a > 14 for a in ages)}.")
    for days in (7, 14):
        vals = [C.full.at(pid, C.now_m) for pid in C.st_ids]
        vals = [v for pid, v in zip(C.st_ids, vals) if v is not None and C.now_m - C.full.t[pid][bisect_right(C.full.t[pid], C.now_m) - 1] <= days * DAY]
        n, md, mn = agg(vals)
        p(f"- Avec un seuil d'activité de {days} jours au lieu de 30 : {nf(n)} stations, médiane {fr(md)}, moyenne {fr(mn)}.")


def s_geo(C):
    R = C.R
    act = [i for i, v in enumerate(C.vals["fev26"][-1]) if v is not None]
    cnt = Counter(C.js["dep"][i] for i in act)
    p(f"- Départements avec le moins de stations actives : {sorted((v, k) for k, v in cnt.items())[:8]}")
    p(f"- Corse : 2A = {cnt.get('2A', 0)}, 2B = {cnt.get('2B', 0)}.")
    lat = np.asarray(C.js["lat"], dtype=float)
    lon = np.asarray(C.js["lon"], dtype=float)
    coords = Counter((round(lat[i], 4), round(lon[i], 4)) for i in act if not math.isnan(lat[i]))
    dup = [(k, v) for k, v in coords.items() if v > 1]
    p(f"- Stations actives partageant exactement les mêmes coordonnées : {sum(v for _, v in dup)} ({len(dup)} positions).")
    try:
        from shapely.geometry import Point, shape
        from shapely.strtree import STRtree
    except ImportError:
        p("- Contrôle des positions impossible (shapely absent).")
        return
    feats = json.loads((C.data / "departements.geojson").read_text())["features"]
    geoms = [shape(f["geometry"]) for f in feats]
    codes = [f["properties"]["code"] for f in feats]
    by_code = dict(zip(codes, geoms))
    tree = STRtree(geoms)
    far, near = [], 0
    for i in act:
        if math.isnan(lat[i]):
            continue
        pt = Point(lon[i], lat[i])
        own = by_code.get(C.js["dep"][i])
        if own is None or own.covers(pt):
            continue
        dist = own.distance(pt)
        hits = [codes[j] for j in tree.query(pt) if geoms[j].covers(pt)]
        if dist > 0.02 and hits:
            far.append((C.st_ids[i], C.js["dep"][i], hits[0], round(dist, 3), R.meta.get(C.st_ids[i], {}).get("ville")))
        elif dist > 0.02:
            far.append((C.st_ids[i], C.js["dep"][i], "hors de tout département", round(dist, 3), R.meta.get(C.st_ids[i], {}).get("ville")))
        else:
            near += 1
    p(f"- Stations actives situées hors du département de leur code postal : {near} à moins de 2 km de la limite (contours simplifiés) et {len(far)} nettement à l'intérieur d'un autre département :")
    for f in far[:20]:
        p(f"  - {f}")
    check(None, f"Géographie : le département vient du code postal ; {len(far)} stations actives sont en réalité dans un département voisin (communes limitrophes).")


def s_api(C):
    raw = B.fetch(API_CSV, None, keep=False).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw), delimiter=";"))
    cols = list(rows[0].keys()) if rows else []
    col = {c.lower(): c for c in cols}
    p(f"- {len(rows)} lignes ; colonnes gazole : {[c for c in cols if 'gazole' in c.lower()]}")
    def api_dt(s):
        # L'export affiche l'heure du flux (heure de Paris) suivie de « +00:00 » : on garde l'heure telle quelle.
        try:
            return datetime.fromisoformat((s or "").strip()).replace(tzinfo=None)
        except ValueError:
            return None

    api = {}
    for r in rows:
        v = r.get(col["gazole_prix"])
        if not v:
            continue
        api[int(r[col["id"]])] = (r[col.get("cp", col.get("code_postal", "cp"))], float(v), api_dt(r[col["gazole_maj"]]))
    p(f"- Format des dates de l'API : {[r[col['gazole_maj']] for r in rows[:2]]}")
    both = [int(r[col["id"]]) for r in rows if r.get(col["gazole_prix"]) and r.get(col.get("gazole_rupture_debut", ""), "")]
    p(f"- Lignes de l'API avec à la fois un prix et une rupture de gazole : {len(both)} ; exemples {both[:6]}")
    known = set(C.st_ids)
    common = [i for i in api if i in known and C.clean.get(i)]
    raw_last = {}
    for (pid, t), v in C.last.items():
        if pid in known and (pid not in raw_last or t > raw_last[pid][0]):
            raw_last[pid] = (t, v)
    same, typo, timing, other = 0, [], [], []
    for i in common:
        t, v = C.clean[i][-1]
        at = mins(api[i][2]) if api[i][2] else None
        if abs(api[i][1] - v / 1000) <= 0.0015 and at is not None and abs(at - t) <= 1:
            same += 1
        elif at is not None and abs(at - raw_last[i][0]) <= 1 and abs(api[i][1] - raw_last[i][1] / 1000) <= 0.0015:
            typo.append(i)  # l'API affiche la faute de saisie que nous avons écartée
        elif at is None or abs(at - t) > 1:
            timing.append((i, api[i][1], str(api[i][2]), v / 1000, ts_txt(t)))
        else:
            other.append((i, api[i][1], str(api[i][2]), v / 1000, ts_txt(t)))
    p(f"- Stations avec un prix gazole dans l'API : {nf(len(api))} ; présentes chez nous : {nf(len(common))} ; même prix à la même minute : {nf(same)} ; "
      f"faute de saisie écartée chez nous mais affichée par l'API : {len(typo)} {typo[:8]} ; mises à jour différentes (l'une des deux sources est plus récente) : {len(timing)} ; même mise à jour mais prix différent : {len(other)}.")
    for d in (other + timing)[:10]:
        p(f"  - {d}")
    check(not other, f"Export officiel (data.economie.gouv.fr) : même prix à la même minute pour {nf(same)} stations sur {nf(len(common))} ; "
          f"{len(typo)} faute(s) de saisie écartée(s) chez nous, {len(timing)} décalage(s) de mise à jour, {len(other)} écart inexpliqué.")
    cutoff = C.now_m - STALE
    A = {i for i, (cp, v, dt) in api.items() if dep_from_cp(cp) and dt and mins(dt) >= cutoff and 1 <= v <= 3}
    S = {pid for pid, v in zip(C.st_ids, C.vals["fev26"][-1]) if v is not None}
    na, ma, aa = agg([int(round(api[i][1] * 1000)) for i in A])
    ns, ms, as_ = agg([v for v in C.vals["fev26"][-1]])
    p(f"- Stations actives au même instant : API {nf(len(A))} (métropole, prix de moins de 30 jours), nous {nf(len(S))} ; communes : {nf(len(A & S))} ; seulement API : {len(A - S)} ; seulement nous : {len(S - A)}.")
    p(f"- Médiane / moyenne : API {fr(ma)} / {fr(aa)} ; nous {fr(ms)} / {fr(as_)}.")
    only_us = sorted(S - A)
    why = Counter()
    for i in only_us:
        if i not in api:
            why["sans prix gazole dans l'API"] += 1
        else:
            why["prix de plus de 30 jours ou hors plage dans l'API"] += 1
    p(f"- Stations comptées chez nous mais pas dans l'API : {dict(why)} ; exemples : {[(i, C.R.meta.get(i, {}).get('ville'), ts_txt(C.full.t[i][-1])) for i in only_us[:6]]}")
    only_api = sorted(A - S)
    p(f"- Stations comptées dans l'API mais pas chez nous : exemples {[(i, api[i][1], str(api[i][2])) for i in only_api[:6]]}")
    check(abs(ma - ms) < 0.0015 and abs(aa - as_) < 0.005,
          f"Dernier point comparé à l'export officiel : médiane {fr(ms)} contre {fr(ma)}, moyenne {fr(as_)} contre {fr(aa)} ({nf(len(S))} stations contre {nf(len(A))}).")


def s_dgec(C):
    p("Prix publiés par la DGEC (pondérés par les réseaux, relevés le vendredi) comparés à nos valeurs non pondérées, à 12 h le vendredi :")
    p()
    p("| Vendredi | DGEC | Notre moyenne | Écart | Notre médiane | Écart | Moyenne à 0 h / samedi 0 h |")
    p("|---|---|---|---|---|---|---|")
    gaps, mgaps = [], []
    for d, ref in sorted(DGEC.items()):
        t0 = mins(datetime.fromisoformat(d))
        if t0 > C.now_m:
            continue
        res = {}
        for lab, t in (("0h", t0), ("12h", t0 + 720), ("sam", t0 + DAY)):
            res[lab] = agg([C.full.at(pid, t) for pid in C.st_ids])
        _, md, mn = res["12h"]
        gaps.append(mn - ref)
        mgaps.append(md - ref)
        p(f"| {d} | {fr(ref)} | {fr(mn)} | {sg(mn - ref)} | {fr(md)} | {sg(md - ref)} | {fr(res['0h'][2])} / {fr(res['sam'][2])} |")
    p()
    if gaps:
        p(f"- Écart moyen de notre moyenne à la DGEC : {sg(statistics.mean(gaps))} (de {sg(min(gaps))} à {sg(max(gaps))}) sur {len(gaps)} semaines ; "
          f"de notre médiane : {sg(statistics.mean(mgaps))} (de {sg(min(mgaps))} à {sg(max(mgaps))}).")
        check(None, f"Prix hebdomadaires de la DGEC (pondérés par les volumes), {len(gaps)} semaines : écart de notre moyenne non pondérée {sg(statistics.mean(gaps))} en moyenne (de {sg(min(gaps))} à {sg(max(gaps))}), de notre médiane {sg(statistics.mean(mgaps))} (de {sg(min(mgaps))} à {sg(max(mgaps))}).")


def s_complements(C):
    pts = C.meta["periods"]["fev26"]["points"]
    rows = C.vals["fev26"]
    share = []
    for k in range(0, len(pts), 7):
        xs = [v for v in rows[k] if v is not None]
        share.append(f"{pts[k][:10]} {pct(sum(v == 2250 for v in xs), len(xs))}")
    xs = [v for v in rows[-1] if v is not None]
    top = Counter(xs).most_common(6)
    p(f"- Prix les plus fréquents au dernier point (prix, nombre de stations) : {[(v / 1000, n) for v, n in top]}")
    p("- Part des stations actives à exactement 2,250 € (un point par semaine) : " + ", ".join(share) + f", dernier point {pct(sum(v == 2250 for v in xs), len(xs))}.")
    n_cap = sum(v == 2250 for v in xs)
    below = sum(v < 2250 for v in xs)
    p(f"- Au dernier point : {nf(below)} stations sous 2,250 €, {nf(n_cap)} à 2,250 €, {nf(len(xs) - below - n_cap)} au-dessus.")
    if below < len(xs) / 2 <= below + n_cap:
        check(None, f"La médiane nationale tombe dans le groupe des {nf(n_cap)} stations affichant exactement 2,250 € ({pct(n_cap, len(xs))}) : elle reste collée à ce prix tant qu'il couvre le milieu de la distribution.")
    p("- Moyennes hebdomadaires France (lundi → dimanche, moyenne des valeurs journalières à minuit) :")
    weeks = defaultdict(list)
    fr_ = C.naive["fev26"]["FR"]
    for t, mn, md in zip(pts, fr_["mean"], fr_["median"]):
        dtt = datetime.fromisoformat(t)
        weeks[(dtt - timedelta(days=dtt.weekday())).date().isoformat()].append((mn, md))
    for wk in sorted(weeks)[-8:]:
        v = weeks[wk]
        p(f"  - semaine du {wk} : moyenne {fr(statistics.mean(x for x, _ in v))}, médiane {fr(statistics.mean(y for _, y in v))} ({len(v)} points)")


# --------------------------------------------------------------------------- programme principal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="_site/data")
    ap.add_argument("--cache", default=".cache")
    ap.add_argument("--events", required=True, help="fichier npz écrit par build.py --dump-events")
    ap.add_argument("--snapshot", help="dossier des flux copiés par build.py --snapshot")
    ap.add_argument("--local-xml", nargs="*", help="fichiers XML locaux (tests)")
    args = ap.parse_args()
    if args.snapshot:
        B.SNAPSHOT = Path(args.snapshot)
    C = SimpleNamespace()
    C.data = data = Path(args.data)
    cache = Path(args.cache)
    C.meta = json.loads((data / "meta.json").read_text())
    C.series = json.loads((data / "series.json").read_text())
    C.js = json.loads((data / "stations.json").read_text())
    C.npz = np.load(args.events)
    C.build_stats = json.loads(str(C.npz["stats"]))
    C.npz_stations = C.npz["stations"]
    C.now_m = int(C.npz["now"])
    C.st_ids = [int(x) for x in C.js["id"]]
    assert ts_txt(C.now_m).replace(" ", "T") == C.meta["data_until"], (ts_txt(C.now_m), C.meta["data_until"])

    # ---------------------------------------------------------------- lecture brute
    log("lecture des flux")
    C.R = R = Raw(mins(B.HISTORY_START), C.now_m)
    if args.local_xml:
        for f in args.local_xml:
            with open(f, "rb") as fh:
                R.read(fh, f, current="instantane_ruptures" in Path(f).name, snapshot="instantane" in Path(f).name)
    else:
        for name in C.build_stats["sources"]:
            R.read(B.xml_from_zip(B.fetch(f"{B.BASE}/{name}", cache, keep=name.startswith("annee/"))), name,
                   current=name == "instantane_ruptures", snapshot=name.startswith("instantane"))

    # ---------------------------------------------------------------- chaîne indépendante
    log("nettoyage indépendant")
    C.last, C.n_out, C.n_dup, C.n_conf = dedup(R.ev)
    C.n_ev = len(R.ev)
    R.ev = None  # libère la mémoire
    C.station_dep = {pid: d for pid, m in R.meta.items() if (d := dep_from_cp(m.get("cp")))}
    C.by = defaultdict(list)
    for (pid, t), v in C.last.items():
        if pid in C.station_dep:
            C.by[pid].append((t, v))
    for evs in C.by.values():
        evs.sort()
    C.spikes, nospike = [], {}
    for pid, evs in C.by.items():
        nospike[pid], rem = remove_spikes(evs)
        C.spikes += [(pid, *r) for r in rem]
    day0 = mins(B.HISTORY_START)
    days, med = daily_medians(nospike, day0, C.now_m)
    C.lows, C.clean = [], {}
    for pid, evs in nospike.items():
        kept = []
        for t, v in evs:
            ref = med[min(max((t - day0) // DAY, 0), len(days) - 1)]
            if ref is not None and v / 1000.0 < ref - B.LOW_OUTLIER:
                C.lows.append((pid, t, v, ref))
            else:
                kept.append((t, v))
        C.clean[pid], rem = remove_spikes(kept)
        C.spikes += [(pid, *r) for r in rem]
    C.intervals = build_intervals(R, C.clean, C.station_dep, C.now_m)
    C.full = Prices(C.clean, C.intervals)
    C.notes = []
    C.typo_only = Prices(C.clean)

    log("prix de chaque station à chaque point")
    zones = {"FR": list(range(len(C.st_ids)))}
    for r, deps in REGIONS.items():
        zones[f"R{r}"] = [i for i, d in enumerate(C.js["dep"]) if d in deps]
    for d in sorted(set(C.js["dep"])):
        zones[f"D{d}"] = [i for i, x in enumerate(C.js["dep"]) if x == d]
    C.vals, C.naive = {}, {}
    for per, pm in C.meta["periods"].items():
        pts = [mins(datetime.fromisoformat(t)) for t in pm["points"]]
        rows = [[C.full.at(pid, t) for pid in C.st_ids] for t in pts]
        C.vals[per] = rows
        C.naive[per] = {}
        for z, members in zones.items():
            a = {"n": [], "median": [], "mean": []}
            for row in rows:
                n, md, mn = agg([row[i] for i in members])
                a["n"].append(n)
                a["median"].append(md)
                a["mean"].append(mn)
            C.naive[per][z] = a

    regs_ok = sorted(DEP_TO_REG) == sorted(METRO) and len(REGIONS) == 13
    check(regs_ok, "Table départements → régions : 96 départements, 13 régions, chaque département dans une seule région.")

    for title, fn in (
        ("1. Lecture des flux", s_lecture),
        ("2. Nettoyage et contre-vérification du calcul", s_nettoyage),
        ("3. Couverture dans le temps", s_couverture),
        ("4. Recalcul indépendant des chiffres publiés", s_recalcul),
        ("5. Fautes de saisie", s_fautes),
        ("6. Effet du nettoyage sur les chiffres France", s_impact),
        ("7. Ruptures, fermetures et ancienneté des prix", s_ruptures),
        ("8. Géographie", s_geo),
        ("9. Comparaison avec l'export officiel data.economie.gouv.fr (flux instantané v2)", s_api),
        ("10. Comparaison avec les prix hebdomadaires de la DGEC", s_dgec),
        ("11. Compléments", s_complements),
    ):
        run(title, fn, C)

    print("# Audit des données gazole")
    print(f"Données jusqu'au {C.meta['data_until']}, calculées le {C.meta['generated_at']}, audit du {datetime.now(B.PARIS).strftime('%Y-%m-%dT%H:%M')}.")
    print()
    print("## Synthèse des contrôles")
    for mark, text in CHECKS:
        print(f"- {mark} {text}")
    print()
    print("\n".join(LINES))


if __name__ == "__main__":
    main()
