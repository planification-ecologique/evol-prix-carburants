/* Évolution du prix des carburants — page statique, données précalculées dans data/ (data/<carburant>/ pour chaque carburant) */
(() => {
  "use strict";

  // tableaux de paires : l'ordre des clés numériques (« 2025 ») n'est pas garanti dans un objet
  // Raccourcis de période : [clé, libellé, nombre de jours avant aujourd'hui ou date de début] ; la date de fin est aujourd'hui
  const SHORTCUTS = [["48h", "48 h", 2], ["7j", "7 jours", 7], ["3m", "3 mois", 91], ["fev26", "Depuis fév. 2026", "2026-02-01"], ["2025", "Depuis 2025", "2025-01-01"]];
  const MIN_DATE = "2025-01-01"; // début de la grille journalière (data/<carburant>/series.json, période « jour »)
  // Carburants : clé = dossier des données (voir FUELS dans pipeline/build.py)
  const FUEL_OPTS = [["gazole", "Gazole"], ["e10", "SP95-E10"], ["sp98", "SP98"]];
  const FUEL_LABEL = Object.fromEntries(FUEL_OPTS);
  const SCALE_OPTS = [["FR", "France"], ["R", "Région"], ["D", "Département"], ["S", "Station"]];
  const INDICS = { mean: "Prix moyen", median: "Prix médian" };
  // Échelle divergente centrée sur 0 : dégradé de vert si le prix baisse, de rouge s'il monte
  const NEUTRAL = [238, 238, 232], GREEN = [0, 104, 55], RED = [165, 0, 38];

  // Retrait temporaire du prix moyen (demande du 02/11/2026) : seul le prix médian est affiché.
  // Repasser à true pour réafficher la courbe moyenne, sa ligne du tableau et le sélecteur moyen/médian.
  const SHOW_MEAN = false;

  // start, end : dates de début et de fin (AAAA-MM-JJ), fixées au chargement (J-7 → aujourd'hui)
  const state = { fuel: initialFuel(), start: null, end: null, scale: "FR", indic: SHOW_MEAN ? "mean" : "median", sel: { kind: "FR", code: "FR" } };
  let D = {}; // données communes chargées (meta, contours)
  let map, layer, chart, renderer;
  let R; // période affichée, calculée à chaque rendu (voir currentRange)
  // Données de chaque carburant, chargées à la demande : { series, stations }
  const FD = Object.fromEntries(FUEL_OPTS.map(([k]) => [k, { matrix: {} }]));
  const F = () => FD[state.fuel];

  // Carburant choisi conservé dans l'adresse (#e10) pour pouvoir partager le lien
  function initialFuel() {
    const h = location.hash.slice(1);
    return FUEL_OPTS.some(([k]) => k === h) ? h : "gazole";
  }

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

  // m : écart (en valeur absolue) qui reçoit la couleur la plus intense
  function colorFor(v, m) {
    if (v == null || Number.isNaN(v)) return css("--null");
    const t = m > 0 ? Math.min(1, Math.abs(v) / m) : 0;
    const end = v < 0 ? GREEN : RED;
    const c = NEUTRAL.map((x, i) => Math.round(x + (end[i] - x) * t));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  }

  // borne de l'échelle : 95e centile des écarts en valeur absolue (évite qu'une valeur extrême écrase les couleurs)
  function scaleMax(values) {
    const abs = values.map(Math.abs).sort((a, b) => a - b);
    return quantile(abs, 0.95) || 0;
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

  // ------------------------------------------------------------------ période (date de début, date de fin)
  const addDays = (d, n) => { const x = new Date(d + "T00:00:00Z"); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };
  const dayDiff = (a, b) => Math.round((new Date(b + "T00:00:00Z") - new Date(a + "T00:00:00Z")) / 864e5);
  const today = () => D.meta.data_until.slice(0, 10);
  const shortcutStart = (v) => (typeof v === "number" ? addDays(today(), -v) : v);
  const fmtDay = (d) => d.split("-").reverse().join("/");

  // Le premier point est l'état des prix à minuit le jour de début ; le dernier, l'état à minuit le lendemain
  // du jour de fin, ou le dernier prix reçu si la date de fin est aujourd'hui. Points horaires si la période
  // tient dans les 3 derniers jours, journaliers sinon.
  function currentRange() {
    const hp = D.meta.periods.heure.points;
    const grid = state.start >= hp[0].slice(0, 10) && dayDiff(state.start, state.end) <= 3 ? "heure" : "jour";
    const pts = D.meta.periods[grid].points;
    let i0 = pts.indexOf(state.start + "T00:00");
    if (i0 < 0) i0 = 0;
    let i1 = pts.indexOf(addDays(state.end, 1) + "T00:00");
    if (i1 < 0) i1 = pts.length - 1;
    const long = dayDiff(state.start, state.end) > 300;
    return { grid, i0, i1, points: pts.slice(i0, i1 + 1), step: grid === "heure" ? "heure" : long ? "semaine" : "jour" };
  }
  const cut = (s) => s && { mean: s.mean.slice(R.i0, R.i1 + 1), median: s.median.slice(R.i0, R.i1 + 1), n: s.n.slice(R.i0, R.i1 + 1) };

  function setDates(start, end) {
    const t = today();
    const clamp = (d) => (!d || d > t ? t : d < MIN_DATE ? MIN_DATE : d);
    start = clamp(start);
    end = clamp(end);
    if (start > end) [start, end] = [end, start];
    state.start = start;
    state.end = end;
  }

  function initPeriod() {
    const s = $("d-start"), e = $("d-end");
    for (const el of [s, e]) {
      el.min = MIN_DATE;
      el.max = today();
      el.addEventListener("change", () => { setDates(s.value, e.value); render(); });
    }
    const box = $("ctl-period");
    box.innerHTML = "";
    for (const [key, label, v] of SHORTCUTS) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.setAttribute("role", "radio");
      b.dataset.value = key;
      b.addEventListener("click", () => { setDates(shortcutStart(v), today()); render(); });
      box.appendChild(b);
    }
    setDates(addDays(today(), -7), today());
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
        if (key === "fuel") history.replaceState(null, "", value === "gazole" ? location.pathname + location.search : "#" + value);
        render();
      });
      el.appendChild(b);
    }
  }
  function syncControls() {
    for (const [id, key] of [["ctl-fuel", "fuel"], ["ctl-scale", "scale"], ["ctl-indic", "indic"]]) {
      for (const b of $(id).children) b.setAttribute("aria-checked", String(b.dataset.value === state[key]));
    }
    $("d-start").value = state.start;
    $("d-end").value = state.end;
    for (const b of $("ctl-period").children) {
      const v = SHORTCUTS.find(([k]) => k === b.dataset.value)[2];
      b.setAttribute("aria-checked", String(state.end === today() && state.start === shortcutStart(v)));
    }
  }

  // ------------------------------------------------------------------ carte
  function initMap() {
    map = L.map("map", { preferCanvas: true, zoomSnap: 0.25, minZoom: 4 }).setView([46.6, 2.4], 5.75);
    // Un seul calque de dessin pour toutes les couches : en recréer un à chaque rendu empilait
    // des canevas vides par-dessus la carte, qui interceptaient les clics.
    renderer = L.canvas({ padding: 0.5 });
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
    const series = F().series[R.grid];
    let values = [];

    if (state.scale === "S") {
      const st = F().stations, deltas = stationDeltas(F().matrix[R.grid]);
      values = deltas.filter((x) => x != null).sort((a, b) => a - b);
      const vmax = scaleMax(values);
      layer = L.layerGroup();
      for (let i = 0; i < st.id.length; i++) {
        if (st.lat[i] == null || st.lon[i] == null) continue; // station sans coordonnées valides : comptée, mais pas cartographiée
        if (st.prix[i] == null) continue; // station qui ne vend pas ce carburant (aucun prix depuis décembre 2024)
        const d = deltas[i];
        const selected = state.sel.kind === "S" && state.sel.code === st.id[i];
        const m = L.circleMarker([st.lat[i], st.lon[i]], {
          renderer, radius: selected ? 7 : 3.5, weight: selected ? 2 : 0.5,
          color: selected ? css("--ink") : "rgba(0,0,0,.25)",
          fillColor: colorFor(d, vmax), fillOpacity: 0.9,
        });
        m.bindTooltip(() => `<strong>${st.ville[i] || "Station " + st.id[i]}</strong><br>${st.adresse[i] || ""}<br>Prix : ${fmtPrice(st.prix[i])}<br>Évolution : ${fmtDelta(d)}`);
        m.on("click", () => selectStation(i));
        layer.addLayer(m);
      }
      layer.addTo(map);
      drawLegend(vmax);
      return;
    }

    // Régions, départements (et France : départements en contexte)
    const kind = state.scale === "R" ? "R" : "D";
    const geo = kind === "R" ? D.regions : D.deps;
    const val = (code) => seriesDelta(cut(series[kind + code]), state.indic);
    values = geo.features.map((f) => val(f.properties.code)).filter((x) => x != null).sort((a, b) => a - b);
    const vmax = scaleMax(values);
    layer = L.geoJSON(geo, {
      renderer,
      style: (f) => {
        const selected = state.sel.kind === kind && state.sel.code === f.properties.code;
        return {
          fillColor: colorFor(val(f.properties.code), vmax), fillOpacity: 0.85,
          color: selected ? css("--ink") : css("--surface"), weight: selected ? 2.5 : 0.8,
        };
      },
      onEachFeature: (f, l) => {
        const code = f.properties.code;
        l.bindTooltip(() => {
          const s = cut(series[kind + code]);
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
    drawLegend(vmax);
  }

  function drawLegend(m) {
    $("legend").innerHTML =
      `<div>Évolution du prix ${FUEL_LABEL[state.fuel]} sur la période (${state.scale === "S" ? "prix de la station" : INDICS[state.indic].toLowerCase()})</div>` +
      `<div class="bar"></div><div class="ticks"><span>${fmtDelta(-m)}</span><span>${fmtDelta(0)}</span><span>${fmtDelta(m)}</span></div>` +
      `<div style="margin-top:4px">Vert : le prix baisse · Rouge : le prix monte · couleur plus foncée = écart plus grand</div>`;
  }

  // ------------------------------------------------------------------ sélection station
  async function selectStation(i) {
    const st = F().stations;
    state.sel = { kind: "S", code: st.id[i], idx: i, name: st.ville[i] || `Station ${st.id[i]}` };
    render();
  }

  // Courbe d'une station : ses prix aux points de la période (mêmes règles que les agrégats : 30 jours, ruptures)
  async function stationSeries(i) {
    const M = await loadMatrix(state.fuel, R.grid);
    const out = [];
    for (let k = R.i0; k <= R.i1; k++) { const v = M.at(k, i); out.push(v ? v / 1000 : null); }
    return { mean: out, median: out, n: out.map((x) => (x == null ? 0 : 1)) };
  }

  // Écart de prix de chaque station entre le début et la fin de la période, en centimes (null sans prix à l'un des deux)
  function stationDeltas(M) {
    const out = new Array(M.nS);
    for (let i = 0; i < M.nS; i++) {
      const a = M.at(R.i0, i), b = M.at(R.i1, i);
      out[i] = a && b ? (b - a) / 10 : null;
    }
    return out;
  }

  // ------------------------------------------------------------------ panneau latéral
  async function drawSide() {
    let s, title, sub;
    const sel = state.sel;
    if (sel.kind === "S") {
      const i = sel.idx, st = F().stations;
      s = await stationSeries(i);
      title = st.ville[i] || `Station ${st.id[i]}`;
      sub = `${FUEL_LABEL[state.fuel]} · ${st.adresse[i] || ""} · département ${st.dep[i]} · dernier prix ${fmtPrice(st.prix[i])}${st.maj[i] ? " le " + st.maj[i].replace("T", " à ").slice(0, 18) : ""}`;
    } else {
      s = cut(F().series[R.grid][sel.kind === "FR" ? "FR" : sel.kind + sel.code]);
      title = sel.kind === "FR" ? "France métropolitaine" : sel.name;
      const nLast = s ? s.n[s.n.length - 1] : 0;
      sub = `${FUEL_LABEL[state.fuel]} · ${{ FR: "Pays", R: "Région", D: "Département" }[sel.kind]} · ${nLast.toLocaleString("fr-FR")} stations actives en fin de période`;
    }
    $("sel-title").textContent = title;
    $("sel-sub").textContent = sub;
    $("sel-reset").hidden = sel.kind === "FR";

    const labels = R.points.map((x) => fmtPoint(x, R.step));
    const isStation = sel.kind === "S";
    const ds = (key, color) => ({
      label: `${isStation ? "Prix de la station" : INDICS[key]} (${FUEL_LABEL[state.fuel]})`,
      data: s ? s[key] : [],
      borderColor: color, backgroundColor: color,
      borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0, spanGaps: true,
      borderDash: !isStation && key !== state.indic ? [5, 4] : [],
    });
    const datasets = isStation
      ? [ds("mean", css("--mean"))]
      : SHOW_MEAN
        ? [ds("mean", css("--mean")), ds("median", css("--median"))]
        : [ds("median", css("--median"))];
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
    const rows = (isStation ? ["mean"] : SHOW_MEAN ? ["mean", "median"] : ["median"]).map((k) => {
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
    const ptTxt = (x) => fmtDay(x.slice(0, 10)) + " à " + x.slice(11, 13) + " h" + (x.slice(14) === "00" ? "" : " " + x.slice(14));
    $("synth-note").textContent = `Du ${ptTxt(R.points[0])} au ${ptTxt(R.points[R.points.length - 1])}, un point par ${R.grid}.`;
    await drawCounts(isStation ? null : s);
  }

  // Nombre de stations de la zone et nombre de stations dont le prix a baissé d'au moins 0,05 / 0,10 / 0,15 / 0,20 €
  const DROPS = [5, 10, 15, 20]; // en centimes, unité des écarts dans stations.json
  async function drawCounts(s) {
    const show = (on) => { $("counts").hidden = !on; $("counts-note").hidden = !on; };
    if (!s) return show(false);
    const sel = state.sel, r = R, fuel = state.fuel;
    const [st, M] = await Promise.all([loadStations(fuel), loadMatrix(fuel, r.grid)]);
    if (state.sel !== sel || R !== r || state.fuel !== fuel) return; // la sélection a changé entre-temps
    const inZone = (i) =>
      sel.kind === "FR" || (sel.kind === "D" ? st.dep[i] === sel.code : D.meta.dep_to_reg[st.dep[i]] === sel.code);
    const deltas = stationDeltas(M);
    let comparable = 0;
    const counts = DROPS.map(() => 0);
    for (let i = 0; i < st.id.length; i++) {
      if (!inZone(i) || deltas[i] == null) continue;
      comparable++;
      DROPS.forEach((d, k) => { if (deltas[i] <= -d) counts[k]++; });
    }
    const active = s.n[s.n.length - 1];
    const pct = (k) => (comparable ? ((100 * k) / comparable).toFixed(1).replace(".", ",") + " %" : "–");
    const nf = (x) => x.toLocaleString("fr-FR");
    $("counts").querySelector("tbody").innerHTML =
      `<tr><td>Actives en fin de période</td><td>${nf(active)}</td><td></td></tr>` +
      `<tr><td>Avec un prix au début et à la fin</td><td>${nf(comparable)}</td><td>100 %</td></tr>` +
      DROPS.map((d, k) => `<tr><td>Baisse d'au moins ${fmtPrice(d / 100)}</td><td>${nf(counts[k])}</td><td>${pct(counts[k])}</td></tr>`).join("");
    $("counts-note").textContent =
      "Les pourcentages portent sur les stations qui ont un prix au début et à la fin de la période.";
    show(true);
  }

  // ------------------------------------------------------------------ rendu
  let renderSeq = 0;
  async function render() {
    const seq = ++renderSeq;
    syncControls();
    const fuel = state.fuel;
    R = currentRange();
    const grid = R.grid;
    // séries du carburant (chargées au premier choix) ; stations et prix par station seulement à l'échelle Station
    const needS = state.scale === "S";
    if (!FD[fuel].series || (needS && (!FD[fuel].stations || !FD[fuel].matrix[grid]))) {
      $("map-loading").hidden = false;
      try {
        await loadSeries(fuel);
        if (needS) await Promise.all([loadStations(fuel), loadMatrix(fuel, grid)]);
      } catch (e) {
        $("maj").textContent = "Impossible de charger les données (" + e.message + ").";
        throw e;
      } finally {
        $("map-loading").hidden = true;
      }
      if (seq !== renderSeq) return; // un autre choix a été fait pendant le chargement
    }
    showMaj();
    drawMap();
    await drawSide();
  }

  // Le navigateur garde les fichiers en cache (10 min sur GitHub Pages) : meta.json est toujours revalidé, et les
  // autres fichiers sont demandés avec la date de calcul (?v=…), pour ne jamais mélanger deux calculs.
  let DV = "";
  const get = (f, opts) => fetch(`data/${f}${DV}`, opts).then((r) => { if (!r.ok) throw new Error(f); return r.json(); });
  const promises = {};
  const loadSeries = (fuel) =>
    (promises[fuel + "/series"] ||= get(`${fuel}/series.json`).then((x) => (FD[fuel].series = x)));
  const loadStations = (fuel) =>
    (promises[fuel + "/stations"] ||= get(`${fuel}/stations.json`).then((x) => (FD[fuel].stations = x)));
  // Prix de chaque station à chaque point de la grille (voir write_matrix dans pipeline/build.py)
  const loadMatrix = (fuel, grid) =>
    (promises[`${fuel}/prix-${grid}`] ||= fetch(`data/${fuel}/prix-${grid}.bin.gz${DV}`).then(async (r) => {
      if (!r.ok) throw new Error(`prix-${grid}`);
      let buf = new Uint8Array(await r.arrayBuffer());
      // fichier gzip, sauf si le serveur l'a déjà décompressé à la volée
      if (buf[0] === 0x1f && buf[1] === 0x8b) {
        const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
        buf = new Uint8Array(await new Response(stream).arrayBuffer());
      }
      const d = new Int16Array(buf.buffer, buf.byteOffset, buf.byteLength >> 1);
      const nS = D.meta.n_stations, out = new Uint16Array(d.length);
      for (let i = 0; i < nS && i < d.length; i++) out[i] = d[i];
      for (let j = nS; j < d.length; j++) out[j] = out[j - nS] + d[j];
      const M = { nS, at: (k, i) => out[k * nS + i] }; // 0 = pas de prix
      FD[fuel].matrix[grid] = M;
      return M;
    }));

  // stations actives = prix déclaré dans les 30 derniers jours (dernier point de la série France)
  function showMaj() {
    const nFR = F().series.heure.FR.n, nActive = nFR[nFR.length - 1];
    $("maj").textContent = `Données jusqu'au ${D.meta.data_until.replace("T", " à ")} · ${nActive.toLocaleString("fr-FR")} stations actives en ${FUEL_LABEL[state.fuel]} · mise à jour toutes les heures`;
  }

  async function load() {
    // Uniquement ce qu'il faut pour le premier affichage (France, régions, départements)
    const meta = await get("meta.json", { cache: "no-cache" });
    if (!meta.periods || !meta.periods.heure || !meta.fuels) {
      // page publiée avant les données correspondantes (publication en cours)
      throw new Error("mise à jour du site en cours, rechargez la page dans quelques minutes");
    }
    DV = "?v=" + encodeURIComponent(meta.generated_at);
    const [, regions, deps] = await Promise.all([
      loadSeries(state.fuel), get("regions.geojson"), get("departements.geojson"),
    ]);
    D = { meta, regions, deps };
    document.querySelectorAll("[data-k=stale_days]").forEach((e) => (e.textContent = meta.stale_days));
    document.querySelectorAll("[data-k=price_range]").forEach((e) => (e.textContent = `${meta.price_range[0]} à ${meta.price_range[1]}`));
  }

  async function init() {
    segmented($("ctl-fuel"), FUEL_OPTS, "fuel");
    segmented($("ctl-scale"), SCALE_OPTS, "scale");
    segmented($("ctl-indic"), Object.entries(INDICS), "indic");
    if (!SHOW_MEAN) $("ctl-indic").closest(".ctl").style.display = "none";
    window.addEventListener("hashchange", () => {
      const f = initialFuel();
      if (f !== state.fuel) { state.fuel = f; render(); }
    });
    $("sel-reset").addEventListener("click", () => { state.sel = { kind: "FR", code: "FR" }; render(); });
    initMap();
    try {
      await load();
      initPeriod();
      $("map-loading").hidden = true;
      await render();
      // préchargement en arrière-plan, une fois la page affichée : stations du carburant affiché, séries des autres
      setTimeout(() => {
        loadStations(state.fuel).catch(() => {});
        for (const [k] of FUEL_OPTS) loadSeries(k).catch(() => {});
      }, 500);
    } catch (e) {
      $("maj").textContent = "Impossible de charger les données (" + e.message + ").";
      console.error(e);
    }
  }

  init();
})();
