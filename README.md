# Évolution du prix du gazole

Page web qui montre l'évolution du prix du gazole dans les stations-service de France métropolitaine. Elle s'appuie sur les [données ouvertes de prix-carburants.gouv.fr](https://www.prix-carburants.gouv.fr/rubrique/opendata/), sous Licence Ouverte 2.0.

- Périodes : 48 h, 7 jours, 3 mois, depuis février 2026, depuis janvier 2025.
- Échelles : France, région, département, station (cliquables sur la carte).
- Indicateurs : prix moyen et prix médian, avec une courbe et un tableau de synthèse.
- Carte colorée selon l'évolution sur la période : vert pour les plus fortes baisses, rouge pour les plus faibles baisses ou les hausses.

## Fonctionnement

- `pipeline/build.py` télécharge les flux XML (archives annuelles, année en cours, jours récents, flux instantané). Il en extrait les prix du gazole, puis calcule des fichiers JSON statiques dans `_site/data/` : agrégats par période et par zone, évolution par station, historique brut par département.
- `site/` contient la page (HTML, CSS, JS sans étape de build, avec Leaflet et Chart.js chargés depuis un CDN).
- `.github/workflows/update.yml` recalcule les données toutes les heures et publie le site sur GitHub Pages. Pour l'activer : Settings › Pages › Source : **GitHub Actions**.

Les hypothèses de calcul sont détaillées en bas de la page.

## En local

```bash
pip install numpy
python pipeline/build.py --out _site/data   # nécessite un accès à donnees.roulez-eco.fr
cp site/* _site/ && python -m http.server -d _site
```

Test sans réseau, sur un flux synthétique :

```bash
python tests/make_fixture.py /tmp/fixture.xml
python pipeline/build.py --local-xml /tmp/fixture.xml --out /tmp/out/data --now 2026-11-02T14:00 --no-geojson
```
