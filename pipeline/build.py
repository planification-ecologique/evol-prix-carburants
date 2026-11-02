"""Construit les données statiques de la page « Évolution du prix du gazole ».

Sources (open data prix-carburants.gouv.fr, Licence Ouverte 2.0) :
  - archives annuelles  https://donnees.roulez-eco.fr/opendata/annee/AAAA
  - année en cours (J-1) https://donnees.roulez-eco.fr/opendata/annee
  - flux quotidiens      https://donnees.roulez-eco.fr/opendata/jour[/AAAAMMJJ]
  - flux instantané      https://donnees.roulez-eco.fr/opendata/instantane

Toutes les dates du flux sont en heure de Paris : on travaille en heure locale
« naïve », convertie en minutes depuis 1970.

Usage : python pipeline/build.py --out _site/data [--cache .cache] [--now 2026-11-02T14:00]
        python pipeline/build.py --local-xml fichier1.xml ... (tests, sans réseau)
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
from collections import Counter
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from geo import DEP_TO_REG, REGIONS, departement  # noqa: E402

BASE = "https://donnees.roulez-eco.fr/opendata"
GEOJSON = "https://raw.githubusercontent.com/gregoiredavid/france-geojson/master/{}-version-simplifiee.geojson"
PARIS = ZoneInfo("Europe/Paris")
EPOCH = datetime(1970, 1, 1)

HISTORY_START = datetime(2024, 12, 1)  # un mois avant 2025 pour connaître les prix au 1er janvier
STALE_DAYS = 30  # un prix non mis à jour depuis plus de 30 jours n'est plus compté
PRICE_MIN, PRICE_MAX = 1000, 3000  # millièmes d'euro : on garde 1 à 3 €/L
# Fautes de saisie corrigées ensuite : saut d'au moins 0,30 € annulé (à 0,05 € près) dans les 72 h
SPIKE_JUMP, SPIKE_BACK, SPIKE_HOURS = 300, 50, 72
# Prix inférieur de plus de 0,50 € à la médiane nationale du jour : faute de saisie (ex. 1,25 € au lieu de 2,25 €)
LOW_OUTLIER = 0.50
FAR_FUTURE = 1 << 40


def minutes(dt: datetime) -> int:
    return int((dt - EPOCH).total_seconds() // 60)


def from_minutes(m: int) -> datetime:
    return EPOCH + timedelta(minutes=int(m))


# --------------------------------------------------------------------------- téléchargement


# Si défini, chaque fichier téléchargé y est copié, et relu tel quel ensuite (l'audit relit ainsi
# exactement les mêmes flux que le calcul, même si le flux instantané a changé entre-temps).
SNAPSHOT: Path | None = None


def fetch(url: str, cache: Path | None, keep: bool) -> bytes:
    """Télécharge url. Si keep, le fichier est mis en cache (archives immuables)."""
    name = url.removeprefix(BASE + "/").replace("/", "_") or "root"
    path = cache / name if cache else None
    if keep and path and path.exists():
        return path.read_bytes()
    snap = SNAPSHOT / name if SNAPSHOT and url.startswith(BASE) else None
    if snap and snap.exists():
        return snap.read_bytes()
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "evol-prix-carburants"})
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
            if keep and path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            if snap:
                snap.parent.mkdir(parents=True, exist_ok=True)
                snap.write_bytes(data)
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"Échec du téléchargement {url}: {last}")


def xml_from_zip(data: bytes) -> io.BytesIO:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
        return io.BytesIO(z.read(name))


# --------------------------------------------------------------------------- lecture XML


def parse_maj(s: str) -> datetime | None:
    s = (s or "").strip().replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def parse_price(s: str) -> int | None:
    """Prix en millièmes d'euro. Anciens fichiers : '1749', récents : '1.749'."""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if v < 100:
        v *= 1000
    return int(round(v))


def parse_coord(s: str) -> float | None:
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if abs(v) > 1000:
        v /= 100000
    return v


