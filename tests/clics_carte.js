// Test manuel : les régions restent cliquables après un passage par l'échelle Station.
// Usage : servir _site sur localhost:8765 puis `node tests/clics_carte.js <dossier contenant node_modules leaflet et chart.js>`
const { chromium } = require('playwright');
const S = process.argv[2];
(async () => {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1400, height: 1100 } });
  p.on('pageerror', e => console.log('pageerror', e.message));
  await p.route('**/*', r => {
    const u = r.request().url();
    if (u.includes('leaflet.min.js')) return r.fulfill({ path: S + '/npm/node_modules/leaflet/dist/leaflet.js', contentType: 'text/javascript' });
    if (u.includes('leaflet.min.css')) return r.fulfill({ path: S + '/npm/node_modules/leaflet/dist/leaflet.css', contentType: 'text/css' });
    if (u.includes('chart.umd')) return r.fulfill({ path: S + '/npm/node_modules/chart.js/dist/chart.umd.js', contentType: 'text/javascript' });
    if (u.includes('geopf') || u.includes('cartocdn')) return r.fulfill({ status: 404, body: '' });
    return r.continue();
  });
  await p.goto('http://localhost:8765/index.html'); await p.waitForTimeout(1500);
  const clickRegion = async () => {
    const xy = await p.evaluate(() => { let r; window.__map.eachLayer(l => { if (l.feature && !r) { const c = window.__map.latLngToContainerPoint(l.getBounds().getCenter()); r = c; } }); const b = document.getElementById('map').getBoundingClientRect(); return { x: b.left + r.x, y: b.top + r.y }; });
    await p.mouse.click(xy.x, xy.y); await p.waitForTimeout(500);
    return p.textContent('#sel-title');
  };
  await p.click('#ctl-scale button[data-value=R]'); await p.waitForTimeout(400);
  console.log('avant station :', await clickRegion());
  await p.click('#ctl-scale button[data-value=S]'); await p.waitForTimeout(600);
  await p.click('#ctl-scale button[data-value=R]'); await p.waitForTimeout(400);
  console.log('après station :', await clickRegion());
  await b.close();
})();
