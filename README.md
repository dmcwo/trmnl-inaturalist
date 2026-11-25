# iNat plates for TRMNL

Your iNaturalist observation on the right, a historical illustration of the same taxon on the left, rendered as a 2-bit PNG for the TRMNL OG.

## Run it locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 1. Offline test: no network, uses the fixture and test plate
python build.py --fixture fixtures/sample_observation.json --art fixtures/test_plate.jpg

# 2. Real data: random observation of yours, live art lookup
python build.py --user dmcwo

# 3. Recent observations near a point (km radius, last 30 days)
python build.py --lat 32.88 --lng -117.24 --radius 10

# Reproducible pick while tuning
python build.py --user dmcwo --seed 42
```

Output lands in `out/`:
- `trmnl.png`: the real 800x480, 2-bit file (must stay under 90,000 bytes)
- `preview@2x.png`: enlarged copy for judging on a normal monitor

Run it repeatedly. Each run picks a new observation and adds any newly found art to `cache.json`.

## Curating

`cache.json` maps an iNat taxon ID to an image. The script checks the cache before any live lookup, so editing an entry by hand (or adding one) overrides the automatic match. Expect to do this: automatic matches will sometimes be wrong or ugly.

```json
"12345": {
  "source": "commons",
  "url": "https://upload.wikimedia.org/...jpg",
  "page": "https://commons.wikimedia.org/wiki/File:...",
  "credit": "Hyla regilla, Pl. XII",
  "artist": "...",
  "date": "1859",
  "license": "Public domain"
}
```

## Tuning the image

In `to_four_gray()`:
- `paper_cut` (default 0.40): anything brighter than this percentile becomes white paper. Raise it for densely shaded plates; lower it for sparse line drawings.
- `dither=False` gives flat posterized tones: cleaner for bold plates, worse for gradients.

## Fonts

Put `regular.ttf`, `bold.ttf`, and `italic.ttf` in `fonts/`. The bundled files are DejaVu Serif as a placeholder; swap in any OFL-licensed serif you like.

## Lookup order

1. `cache.json`
2. Wikimedia Commons: files that depict the taxon's Wikidata item (joined via iNat taxon ID, P3151), typed as illustration, with a BHL page ID
3. Repeat step 2 for the genus and family
4. PhyloPic silhouette (via GBIF name match)
5. No art: text-only layout

## Moving to GitHub Actions

Once it looks right locally, push this folder to a GitHub repo, enable GitHub Pages, and keep `.github/workflows/build.yml`. It runs the same command on a schedule and publishes `out/trmnl.png`. Point the TRMNL Alias plugin at the Pages URL for that file.

To dry-run the workflow itself on your machine, [`act`](https://github.com/nektos/act) runs GitHub Actions locally in Docker. It's optional; running `build.py` directly covers almost everything.

## Untested parts

The live lookups (iNat, Wikidata, Commons, GBIF, PhyloPic) were written against documented API shapes but have **not been run**, because the environment they were written in couldn't reach those services. Your first `--user dmcwo` run is the real test. If something breaks, the error message names the lookup that failed.
