#!/usr/bin/env python3
"""
iNat x historical illustration -> TRMNL OG 2-bit PNG.

Pipeline:
  1. Pick an observation (iNat API, or a local fixture with --fixture)
  2. Find art for its taxon: cache -> Commons (BHL-tagged illustrations) -> PhyloPic -> none
     Walks up the taxonomy (species -> genus -> family) when nothing is found.
  3. Process art for 4-gray e-ink (grayscale, levels, sharpen, ordered dither)
  4. Compose an 800x480 screen with observation metadata and source credit
  5. Save a 2-bit PNG under TRMNL OG's 90,000-byte limit, plus an enlarged preview

Run:  python build.py --user dmcwo
      python build.py --fixture fixtures/sample_observation.json   (no network)
"""
import argparse
import datetime
import io
import json
import os
import random
import re
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 800, 480
ART_W = 470                      # left panel width for the illustration
GRAYS = [0, 85, 170, 255]        # 4 levels; check against trmnl.com/api/palettes
MAX_BYTES = 90_000               # TRMNL OG firmware limit for PNGs
UA = {"User-Agent": "trmnl-inat-plates/0.1 (personal project; contact: you@example.com)"}

ROOT = Path(__file__).parent
CACHE_PATH = ROOT / "cache.json"
OUT_DIR = ROOT / "out"            # local scratch (gitignored): monitor preview only
LIBRARY_DIR = ROOT / "library"     # published archive: one PNG per run, plus latest.png
LIBRARY_INDEX = LIBRARY_DIR / "index.json"


# ---------------------------------------------------------------- data sources

def http_json(url, params=None):
    import requests
    r = requests.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def http_bytes(url):
    import requests
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.content


def pick_observation(user=None, lat=None, lng=None, radius=None, days=30):
    """Return one observation dict from the iNat v1 API."""
    params = {"per_page": 100, "photos": "true", "order_by": "observed_on",
              "rank": "species,subspecies,genus"}
    if user:
        params["user_login"] = user
    if lat is not None:
        from datetime import date, timedelta
        params.update(lat=lat, lng=lng, radius=radius or 10,
                      d1=(date.today() - timedelta(days=days)).isoformat())
    data = http_json("https://api.inaturalist.org/v1/observations", params)
    results = [o for o in data.get("results", []) if o.get("taxon")]
    if not results:
        sys.exit("No observations returned.")
    return random.choice(results)


def pick_recent(user, count):
    """Return the `count` most recent observations for a user, newest first, no randomness
    (unlike pick_observation, which samples one at random from a wider recent pool)."""
    params = {"user_login": user, "per_page": count, "order": "desc", "order_by": "observed_on",
              "photos": "true", "rank": "species,subspecies,genus"}
    data = http_json("https://api.inaturalist.org/v1/observations", params)
    results = [o for o in data.get("results", []) if o.get("taxon")]
    if not results:
        sys.exit("No observations returned.")
    return results


def taxon_chain(obs):
    """Taxon IDs to try, most specific first: the taxon, then ancestors up to family."""
    t = obs["taxon"]
    chain = [t["id"]]
    # ancestor_ids runs root -> self; reverse it and stop around family level.
    ancestors = [a for a in reversed(t.get("ancestor_ids", [])) if a != t["id"]]
    chain += ancestors[:3]
    return chain


def wikidata_qid_for_inat(taxon_id):
    q = f'SELECT ?item WHERE {{ ?item wdt:P3151 "{taxon_id}" }} LIMIT 1'
    data = http_json("https://query.wikidata.org/sparql", {"query": q, "format": "json"})
    rows = data["results"]["bindings"]
    return rows[0]["item"]["value"].rsplit("/", 1)[-1] if rows else None


def commons_illustration(qid):
    """Search Commons for files that depict the taxon and are typed as illustrations.
    Q178659 = illustration. P687 = BHL page ID (restricts to BHL-sourced files)."""
    search = f"haswbstatement:P180={qid} haswbstatement:P31=Q178659 haswbstatement:P687"
    data = http_json("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": search, "gsrnamespace": 6, "gsrlimit": 10,
        "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 1200,
    })
    pages = list(data.get("query", {}).get("pages", {}).values())
    if not pages:
        return None
    p = random.choice(pages)
    info = p["imageinfo"][0]
    meta = info.get("extmetadata", {})
    strip = lambda k: _strip_html(meta.get(k, {}).get("value", ""))
    return {
        "source": "commons",
        "url": info.get("thumburl") or info["url"],
        "page": info.get("descriptionurl"),
        "credit": strip("ObjectName") or p["title"].removeprefix("File:"),
        "artist": strip("Artist"),
        "date": strip("DateTimeOriginal"),
        "license": strip("LicenseShortName"),
    }


