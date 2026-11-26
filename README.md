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
- `--curate`: interactive, one observation at a time — the primary way to review and build. See "Curatorial review" below.
- `--observation <id>`: target one specific observation directly, fetched by id from iNat, instead of picking one for you
- `--new`: process every recent observation not already in `library/`, with whatever's already cached/found — no review, see "Batch mode" below
- `--order {asc,desc}`: for `--new`/`--curate`/`--count`, oldest-first or newest-first (default). Newest-first is the usual day-to-day order; oldest-first is for a one-time pass through your whole collection in chronological order.
- `--all`: with `--new`/`--curate`, fetch every matching observation (paginated past iNat's 200-per-page cap) instead of stopping at 200 - for that same one-time full-collection pass
- `--photo`: use the observation's own photo instead of searching for historical art
- `--no-photo-fallback`: when no historical art is found, show text-only instead of falling back to the observation's photo (the default is to fall back)
- `--rotate {0,90,180,270}`: rotate the plate clockwise before processing — see "Curating" below
- `--force`: reprocess an observation that's already in `library/` (normally skipped — see below)
- `--review`: write `out/contact_sheet.png`, a grid of every library entry, and exit — see "Batch mode"
- `--skip <observation-id>` / `--unskip <observation-id>`: mark or unmark an entry to stay in `library/` but be left out of device rotation, and exit

Each run adds a file to `library/`, the published archive:
- `library/<observed-date>-<common-name>-<observation-id>.png`: that observation's 800x480, 2-bit file (must stay under 90,000 bytes). One file per observation — the filename is the observation's own date and id, not the date you happened to run the script, so it's a stable record of that sighting and re-running over the same observations is a no-op (skipped, not duplicated; pass `--force` to redo one on purpose).
- `library/latest.png`: a copy of the most recently processed file, always under this same name — point TRMNL's Alias plugin at this one, fixed URL
- `library/index.json`: a manifest with one entry per observation (observation, taxon, and full art/attribution metadata), newest-observed-first

`out/preview@2x.png` is a local-only enlarged copy of the most recently processed file, for judging on a normal monitor. `out/` and `review/` (candidate sheets, see "Curatorial review" below) are both gitignored scratch space, never published. After a batch run, a summary lists what was added vs. skipped, with each one's art source.

Run it repeatedly. Each run picks new observations, archives a file per observation in `library/`, and adds any newly found art to `cache.json`. The same species observed on different dates gets a separate entry each time (it's a record of observations, not a one-per-species gallery) — if you later want species-level aggregation (count of sightings, locations, etc.) instead of or alongside this, that's a bigger feature worth designing separately.

## Curatorial review

This is the primary way to use the tool. You can't predict which art option will look best on an 800x480 2-bit screen without seeing it rendered that way, so the default mode is one observation at a time, not batch-then-review:

```bash
python build.py --curate --user dmcwo
```

Each round:
1. It picks the next observation not already in `library/` (same underlying queue as `--new`, consumed one at a time) and gathers *every* available art option: your iNat photo, a Commons illustration at the species/genus/family level if one exists, a PhyloPic silhouette if one exists, and your previously cached pick for that species if there is one.
2. It writes `review/candidates.png` — one row of every option, art-only (no metadata panel; that's identical no matter which image wins, so drawing it per-candidate would be wasted work), each run through the *real* processing pipeline and labeled with a menu number. The first round auto-opens it; after that, **Quick Look it again with spacebar in Finder** rather than reopening in Preview — Preview doesn't reliably notice the file changed underneath it, Quick Look always re-reads from disk.
3. You pick a number in the terminal. Enter accepts the default (your cached pick if there is one, otherwise whatever's listed first). `s` skips the observation for next time; `q` quits the session.
4. It builds the full composed screen from your pick and archives it, exactly like the batch flow. Picking a Commons/PhyloPic option also promotes it to that species' cache default (next time, every observation of that species pre-selects it, cached-pick-and-all — still overridable per observation). A photo or local file is this sighting's own, so it isn't promoted.
5. `[n]ext`, `[r]eview this one` (opens the finished screen, with its text, as a last look — press it again as many times as you want), or `[q]uit`.

Quitting mid-session is fine — it just picks up with whatever's left next time. `--observation <id>` targets one specific observation directly (by id, fetched fresh from iNat) instead of the next one in queue — useful for redoing a single entry after a fix, outside a full session.

### Known gaps

- No way yet to flip through an observation's other photos if `photos[0]` looks bad — it's always the first one. Worth revisiting if this comes up.

## Batch mode

For bulk backfills where you're fine with whatever's cached or auto-found, without reviewing each one:

1. `python build.py --user dmcwo --new` — processes everything not already in `library/`, no prompts.
2. `python build.py --review` — writes `out/contact_sheet.png`, a grid of every entry in `library/`, each labeled with its filename, common name, observation id, and art source, for scanning after the fact.
3. Fix what's wrong the same way as below (`cache.json`, `--rotate`, `--skip`/`--unskip`), then `--force` to re-archive a specific one.

This is the original ingest-then-review flow; keeping it around for fast bulk catch-up, since not every session calls for a careful look at each item.

## API usage

iNat's own API is already used efficiently - observations are fetched in batches of up to 200 (paginated automatically when `--all` needs more), never one call per observation, and nowhere close to its documented 60/minute, 10,000/day limits.

The real repeat-call cost was Wikidata/Commons/PhyloPic during `--curate`'s "gather every candidate" step: two observations sharing a genus or family would repeat the identical lookup for that shared ancestor, every time, in every session, until something gets promoted into `cache.json`. `.api_cache.json` (gitignored - it's a performance cache, not curated content like `cache.json`) memoizes all three lookups, including misses, across every call and every session. Combined with reusing one `requests.Session()` for connection keep-alive, this is what makes frequent short `--curate` sessions (rather than one long one) cheap to run.

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

`to_four_gray()` has two processing modes:
- `mode="line_art"` (default): assumes most of the image is blank page, pushes anything brighter than `paper_cut` (default 0.40) to pure white, and median-filters to kill paper grain. Raise `paper_cut` (0.6-0.8) for densely shaded plates or paintings; lower it for sparse line drawings.
- `mode="photo"` (used automatically for iNat photos): skips the paper-threshold assumption — a photo's background is real scene content, not blank page, so the same threshold either blows out detail or looks muddy. Uses a plain symmetric contrast stretch instead, and skips the median filter (there's no scan grain to kill, just real texture it would blur).

`dither=False` gives flat posterized tones in either mode: cleaner for bold plates, worse for gradients.

## Fonts

Put `regular.ttf`, `bold.ttf`, and `italic.ttf` in `fonts/`. The bundled files are DejaVu Serif as a placeholder; swap in any OFL-licensed serif you like.

## Lookup order

1. `cache.json`
2. Wikimedia Commons: files that depict the taxon's Wikidata item (joined via iNat taxon ID, P3151), typed as illustration, with a BHL page ID
3. Repeat step 2 for the genus and family
4. PhyloPic silhouette (via GBIF name match)
5. The observation's own photo (skip with `--no-photo-fallback`)
6. No art: text-only layout

Even with the photo-specific processing mode (see "Tuning the image"), not every photo comes out well — a genuinely cluttered or low-contrast shot can still look rough at 2-bit. That's exactly what `--curate` is for: compare it against the other options before committing to one.

## Moving to GitHub Actions

Once it looks right locally, push this folder to a GitHub repo, enable GitHub Pages, and keep `.github/workflows/build.yml`. It runs the same command on a schedule, commits the new `library/` files, and publishes them. Point the TRMNL Alias plugin at the Pages URL for `library/latest.png` — that filename never changes, only its contents do.

To dry-run the workflow itself on your machine, [`act`](https://github.com/nektos/act) runs GitHub Actions locally in Docker. It's optional; running `build.py` directly covers almost everything.
