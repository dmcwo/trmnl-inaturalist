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

# Normal use: pick up where you left off - everything new since last time
python build.py --user dmcwo --new

# One-off backfill: a specific number of recent observations, deterministic, newest-first
python build.py --user dmcwo --count 10
```

Other flags:
- `--new`: process every recent observation not already in `library/` — see "Triage workflow" below
- `--photo`: use the observation's own photo instead of searching for historical art
- `--no-photo-fallback`: when no historical art is found, show text-only instead of falling back to the observation's photo (the default is to fall back)
- `--rotate {0,90,180,270}`: rotate the plate clockwise before processing — see "Curating" below
- `--force`: reprocess an observation that's already in `library/` (normally skipped — see below)
- `--review`: write `out/contact_sheet.png`, a grid of every library entry, and exit — see "Triage workflow"
- `--skip <observation-id>` / `--unskip <observation-id>`: mark or unmark an entry to stay in `library/` but be left out of device rotation, and exit

Each run adds a file to `library/`, the published archive:
- `library/<observed-date>-<common-name>-<observation-id>.png`: that observation's 800x480, 2-bit file (must stay under 90,000 bytes). One file per observation — the filename is the observation's own date and id, not the date you happened to run the script, so it's a stable record of that sighting and re-running over the same observations is a no-op (skipped, not duplicated; pass `--force` to redo one on purpose).
- `library/latest.png`: a copy of the most recently processed file, always under this same name — point TRMNL's Alias plugin at this one, fixed URL
- `library/index.json`: a manifest with one entry per observation (observation, taxon, and full art/attribution metadata), newest-observed-first

`out/preview@2x.png` is a local-only enlarged copy of the most recently processed file, for judging on a normal monitor. `out/` is gitignored and never published. After a run, a summary lists what was added vs. skipped, with each one's art source.

Run it repeatedly. Each run picks new observations, archives a file per observation in `library/`, and adds any newly found art to `cache.json`. The same species observed on different dates gets a separate entry each time (it's a record of observations, not a one-per-species gallery) — if you later want species-level aggregation (count of sightings, locations, etc.) instead of or alongside this, that's a bigger feature worth designing separately.

## Triage workflow

This is a curation tool, not a fire-and-forget pipeline. Expect to look at what it makes and fix the ones that are wrong. The loop:

1. **Ingest what's new.**
   ```bash
   python build.py --user dmcwo --new
   ```
   This is the normal way to run it day-to-day — no need to guess a `--count`; observations already in `library/` are skipped automatically, so it only processes what's actually new. (It fetches iNat's max page size, 200, so if you're ever more than 200 observations behind, run `--count` with a larger number first.)

2. **Look at everything at once.**
   ```bash
   python build.py --review
   ```
   Writes `out/contact_sheet.png` — a grid of every entry in `library/`, each labeled with its filename, common name, observation id, and art source. That's deliberately everything you'd need to go fix it: the filename is the exact string in `library/`, the observation id is what `--skip`/`--unskip` and `cache.json` lookups key on. Scan it for anything that looks wrong — sideways scans, muddy photos, solid black silhouettes, mismatched illustrations.

3. **Fix what's worth fixing:**
   - **Sideways scan** (Audubon/BHL folio plates): add `"rotate": 90` (or 180/270) to that taxon's `cache.json` entry — check it first with `--rotate 90` before saving it.
   - **Bad or wrong art for a whole species**: edit or delete that taxon's `cache.json` entry (below).
   - **Not worth fixing, but fine to keep on record**: `python build.py --skip <observation-id>` — keeps the entry in `library/`, just excluded from device rotation. `--unskip` reverses it.
   - **One observation's own photo looks bad and there's no historical art to use instead**: no swap command for this yet (see "Known gaps" below).

4. **Re-run to confirm a `cache.json` fix.** Reprocessing an already-archived observation is normally a no-op; add `--force` to regenerate one on purpose.

### Known gaps

- No command yet to target one already-known observation directly for reprocessing, or to pick a different photo than `photos[0]` if the first one looks bad. Both are natural next additions if this keeps coming up.

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
  "license": "Public domain",
  "rotate": 90
}
```

`rotate` is optional. Some large folio plate scans (Audubon/BHL in particular) are stored sideways in the file itself, with no EXIF flag to detect it — this varies plate-by-plate, not by series, so check each one. Use `--rotate {90,180,270}` to preview a fix before saving it to the cache entry.

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
5. The observation's own photo (skip with `--no-photo-fallback`)
6. No art: text-only layout

Not every photo looks good run through the 4-gray pipeline (it's tuned for line art, not photos with busy backgrounds) — that's part of triage: check `out/preview@2x.png`, and if a particular observation's photo came out poorly, either live with it or swap in something better by hand.

## Moving to GitHub Actions

Once it looks right locally, push this folder to a GitHub repo, enable GitHub Pages, and keep `.github/workflows/build.yml`. It runs the same command on a schedule, commits the new `library/` files, and publishes them. Point the TRMNL Alias plugin at the Pages URL for `library/latest.png` — that filename never changes, only its contents do.

To dry-run the workflow itself on your machine, [`act`](https://github.com/nektos/act) runs GitHub Actions locally in Docker. It's optional; running `build.py` directly covers almost everything.