class Collector:
    def __init__(self) -> None:
        self.sid: list[int] = []
        self.ts: list[int] = []
        self.price: list[int] = []
        self.meta: dict[int, dict] = {}
        # ruptures de gazole : (station, début) -> [fin | None, type, en cours selon le flux instantané]
        self.rupt: dict[tuple[int, int], list] = {}
        # fermetures : (station, type, début) -> fin | None
        self.ferm: dict[tuple[int, str, int], int | None] = {}

    def read(self, stream, since: datetime, label: str, current: bool = False) -> None:
        """current=True pour le flux instantané avec ruptures : ses ruptures sont en cours maintenant."""
        n_before = len(self.ts)
        since_m = minutes(since)
        for _, el in ET.iterparse(stream, events=("end",)):
            if el.tag != "pdv":
                continue
            try:
                pid = int(el.get("id"))
            except (TypeError, ValueError):
                el.clear()
                continue
            lat, lon = parse_coord(el.get("latitude")), parse_coord(el.get("longitude"))
            cp = el.get("cp") or ""
            m = self.meta.setdefault(pid, {})
            if lat and lon and 41 < lat < 52 and -6 < lon < 10:
                m["lat"], m["lon"] = round(lat, 5), round(lon, 5)
            if cp:
                m["cp"] = cp
            for tag in ("adresse", "ville"):
                t = el.findtext(tag)
                if t:
                    m[tag] = t.strip()
            for p in el.iter("prix"):
                if p.get("nom") != "Gazole" and p.get("id") != "1":
                    continue
                dt, v = parse_maj(p.get("maj")), parse_price(p.get("valeur"))
                if dt is None or v is None:
                    continue
                t = minutes(dt)
                if t < since_m:
                    continue
                self.sid.append(pid)
                self.ts.append(t)
                self.price.append(v)
            for r in el.iter("rupture"):
                if r.get("nom") != "Gazole" and r.get("id") != "1":
                    continue
                d = parse_maj(r.get("debut") or "")
                if d is None:
                    continue
                f = parse_maj(r.get("fin") or "")
                fin = minutes(f) if f else None
                key = (pid, minutes(d))
                prev = self.rupt.get(key)
                if prev is None:
                    self.rupt[key] = [None if current else fin, r.get("type") or "", current]
                elif current:
                    prev[0], prev[2] = None, True
                elif fin is not None:
                    prev[0] = fin  # une date de fin connue l'emporte
            for f_el in el.iter("fermeture"):
                d = parse_maj(f_el.get("debut") or "")
                if d is None:
                    continue
                f = parse_maj(f_el.get("fin") or "")
                key = (pid, f_el.get("type") or "", minutes(d))
                fin = minutes(f) if f else None
                if key not in self.ferm or fin is not None:
                    self.ferm[key] = fin
            el.clear()
        print(f"  {label}: {len(self.ts) - n_before} prix gazole", flush=True)

    def arrays(self):
        sid = np.asarray(self.sid, dtype=np.int64)
        ts = np.asarray(self.ts, dtype=np.int64)
        pr = np.asarray(self.price, dtype=np.int64)
        ok = (pr >= PRICE_MIN) & (pr <= PRICE_MAX)
        sid, ts, pr = sid[ok], ts[ok], pr[ok]
        order = np.lexsort((ts, sid))
        sid, ts, pr = sid[order], ts[order], pr[order]
        # dédoublonnage (même station, même horodatage : on garde le dernier lu)
        keep = np.ones(len(sid), bool)
        keep[:-1] = (sid[1:] != sid[:-1]) | (ts[1:] != ts[:-1])
        return sid[keep], ts[keep], pr[keep]


# --------------------------------------------------------------------------- nettoyage


def drop_spikes(sid, ts, pr):
    """Masque des prix à garder : écarte les fautes de saisie corrigées ensuite (aller-retour)."""
    keep = np.ones(len(sid), bool)
    if len(sid) < 3:
        return keep
    same3 = (sid[1:-1] == sid[:-2]) & (sid[2:] == sid[1:-1])
    spike = (
        same3
        & (np.abs(pr[1:-1] - pr[:-2]) >= SPIKE_JUMP)
        & (np.abs(pr[2:] - pr[:-2]) <= SPIKE_BACK)
        & ((ts[2:] - ts[1:-1]) <= SPIKE_HOURS * 60)
    )
    keep[1:-1] = ~spike
    return keep