def phylopic_silhouette(scientific_name):
    """GBIF name match -> PhyloPic resolve -> primary image raster."""
    m = http_json("https://api.gbif.org/v1/species/match", {"name": scientific_name})
    key = m.get("usageKey")
    if not key:
        return None
    data = http_json("https://api.phylopic.org/resolve/gbif.org/species",
                     {"objectIDs": key, "embed_primaryImage": "true"})
    img = data.get("_embedded", {}).get("primaryImage")
    if not img:
        return None
    rasters = img.get("_links", {}).get("rasterFiles", [])
    if not rasters:
        return None
    best = max(rasters, key=lambda r: int(r.get("sizes", "0x0").split("x")[0]))
    return {
        "source": "phylopic",
        "url": best["href"],
        "page": "https://www.phylopic.org/images/" + img.get("uuid", ""),
        "credit": "Silhouette via PhyloPic",
        "artist": img.get("attribution") or "",
        "license": license_label(img.get("_links", {}).get("license", {}).get("href", "")),
    }


def _strip_html(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "untitled"


def license_label(value):
    """Shorten a Creative Commons/public-domain URL to a readable label for on-screen
    attribution. Commons already gives a short label (e.g. "Public domain"); anything
    that isn't a URL passes through unchanged."""
    if not value or not value.startswith("http"):
        return value
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([\w-]+)/([\d.]+)", value)
    if not m:
        return value
    kind, variant, version = m.groups()
    if kind == "publicdomain":
        return "CC0" if variant == "zero" else "Public domain"
    return f"CC {variant.upper()} {version}"


def find_art(obs, cache):
    """Cache first (your curated overrides win), then live lookups."""
    for tid in taxon_chain(obs):
        if str(tid) in cache:
            return cache[str(tid)], tid
    for tid in taxon_chain(obs):
        try:
            qid = wikidata_qid_for_inat(tid)
            art = commons_illustration(qid) if qid else None
        except Exception as e:
            print(f"  commons lookup failed for {tid}: {e}")
            art = None
        if art:
            art["wikidata"] = qid
            cache[str(tid)] = art
            return art, tid
    try:
        art = phylopic_silhouette(obs["taxon"]["name"])
    except Exception as e:
        print(f"  phylopic lookup failed: {e}")
        art = None
    if art:
        cache[str(obs["taxon"]["id"])] = art
        return art, obs["taxon"]["id"]
    return None, None


# ---------------------------------------------------------------- image processing

BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0


def to_four_gray(img, box, paper_cut=0.40, ink_cut=0.02, dither=True):
    """Fit art into box, push paper to white, quantize to 4 grays with ordered dither.
    paper_cut: in line art the paper covers most of the plate, so anything brighter than
    this percentile is treated as paper and becomes pure white. Raise it (0.6-0.8) for
    densely shaded plates or paintings; lower it for sparse line drawings.
    Ordered dither compresses far better than Floyd-Steinberg, which matters for 90 KB."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        # PhyloPic silhouettes are black-on-transparent; dropping alpha straight to
        # grayscale reads the hidden black RGB and paints the whole box solid black.
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img.convert("RGBA")).convert("RGB")
    g = ImageOps.grayscale(img)
    g = g.filter(ImageFilter.MedianFilter(3))   # kill paper grain at full res, before
    g = ImageOps.contain(g, box, Image.LANCZOS)  # downscaling, so fine hatching survives
    g = g.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=2))
    a = np.asarray(g, dtype=np.float32) / 255.0
    lo = np.quantile(a, ink_cut)
    hi = np.quantile(a, paper_cut) - 0.05       # margin so paper noise doesn't speckle
    a = np.clip((a - lo) / max(hi - lo, 1e-3), 0, 1)       # aged paper -> white
    levels = len(GRAYS) - 1
    if dither:
        th = np.tile(BAYER4, (a.shape[0] // 4 + 1, a.shape[1] // 4 + 1))[: a.shape[0], : a.shape[1]]
        q = np.floor(a * levels + th).clip(0, levels)
    else:
        q = np.round(a * levels)
    out = np.take(np.array(GRAYS, dtype=np.uint8), q.astype(int))
    return Image.fromarray(out, "L")


# ---------------------------------------------------------------- layout

def load_font(size, bold=False, italic=False):
    """Put regular.ttf, bold.ttf, italic.ttf in ./fonts (any OFL serif), or set TRMNL_FONT_DIR."""
    d = Path(os.environ.get("TRMNL_FONT_DIR", ROOT / "fonts"))
    want = "bold" if bold else "italic" if italic else "regular"
    for ext in ("ttf", "otf"):
        f = d / f"{want}.{ext}"
        if f.exists():
            return ImageFont.truetype(str(f), size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def wrap(draw, text, font, width):
    words, lines, line = text.split(), [], ""
    for w in words:
        test = (line + " " + w).strip()
        if draw.textlength(test, font=font) <= width:
            line = test
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def compose(obs, art, art_img, matched_rank_note):
    screen = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(screen)

    # Left: the plate (or a quiet placeholder)
    if art_img is not None:
        x = (ART_W - art_img.width) // 2
        y = (H - art_img.height) // 2
        screen.paste(art_img, (x, y))
    else:
        d.text((40, H // 2), "no plate found", font=load_font(18, italic=True), fill=170)
    d.line([(ART_W + 14, 30), (ART_W + 14, H - 30)], fill=85, width=1)

    # Right: observation metadata
    t = obs["taxon"]
    x0, x1 = ART_W + 34, W - 24
    y = 36
    common = (t.get("preferred_common_name") or t["name"]).title()
    for line in wrap(d, common, load_font(30, bold=True), x1 - x0):
        d.text((x0, y), line, font=load_font(30, bold=True), fill=0)
        y += 36
    d.text((x0, y + 2), t["name"], font=load_font(20, italic=True), fill=0)
    y += 44

    rows = [
        ("Observed", obs.get("observed_on") or "unknown date"),
        ("Where", obs.get("place_guess") or "location hidden"),
        ("By", obs.get("user", {}).get("login", "")),
    ]
    small = load_font(16)
    label = load_font(13, bold=True)
    for k, v in rows:
        if not v:
            continue
        d.text((x0, y), k.upper(), font=label, fill=85)
        y += 18
        for line in wrap(d, v, small, x1 - x0)[:2]:
            d.text((x0, y), line, font=small, fill=0)
            y += 20
        y += 8

    # Bottom right: provenance of the image. Always shown; this is the point.
    if art:
        credit = art.get("credit", "")
        bits = [b for b in (art.get("artist"), art.get("date"), art.get("license")) if b]
        lines = wrap(d, credit, load_font(13, italic=True), x1 - x0)[:2]
        lines += wrap(d, " · ".join(bits), load_font(12), x1 - x0)[:2]
        if matched_rank_note:
            lines.append(matched_rank_note)
        yy = H - 28 - 16 * len(lines)
        d.line([(x0, yy - 10), (x0 + 60, yy - 10)], fill=85, width=1)
        for line in lines:
            d.text((x0, yy), line, font=load_font(13, italic=True), fill=85)
            yy += 16
    return screen


def save_2bit(img, path):
    """Save as a 4-color palettized PNG at bit depth 2, and report its size."""
    pal = []
    for g in GRAYS:
        pal += [g, g, g]
    p = Image.new("P", (1, 1))
    p.putpalette(pal + [0] * (768 - len(pal)))
    q = img.convert("RGB").quantize(palette=p, dither=Image.Dither.NONE)
    q.save(path, format="PNG", bits=2, optimize=True)
    return path.stat().st_size


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="iNat login, e.g. dmcwo")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lng", type=float)
    ap.add_argument("--radius", type=float, help="km")
    ap.add_argument("--fixture", help="local observation JSON (skips the iNat API)")
    ap.add_argument("--art", help="local image to use as the plate (skips art lookup)")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--rotate", type=int, choices=[0, 90, 180, 270],
                     help="rotate the plate clockwise before processing; overrides cache.json")
    ap.add_argument("--count", type=int, default=1,
                     help="process this many of the most recent observations in one run (--user only)")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    OUT_DIR.mkdir(exist_ok=True)
    LIBRARY_DIR.mkdir(exist_ok=True)

    if args.fixture:
        observations = [json.loads(Path(args.fixture).read_text())]
    elif args.count > 1:
        if not args.user:
            sys.exit("--count requires --user.")
        observations = pick_recent(args.user, args.count)
    elif args.user or args.lat is not None:
        observations = [pick_observation(args.user, args.lat, args.lng, args.radius)]
    else:
        sys.exit("Give --user, --lat/--lng, or --fixture.")

    # Process oldest-to-newest so index.json ends up newest-first: each run inserts
    # at position 0, so processing newest last puts it on top.
    for obs in reversed(observations):
        process_observation(obs, cache, args)

    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def process_observation(obs, cache, args):
    print(f"Observation: {obs['taxon'].get('preferred_common_name')} ({obs['taxon']['name']})")

    if args.art:
        art = {"source": "local", "credit": Path(args.art).stem.replace("_", " ")}
        raw, matched = Image.open(args.art), obs["taxon"]["id"]
    else:
        art, matched = find_art(obs, cache)
        raw = Image.open(io.BytesIO(http_bytes(art["url"]))) if art else None
    note = ""
    if art and matched and matched != obs["taxon"]["id"]:
        note = "Plate shows a related taxon (matched higher in the tree)"
    print(f"Art: {art.get('source') if art else 'none'}  {art.get('page', '') if art else ''}")

    # Large folio plate scans (Audubon/BHL in particular) are often stored sideways,
    # with no EXIF flag to detect it automatically. --rotate lets you check a fix while
    # triaging; once confirmed, save it as "rotate" on the cache.json entry so it sticks.
    rotate = args.rotate if args.rotate is not None else ((art or {}).get("rotate") or 0)
    if raw is not None and rotate:
        raw = raw.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                              270: Image.Transpose.ROTATE_90}[rotate])

    art_img = to_four_gray(raw, (ART_W - 30, H - 40)) if raw is not None else None
    screen = compose(obs, art, art_img, note)

    # Semantically named archive file: run-date + common name + observation id,
    # so repeated runs (or fixture tests) never collide.
    common = obs["taxon"].get("preferred_common_name") or obs["taxon"]["name"]
    today = datetime.date.today().isoformat()
    filename = f"{today}-{slugify(common)}-{obs.get('id', 'fixture')}.png"
    target = LIBRARY_DIR / filename

    size = save_2bit(screen, target)
    if size > MAX_BYTES and art_img is not None:
        print(f"  {size:,} bytes is over the limit; retrying without dither")
        art_img = to_four_gray(raw, (ART_W - 30, H - 40), dither=False)
        size = save_2bit(compose(obs, art, art_img, note), target)
    print(f"Wrote {target} ({size:,} bytes; limit {MAX_BYTES:,})"
          + ("  ** STILL OVER LIMIT **" if size > MAX_BYTES else ""))

    # Fixed, predictable filename for TRMNL's Alias plugin to point at.
    latest = LIBRARY_DIR / "latest.png"
    shutil.copyfile(target, latest)
    print(f"Updated {latest}")

    # 2x preview so you can judge it on a normal monitor; local only, not archived.
    Image.open(target).convert("L").resize((W * 2, H * 2), Image.NEAREST).save(OUT_DIR / "preview@2x.png")

    entries = json.loads(LIBRARY_INDEX.read_text()) if LIBRARY_INDEX.exists() else []
    entries.insert(0, {
        "file": filename,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "bytes": size,
        "observation": {
            "id": obs.get("id"),
            "observed_on": obs.get("observed_on"),
            "place_guess": obs.get("place_guess"),
            "observer": obs.get("user", {}).get("login"),
        },
        "taxon": {
            "id": obs["taxon"]["id"],
            "name": obs["taxon"]["name"],
            "common_name": common,
            "matched_id": matched,
        },
        "art": art,
        "note": note,
    })
    LIBRARY_INDEX.write_text(json.dumps(entries, indent=2))


if __name__ == "__main__":
    main()
