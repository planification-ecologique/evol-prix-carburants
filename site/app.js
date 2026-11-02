/* Évolution du prix du gazole — page statique, données précalculées dans data/ */
(() => {
  "use strict";

  // tableaux de paires : l'ordre des clés numériques (« 2025 ») n'est pas garanti dans un objet
  const PERIOD_OPTS = [["48h", "48 h"], ["7j", "7 jours"], ["3m", "3 mois"], ["fev26", "Depuis fév. 2026"], ["2025", "Depuis 2025"]];
  const SCALE_OPTS = [["FR", "France"], ["R", "Région"], ["D", "Département"], ["S", "Station"]];
  const INDICS = { mean: "Prix moyen", median: "Prix médian" };
  const STOPS = [[26, 152, 80], [240, 162, 2], [215, 48, 39]]; // vert → orange → rouge

  const state = { period: "7j", scale: "FR", indic: "mean", sel: { kind: "FR", code: "FR" } };
  let D = {}; // données chargées
  let map, layer, chart;
  const stationCache = {};

  // ------------------------------------------------------------------ utilitaires
  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  // Montants toujours en euros, avec 2 décimales
  const fmtPrice = (v) => (v == null ? "–" : v.toFixed(2).replace(".", ",") + " €");
  // c : écart en centimes (unité interne des données) → affiché en euros
  const fmtDelta = (c) => {
    if (c == null) return "–";
    const e = Math.round(c) / 100;
    return (e > 0 ? "+" : e < 0 ? "−" : "") + Math.abs(e).toFixed(2).replace(".", ",") + " €";
  };
  const parseT = (s) => new Date(s + ":00Z"); // heure de Paris stockée « naïve »
  const minutesOf = (s) => Math.round(parseT(s).getTime() / 60000);

  function fmtPoint(s, step) {
    const d = parseT(s);
    const dd = String(d.getUTCDate()).padStart(2, "0");
    const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
    if (step === "heure") return `${dd}/${mm} ${String(d.getUTCHours()).padStart(2, "0")}h`;
    if (step === "semaine") return `${dd}/${mm}/${String(d.getUTCFullYear()).slice(2)}`;
    return `${dd}/${mm}`;
  }

  function colorFor(v, lo, hi) {
    if (v == null || Number.isNaN(v)) return css("--null");
    let t = hi > lo ? (v - lo) / (hi - lo) : 0.5;
    t = Math.max(0, Math.min(1, t));
    const [a, b, u] = t < 0.5 ? [STOPS[0], STOPS[1], t * 2] : [STOPS[1], STOPS[2], (t - 0.5) * 2];
    const c = a.map((x, i) => Math.round(x + (b[i] - x) * u));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  }

  function quantile(sorted, q) {
    if (!sorted.length) return null;
    const pos = (sorted.length - 1) * q;
    const lo = Math.floor(pos), hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  function seriesDelta(s, indic) {
    if (!s) return null;
    const a = s[indic];
    const first = a.find((x) => x != null);
    let last = null;
    for (let i = a.length - 1; i >= 0; i--) if (a[i] != null) { last = a[i]; break; }
    return first == null || last == null ? null : Math.round((last - first) * 1000) / 10;
  }

  // ------------------------------------------------------------------ contrôles
  function segmented(el, options, key) {
    el.innerHTML = "";
    for (const [value, label] of options) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.setAttribute("role", "radio");
      b.dataset.value = value;
      b.addEventListener("click", () => {
        state[key] = value;
        if (key === "scale") state.sel = { kind: "FR", code: "FR" };
        render();
      });
      el.appendChild(b);
    }
  }
  function syncControls() {
    for (const [id, key] of [["ctl-period", "period"], ["ctl-scale", "scale"], ["ctl-indic", "indic"]]) {
      for (const b of $(id).children) b.setAttribute("aria-checked", String(b.dataset.value === state[key]));
    }
  }

  // ------------------------------------------------------------------ carte
  function initMap() {
    map = L.map("map", { preferCanvas: true, zoomSnap: 0.25, minZoom: 4 }).setView([46.6, 2.4], 5.75);
    // Fond Plan IGN (Géoplateforme) : libre d'accès, sans clé d'API
    L.tileLayer(
      "https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2" +
        "&STYLE=normal&TILEMATRIXSET=PM&FORMAT=image/png&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}",
      { attribution: '&copy; <a href="https://www.ign.fr/">IGN</a> – Plan IGN', maxZoom: 18, opacity: 0.6 },
    ).addTo(map);
    window.__map = map; // pratique pour déboguer depuis la console
  }

  function drawMap() {
    if (layer) map.removeLayer(layer);
    const p = state.period;
    const series = D.series[p];
    let values = [];

    if (state.scale === "S") {
      const st = D.stations, deltas = st.delta[p];
      values = deltas.filter((x) => x != null).sort((a, b) => a - b);
      const lo = quantile(values, 0.05), hi = quantile(values, 0.95);
      const renderer = L.canvas({ padding: 0.5 });
      layer = L.layerGroup();
      for (let i = 0; i < st.id.length; i++) {
        const d = deltas[i];
        const selected = state.sel.kind === "S" && state.sel.code === st.id[i];
        const m = L.circleMarker([st.lat[i], st.lon[i]], {
          renderer, radius: selected ? 7 : 3.5, weight: selected ? 2 : 0.5,
          color: selected ? css("--ink") : "rgba(0,0,0,.25)",
          fillColor: colorFor(d, lo, hi), fillOpacity: 0.9,
        });
        m.bindTooltip(() => `<strong>${st.ville[i] || "Station " + st.id[i]}</strong><br>${st.adresse[i] || ""}<br>Prix : ${fmtPrice(st.prix[i])}<br>Évolution : ${fmtDelta(d)}`);
        m.on("click", () => selectStation(i));
        layer.addLayer(m);
      }
      layer.addTo(map);
      drawLegend(lo, hi);
      return;
    }

    // Régions, départements (et France : départements en contexte)
    const kind = state.scale === "R" ? "R" : "D";
    const geo = kind === "R" ? D.regions : D.deps;
    const val = (code) => seriesDelta(series[kind + code], state.indic);
    values = geo.features.map((f) => val(f.properties.code)).filter((x) => x != null).sort((a, b) => a - b);
    const lo = quantile(values, 0.05), hi = quantile(values, 0.95);
    layer = L.geoJSON(geo, {
      style: (f) => {
        const selected = state.sel.kind === kind && state.sel.code === f.properties.code;
        return {
          fillColor: colorFor(val(f.properties.code), lo, hi), fillOpacity: 0.85,
          color: selected ? css("--ink") : css("--surface"), weight: selected ? 2.5 : 0.8,
        };
      },
      onEachFeature: (f, l) => {
        const code = f.properties.code;
        l.bindTooltip(() => {
          const s = series[kind + code];
          const last = s ? [...s[state.indic]].reverse().find((x) => x != null) : null;
          return `<strong>${f.properties.nom}</strong><br>${INDICS[state.indic]} : ${fmtPrice(last)}<br>Évolution : ${fmtDelta(val(code))}`;
        }, { sticky: true });
        l.on("click", () => {
          if (state.scale === "FR") state.scale = "D";
          state.sel = { kind, code, name: f.properties.nom };
          render();
        });
      },
    }).addTo(map);
    drawLegend(lo, hi);
  }

  function drawLegend(lo, hi) {
    const mid = lo != null && hi != null ? (lo + hi) / 2 : null;
    $("legend").innerHTML =
      `<div>Évolution sur la période (${state.scale === "S" ? "prix de la station" : INDICS[state.indic].toLowerCase()})</div>` +
      `<div class="bar"></div><div class="ticks"><span>${fmtDelta(lo)}</span><span>${fmtDelta(mid)}</span><span>${fmtDelta(hi)}</span></div>` +
      `<div style="margin-top:4px">Vert : plus forte baisse · Rouge : plus faible baisse ou hausse</div>`;
  }

  // ------------------------------------------------------------------ sélection station
  async function selectStation(i) {
    const st = D.stations;
    state.sel = { kind: "S", code: st.id[i], idx: i, name: st.ville[i] || `Station ${st.id[i]}` };
    render();
  }

  async function stationSeries(i, period) {
    const st = D.stations, dep = st.dep[i];
    if (!stationCache[dep]) stationCache[dep] = fetch(`data/stations/${dep}.json`).then((r) => r.json());
    const raw = (await stationCache[dep])[String(st.id[i])];
    const pts = D.meta.periods[period].points.map(minutesOf);
    if (!raw) return { mean: pts.map(() => null), median: pts.map(() => null), n: pts.map(() => 0) };
    let t = 0;
    const ts = raw[0].map((d) => (t += d));
    const prices = raw[1];
    const stale = D.meta.stale_days * 24 * 60;
    const out = pts.map((q) => {
      let lo = 0, hi = ts.length - 1, k = -1;
      while (lo <= hi) { const m = (lo + hi) >> 1; if (ts[m] <= q) { k = m; lo = m + 1; } else hi = m - 1; }
      return k >= 0 && q - ts[k] <= stale ? prices[k] / 1000 : null;
    });
    return { mean: out, median: out, n: out.map((x) => (x == null ? 0 : 1)) };
  }

  // ------------------------------------------------------------------ panneau latéral
  async function drawSide() {
    const p = state.period, meta = D.meta.periods[p];
    let s, title, sub;
    const sel = state.sel;
    if (sel.kind === "S") {
      const i = sel.idx, st = D.stations;
      s = await stationSeries(i, p);
      title = st.ville[i] || `Station ${st.id[i]}`;
      sub = `${st.adresse[i] || ""} · département ${st.dep[i]} · dernier prix ${fmtPrice(st.prix[i])}${st.maj[i] ? " le " + st.maj[i].replace("T", " à ").slice(0, 18) : ""}`;
    } else {
      s = D.series[p][sel.kind === "FR" ? "FR" : sel.kind + sel.code];
      title = sel.kind === "FR" ? "France métropolitaine" : sel.name;
      const nLast = s ? s.n[s.n.length - 1] : 0;
      sub = `${{ FR: "Pays", R: "Région", D: "Département" }[sel.kind]} · ${nLast.toLocaleString("fr-FR")} stations actives en fin de période`;
    }
    $("sel-title").textContent = title;
    $("sel-sub").textContent = sub;
    $("sel-reset").hidden = sel.kind === "FR";

    const labels = meta.points.map((x) => fmtPoint(x, meta.step));
    const isStation = sel.kind === "S";
    const ds = (key, color) => ({
      label: isStation ? "Prix de la station" : INDICS[key],
      data: s ? s[key] : [],
      borderColor: color, backgroundColor: color,
      borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0, spanGaps: true,
      borderDash: !isStation && key !== state.indic ? [5, 4] : [],
    });
    const datasets = isStation ? [ds("mean", css("--mean"))] : [ds("mean", css("--mean")), ds("median", css("--median"))];
    const ink2 = css("--ink-2"), line = css("--line");
    if (!chart) {
      chart = new Chart($("chart"), {
        type: "line",
        data: { labels, datasets },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { position: "top", align: "start", labels: { boxWidth: 14, boxHeight: 2, color: ink2 } },
            tooltip: { callbacks: { label: (c) => `${c.dataset.label} : ${fmtPrice(c.parsed.y)}` } },
          },
          scales: {
            x: { ticks: { color: ink2, maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } },
            y: { ticks: { color: ink2, callback: (v) => v.toFixed(2).replace(".", ",") + " €" }, grid: { color: line } },
          },
        },
      });
    } else {
      chart.data.labels = labels;
      chart.data.datasets = datasets;
      chart.update();
    }

    // Tableau de synthèse
    const rows = (isStation ? ["mean"] : ["mean", "median"]).map((k) => {
      const a = s ? s[k] : [];
      const vals = a.filter((x) => x != null);
      const first = a.find((x) => x != null), last = [...a].reverse().find((x) => x != null);
      const delta = first != null && last != null ? Math.round((last - first) * 1000) / 10 : null;
      const cls = delta == null ? "" : delta < 0 ? "down" : delta > 0 ? "up" : "";
      const color = k === "mean" ? css("--mean") : css("--median");
      return `<tr><td><span class="swatch" style="background:${color}"></span>${isStation ? "Prix" : INDICS[k]}</td>` +
        `<td>${fmtPrice(first)}</td><td>${fmtPrice(last)}</td><td class="${cls}">${fmtDelta(delta)}</td>` +
        `<td>${fmtPrice(vals.length ? Math.min(...vals) : null)}</td><td>${fmtPrice(vals.length ? Math.max(...vals) : null)}</td></tr>`;
    });
    $("synth").querySelector("tbody").innerHTML = rows.join("");
    $("synth-note").textContent = `${meta.label}, pas de temps : ${meta.step}. Du ${meta.points[0].replace("T", " à ")} au ${meta.points[meta.points.length - 1].replace("T", " à ")}.`;
  }

  // ------------------------------------------------------------------ rendu
  async function render() {
    syncControls();
    drawMap();
    await drawSide();
  }

  async function load() {
    const get = (f) => fetch(`data/${f}`).then((r) => { if (!r.ok) throw new Error(f); return r.json(); });
    const [meta, series, stations, regions, deps] = await Promise.all([
      get("meta.json"), get("series.json"), get("stations.json"), get("regions.geojson"), get("departements.geojson"),
    ]);
    D = { meta, series, stations, regions, deps };
    $("maj").textContent = `Données jusqu'au ${meta.data_until.replace("T", " à ")} · ${meta.n_stations.toLocaleString("fr-FR")} stations · mise à jour toutes les heures`;
    document.querySelectorAll("[data-k=stale_days]").forEach((e) => (e.textContent = meta.stale_days));
    document.querySelectorAll("[data-k=price_range]").forEach((e) => (e.textContent = `${meta.price_range[0]} à ${meta.price_range[1]}`));
  }

  async function init() {
    segmented($("ctl-period"), PERIOD_OPTS, "period");
    segmented($("ctl-scale"), SCALE_OPTS, "scale");
    segmented($("ctl-indic"), Object.entries(INDICS), "indic");
    $("sel-reset").addEventListener("click", () => { state.sel = { kind: "FR", code: "FR" }; render(); });
    initMap();
    try {
      await load();
      await render();
    } catch (e) {
      $("maj").textContent = "Impossible de charger les données (" + e.message + ").";
      console.error(e);
    }
  }

  init();
})();