def drop_low_outliers(sid, ts, pr, stations, now: datetime):
    """Masque des prix à garder : écarte ceux inférieurs de plus de LOW_OUTLIER € à la médiane nationale du jour."""
    days = np.arange(minutes(HISTORY_START), minutes(now) + 1, 1440, dtype=np.int64)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(price_matrix(sid, ts, pr, stations, days), axis=0)
    d = np.clip((ts - days[0]) // 1440, 0, len(days) - 1)
    ref = med[d]
    return ~(np.isfinite(ref) & (pr / 1000.0 < ref - LOW_OUTLIER))


def exclusion_intervals(col: Collector, sid, ts, stations, now_m: int):
    """Intervalles [début, fin) pendant lesquels une station ne vend pas de gazole (rupture ou fermeture).

    Une rupture ou une fermeture sans date de fin prend fin au premier prix publié ensuite
    (une station qui publie un prix vend à nouveau du gazole), sauf rupture encore en cours
    d'après le flux instantané.
    """
    big = np.int64(1) << 32
    keys = sid * big + ts

    def next_price(pid, t):
        i = int(np.searchsorted(keys, pid * big + t, side="right"))
        return int(ts[i]) if i < len(sid) and sid[i] == pid else None

    rows, starts, ends = [], [], []
    stats = Counter()

    def add(pid, s, e, kind):
        r = int(np.searchsorted(stations, pid))
        if r >= len(stations) or stations[r] != pid or (e is not None and e <= s):
            return
        rows.append(r)
        starts.append(s)
        ends.append(FAR_FUTURE if e is None else e)
        stats[kind] += 1

    for (pid, s), (fin, typ, current) in col.rupt.items():
        if fin is None and not current:
            nxt = next_price(pid, s)
            fin = nxt if nxt is not None else now_m  # terminée avant maintenant, date exacte inconnue
        add(pid, s, fin, f"rupture {typ or '?'}{' en cours' if current else ''}")
    for (pid, typ, s), fin in col.ferm.items():
        if fin is None:
            fin = next_price(pid, s)
        add(pid, s, fin, f"fermeture {typ or '?'}")
    return (np.asarray(rows, dtype=np.int64), np.asarray(starts, dtype=np.int64), np.asarray(ends, dtype=np.int64)), stats


# --------------------------------------------------------------------------- périodes


def periods(now: datetime):
    """Points de calcul de chaque période : début, fins de pas, puis « maintenant »."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def series(start, step, label, unit):
        pts, t = [], start
        while t < now:
            pts.append(t)
            t += step
        pts.append(now)
        return {"label": label, "step": unit, "points": pts}

    h48_start = (now - timedelta(hours=48)).replace(minute=0, second=0, microsecond=0)
    return {
        "48h": series(h48_start, timedelta(hours=1), "48 dernières heures", "heure"),
        "7j": series(today - timedelta(days=7), timedelta(days=1), "7 derniers jours", "jour"),
        "3m": series(today - timedelta(days=91), timedelta(days=1), "3 derniers mois", "jour"),
        "fev26": series(datetime(2026, 2, 1), timedelta(days=1), "Depuis février 2026", "jour"),
        "2025": series(datetime(2025, 1, 1), timedelta(days=7), "Depuis janvier 2025", "semaine"),
    }


def price_matrix(sid, ts, pr, stations, points_m, excl=None):
    """prix[i, k] : dernier prix connu de stations[i] à l'instant points_m[k].

    NaN si la station n'a pas de prix, si ce prix date de plus de STALE_DAYS jours,
    ou si l'instant tombe dans un intervalle d'exclusion (rupture, fermeture).
    """
    big = np.int64(1) << 32
    keys = sid * big + ts
    q = (stations[:, None] * big + points_m[None, :]).ravel()
    idx = np.searchsorted(keys, q, side="right") - 1
    st_q = np.repeat(stations, len(points_m))
    t_q = np.tile(points_m, len(stations))
    valid = idx >= 0
    idx_c = np.where(valid, idx, 0)
    valid &= sid[idx_c] == st_q
    valid &= (t_q - ts[idx_c]) <= STALE_DAYS * 24 * 60
    out = np.where(valid, pr[idx_c] / 1000.0, np.nan).reshape(len(stations), len(points_m))
    if excl is not None and len(excl[0]):
        rows, starts, ends = excl
        a = np.searchsorted(points_m, starts, side="left")
        b = np.searchsorted(points_m, ends, side="left")
        for r, i, j in zip(rows.tolist(), a.tolist(), b.tolist()):
            if j > i:
                out[r, i:j] = np.nan
    return out


def aggregate(mat):
    with np.errstate(all="ignore"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mean = np.nanmean(mat, axis=0)
            med = np.nanmedian(mat, axis=0)
    n = np.sum(~np.isnan(mat), axis=0)

    def clean(a):
        return [None if np.isnan(x) else round(float(x), 4) for x in a]

    return {"mean": clean(mean), "median": clean(med), "n": [int(x) for x in n]}


# --------------------------------------------------------------------------- principal


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site/data")
    ap.add_argument("--cache", default=".cache")
    ap.add_argument("--now", help="heure de Paris, format ISO (tests)")
    ap.add_argument("--local-xml", nargs="*", help="fichiers XML locaux au lieu du téléchargement")
    ap.add_argument("--no-geojson", action="store_true")
    ap.add_argument("--dump-events", help="enregistre les prix nettoyés et les exclusions (npz) pour l'audit")
    ap.add_argument("--snapshot", help="dossier où copier les flux téléchargés, pour que l'audit relise les mêmes")
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(PARIS).replace(tzinfo=None)
    now = now.replace(second=0, microsecond=0)
    out = Path(args.out)
    (out / "stations").mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache)

    if args.snapshot:
        global SNAPSHOT
        SNAPSHOT = Path(args.snapshot)

    col = Collector()
    print("Lecture des flux…", flush=True)
    sources = []  # (chemin, en cours) dans l'ordre de lecture, pour l'audit
    if args.local_xml:
        for f in args.local_xml:
            with open(f, "rb") as fh:
                col.read(fh, HISTORY_START, f)
    else:
        # Archives annuelles (immuables, mises en cache), puis année en cours, puis jours récents,
        # puis flux instantané (prix, puis ruptures en cours).
        names = [f"annee/{year}" for year in range(HISTORY_START.year, now.year)] + ["annee"]
        names += [f"jour/{(now - timedelta(days=d)).strftime('%Y%m%d')}" for d in range(3, 0, -1)]
        names += ["jour", "instantane", "instantane_ruptures"]
        for name in names:
            try:
                data = fetch(f"{BASE}/{name}", cache, keep=name.startswith("annee/"))
            except Exception as e:  # noqa: BLE001
                if not name.startswith("jour/"):
                    raise
                print(f"  {name} indisponible : {e}")
                continue
            current = name == "instantane_ruptures"
            col.read(xml_from_zip(data), HISTORY_START, name, current=current)
            sources.append(name)

    sid, ts, pr = col.arrays()
    ts_max = int(ts.max()) if len(ts) else minutes(now)
    # « maintenant » = dernier prix reçu (évite un point futur si le flux a du retard)
    now = min(now, from_minutes(ts_max)).replace(second=0)
    print(f"{len(ts)} prix gazole retenus, dernier à {from_minutes(ts_max)}", flush=True)

    # Stations de métropole (le département vient du code postal ; les coordonnées ne servent qu'à la carte)
    stations, deps = [], []
    for pid, m in col.meta.items():
        d = departement(m.get("cp"))
        if d:
            stations.append(pid)
            deps.append(d)
    order = np.argsort(stations)
    stations = np.asarray(stations, dtype=np.int64)[order]
    deps = np.asarray(deps)[order]
    regs = np.asarray([DEP_TO_REG[d] for d in deps])

    # Ne garder que les événements des stations retenues
    keep = np.isin(sid, stations)
    sid, ts, pr = sid[keep], ts[keep], pr[keep]

    # Fautes de saisie
    keep = drop_spikes(sid, ts, pr)
    n_spikes = int(np.sum(~keep))
    sid, ts, pr = sid[keep], ts[keep], pr[keep]
    keep = drop_low_outliers(sid, ts, pr, stations, now)
    n_low = int(np.sum(~keep))
    sid, ts, pr = sid[keep], ts[keep], pr[keep]
    print(f"Fautes de saisie écartées : {n_spikes} allers-retours, {n_low} prix anormalement bas", flush=True)

    # Ruptures et fermetures
    excl, excl_stats = exclusion_intervals(col, sid, ts, stations, minutes(now))
    print(f"Intervalles d'exclusion : {dict(excl_stats)}", flush=True)
    if args.dump_events:
        np.savez_compressed(
            args.dump_events, sid=sid, ts=ts, pr=pr, stations=stations, deps=deps,
            excl_rows=excl[0], excl_starts=excl[1], excl_ends=excl[2],
            now=minutes(now),
            stats=json.dumps({"spikes": n_spikes, "low": n_low, "excl": dict(excl_stats), "sources": sources}),
        )

    pers = periods(now)
    series: dict = {}
    deltas: dict = {}
    for key, p in pers.items():
        pts_m = np.asarray([minutes(t) for t in p["points"]], dtype=np.int64)
        mat = price_matrix(sid, ts, pr, stations, pts_m, excl)
        s = {"FR": aggregate(mat)}
        for r in REGIONS:
            s[f"R{r}"] = aggregate(mat[regs == r])
        for d in sorted(set(deps)):
            s[f"D{d}"] = aggregate(mat[deps == d])
        series[key] = s
        delta = (mat[:, -1] - mat[:, 0]) * 100  # c€/L
        deltas[key] = [None if np.isnan(x) else round(float(x), 1) for x in delta]
        fr = s["FR"]
        print(
            f"  période {key}: {len(pts_m)} points, {int(np.sum(~np.isnan(mat[:, -1])))} stations actives, "
            f"France moyen {fr['mean'][0]} → {fr['mean'][-1]}, médian {fr['median'][0]} → {fr['median'][-1]}",
            flush=True,
        )

    # Dernier prix connu de chaque station
    last_idx = np.searchsorted(sid, stations, side="right") - 1
    has = (last_idx >= 0) & (sid[np.clip(last_idx, 0, None)] == stations)
    stations_out = {
        "id": stations.tolist(),
        "lat": [col.meta[i].get("lat") for i in stations.tolist()],
        "lon": [col.meta[i].get("lon") for i in stations.tolist()],
        "dep": deps.tolist(),
        "ville": [col.meta[i].get("ville", "") for i in stations.tolist()],
        "adresse": [col.meta[i].get("adresse", "") for i in stations.tolist()],
        "prix": [round(pr[j] / 1000, 3) if h else None for j, h in zip(last_idx, has)],
        "maj": [from_minutes(ts[j]).strftime("%Y-%m-%dT%H:%M") if h else None for j, h in zip(last_idx, has)],
        "delta": deltas,
    }

    # Historique brut par département (chargé au clic sur une station) : temps delta-encodés
    for d in sorted(set(deps)):
        sel = stations[deps == d]
        out_d = {}
        for pid in sel.tolist():
            a, b = np.searchsorted(sid, [pid, pid + 1])
            if a == b:
                continue
            t = ts[a:b]
            out_d[str(pid)] = [np.diff(t, prepend=0).tolist(), pr[a:b].tolist()]
        (out / "stations" / f"{d}.json").write_text(json.dumps(out_d, separators=(",", ":")))

    meta = {
        "generated_at": datetime.now(PARIS).strftime("%Y-%m-%dT%H:%M"),
        "data_until": now.strftime("%Y-%m-%dT%H:%M"),
        "stale_days": STALE_DAYS,
        "price_range": [PRICE_MIN / 1000, PRICE_MAX / 1000],
        "periods": {
            k: {"label": p["label"], "step": p["step"], "points": [t.strftime("%Y-%m-%dT%H:%M") for t in p["points"]]}
            for k, p in pers.items()
        },
        "dep_to_reg": {d: DEP_TO_REG[d] for d in sorted(set(deps))},
        "n_stations": len(stations),
    }

    def dump(name, obj):
        (out / name).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    dump("meta.json", meta)
    dump("series.json", series)
    dump("stations.json", stations_out)

    if not args.no_geojson:
        for kind in ("regions", "departements"):
            data = fetch(GEOJSON.format(kind), cache, keep=True)
            (out / f"{kind}.geojson").write_bytes(data)
    print("Terminé.", flush=True)


if __name__ == "__main__":
    main()
