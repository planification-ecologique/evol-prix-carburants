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

## Nettoyage des données

- Prix hors de la plage 1 à 3 €/L écartés ; doublons (même station, même horodatage) supprimés.
- Fautes de saisie écartées : saut d'au moins 0,30 € annulé à 0,05 € près dans les 72 heures (par exemple 2,25 € → 1,25 € → 2,25 €), et prix inférieur de plus de 0,50 € à la médiane nationale du jour. La station garde son prix valide précédent.
- Ruptures de gazole et fermetures : la station n'est pas comptée pendant ces périodes (balises `rupture` et `fermeture` des flux, et ruptures en cours du flux instantané). Sans date de fin, la période s'arrête au prix suivant publié par la station.
- Dernier point : seules les stations qui affichent un prix gazole dans le flux instantané sont comptées (même périmètre que l'export officiel de data.economie.gouv.fr).
- Stations sans coordonnées valides : comptées dans les statistiques, absentes de la carte.

## Audit de la qualité des données

`.github/workflows/audit.yml` (lancement manuel, ou à chaque push sur une branche `claude/audit-…`) recalcule les données, puis `pipeline/audit.py` relit les mêmes flux et refait toute la chaîne de calcul avec une implémentation indépendante. Il compare le résultat aux fichiers publiés, à l'export officiel de data.economie.gouv.fr et aux prix hebdomadaires publiés par le ministère. Le rapport s'affiche dans le résumé de l'exécution (onglet Actions).

## En local

```bash
pip install numpy shapely
python pipeline/build.py --out _site/data   # nécessite un accès à donnees.roulez-eco.fr
cp site/* _site/ && python -m http.server -d _site
```

Test sans réseau, sur un flux synthétique :

```bash
python tests/make_fixture.py /tmp/fixture.xml
python pipeline/build.py --local-xml /tmp/fixture.xml --out /tmp/out/data --now 2026-11-02T14:00 --no-geojson --dump-events /tmp/events.npz
python pipeline/audit.py --local-xml /tmp/fixture.xml --data /tmp/out/data --events /tmp/events.npz
```
