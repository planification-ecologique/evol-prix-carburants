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
import sys
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


def minutes(dt: datetime) -> int:
    return int((dt - EPOCH).total_seconds() // 60)


def from_minutes(m: int) -> datetime:
    return EPOCH + timedelta(minutes=int(m))


# --------------------------------------------------------------------------- téléchargement


def fetch(url: str, cache: Path | None, keep: bool) -> bytes:
    """Télécharge url. Si keep, le fichier est mis en cache (archives immuables)."""
    name = url.removeprefix(BASE + "/").replace("/", "_") or "root"
    path = cache / name if cache else None
    if keep and path and path.exists():
        return path.read_bytes()
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "evol-prix-carburants"})
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
            if keep and path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
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

    def read(self, stream, since: datetime, label: str) -> None:
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


def price_matrix(sid, ts, pr, stations, points_m):
    """prix[i, k] : dernier prix connu de stations[i] à l'instant points_m[k] (NaN si absent ou périmé)."""
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
    out = np.where(valid, pr[idx_c] / 1000.0, np.nan)
    return out.reshape(len(stations), len(points_m))


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
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(PARIS).replace(tzinfo=None)
    now = now.replace(second=0, microsecond=0)
    out = Path(args.out)
    (out / "stations").mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache)

    col = Collector()
    print("Lecture des flux…", flush=True)
    if args.local_xml:
        for f in args.local_xml:
            with open(f, "rb") as fh:
                col.read(fh, HISTORY_START, f)
    else:
        # Archives annuelles (immuables, mises en cache), puis année en cours, puis jours récents.
        for year in range(HISTORY_START.year, now.year):
            col.read(xml_from_zip(fetch(f"{BASE}/annee/{year}", cache, keep=True)), HISTORY_START, f"annee/{year}")
        col.read(xml_from_zip(fetch(f"{BASE}/annee", cache, keep=False)), HISTORY_START, "annee (en cours)")
        for d in range(3, 0, -1):
            day = (now - timedelta(days=d)).strftime("%Y%m%d")
            try:
                col.read(xml_from_zip(fetch(f"{BASE}/jour/{day}", cache, keep=False)), HISTORY_START, f"jour/{day}")
            except Exception as e:  # noqa: BLE001
                print(f"  jour/{day} indisponible : {e}")
        col.read(xml_from_zip(fetch(f"{BASE}/jour", cache, keep=False)), HISTORY_START, "jour (aujourd'hui)")
        col.read(xml_from_zip(fetch(f"{BASE}/instantane", cache, keep=False)), HISTORY_START, "instantane")

    sid, ts, pr = col.arrays()
    ts_max = int(ts.max()) if len(ts) else minutes(now)
    # « maintenant » = dernier prix reçu (évite un point futur si le flux a du retard)
    now = min(now, from_minutes(ts_max)).replace(second=0)
    print(f"{len(ts)} prix gazole retenus, dernier à {from_minutes(ts_max)}", flush=True)

    # Stations de métropole avec coordonnées
    stations, deps = [], []
    for pid, m in col.meta.items():
        d = departement(m.get("cp"))
        if d and "lat" in m:
            stations.append(pid)
            deps.append(d)
    order = np.argsort(stations)
    stations = np.asarray(stations, dtype=np.int64)[order]
    deps = np.asarray(deps)[order]
    regs = np.asarray([DEP_TO_REG[d] for d in deps])

    # Ne garder que les événements des stations retenues
    keep = np.isin(sid, stations)
    sid, ts, pr = sid[keep], ts[keep], pr[keep]

    pers = periods(now)
    series: dict = {}
    deltas: dict = {}
    for key, p in pers.items():
        pts_m = np.asarray([minutes(t) for t in p["points"]], dtype=np.int64)
        mat = price_matrix(sid, ts, pr, stations, pts_m)
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
        "lat": [col.meta[i]["lat"] for i in stations.tolist()],
        "lon": [col.meta[i]["lon"] for i in stations.tolist()],
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
