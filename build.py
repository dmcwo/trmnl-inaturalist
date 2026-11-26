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
import subprocess
import sys
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 800, 480
ART_W = 470                      # left panel width for the illustration
GRAYS = [0, 85, 170, 255]        # 4 levels; check against trmnl.com/api/palettes
MAX_BYTES = 90_000               # TRMNL OG firmware limit for PNGs
UA = {"User-Agent": "trmnl-inat-plates/0.1 (personal project; contact: you@example.com)"}

ROOT = Path(__file__).parent
CACHE_PATH = ROOT / "cache.json"
API_CACHE_PATH = ROOT / ".api_cache.json"  # gitignored: memoized Wikidata/Commons/PhyloPic
                                           # lookups, not curated content like cache.json
OUT_DIR = ROOT / "out"            # local scratch (gitignored): monitor preview only
LIBRARY_DIR = ROOT / "library"     # published archive: one PNG per run, plus latest.png
LIBRARY_INDEX = LIBRARY_DIR / "index.json"
REVIEW_DIR = ROOT / "review"        # local scratch (gitignored): --curate's candidate sheet

_session = requests.Session()  # connection keep-alive across the many lookups one run makes


# ---------------------------------------------------------------- data sources

def http_json(url, params=None):
    r = _session.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def http_bytes(url):
    r = _session.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.content


_api_cache = None


def _memoized(section, key, compute):
    """Call compute() once per (section, key) ever, including a None result, and persist
    immediately. A species sharing a genus or family with many others would otherwise
    repeat the same live lookup for every single observation, in every session, until
    one gets promoted into cache.json - and --curate runs in short sessions."""
    global _api_cache
    if _api_cache is None:
        _api_cache = json.loads(API_CACHE_PATH.read_text()) if API_CACHE_PATH.exists() \
            else {"wikidata": {}, "commons": {}, "phylopic": {}}
    section_dict = _api_cache[section]
    if key in section_dict:
        return section_dict[key]
    value = compute()
    section_dict[key] = value
    API_CACHE_PATH.write_text(json.dumps(_api_cache, indent=2))
    return value


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


def pick_recent(user, count, order="desc"):
    """Return up to `count` observations, newest-first by default ("desc", matching
    day-to-day catch-up) or oldest-first ("asc", for a one-time full-collection pass).
    Paginates past iNat's 200-per-page cap as needed, in as few calls as possible."""
    results = []
    page = 1
    while len(results) < count:
        per_page = min(200, count - len(results))
        params = {"user_login": user, "per_page": per_page, "page": page,
                  "order": order, "order_by": "observed_on",
                  "photos": "true", "rank": "species,subspecies,genus"}
        data = http_json("https://api.inaturalist.org/v1/observations", params)
        page_results = data.get("results", [])
        results.extend(o for o in page_results if o.get("taxon"))
        if len(page_results) < per_page:
            break  # that was the last page
        page += 1
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
    def compute():
        q = f'SELECT ?item WHERE {{ ?item wdt:P3151 "{taxon_id}" }} LIMIT 1'
        data = http_json("https://query.wikidata.org/sparql", {"query": q, "format": "json"})
        rows = data["results"]["bindings"]
        return rows[0]["item"]["value"].rsplit("/", 1)[-1] if rows else None
    return _memoized("wikidata", str(taxon_id), compute)


def commons_illustration(qid):
    """Search Commons for files that depict the taxon and are typed as illustrations.
    Q178659 = illustration. P687 = BHL page ID (restricts to BHL-sourced files)."""
    return _memoized("commons", qid, lambda: _commons_illustration_live(qid))


def _commons_illustration_live(qid):
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
    return _memoized("phylopic", scientific_name, lambda: _phylopic_silhouette_live(scientific_name))


def _phylopic_silhouette_live(scientific_name):
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


def inat_photo_art(obs):
    """Fall back to the observation's own photo. Not cached in cache.json - that's keyed
    by taxon, but each observation's photo is specific to that one sighting."""
    photos = obs.get("photos") or []
    if not photos:
        return None
    p = photos[0]
    return {
        "source": "inat",
        "url": p["url"].replace("square.jpg", "large.jpg"),
        "page": f"https://www.inaturalist.org/observations/{obs.get('id', '')}",
        "credit": "Your observation photo",
        "artist": p.get("attribution") or "",
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


def find_all_art_candidates(obs, cache):
    """Unlike find_art (which stops at the first hit), gather every available option
    for curatorial choice: the cached pick if any, then a fresh look at each taxon level,
    then PhyloPic, then the observation's own photo. Doesn't write to cache - that only
    happens once the curator actually picks one (see build_and_archive)."""
    candidates = []
    chain = taxon_chain(obs)
    rank_labels = ["species", "genus", "family"]

    cached_tid = next((tid for tid in chain if str(tid) in cache), None)
    if cached_tid is not None:
        label = "Cached pick" if cached_tid == obs["taxon"]["id"] else "Cached pick (higher taxon)"
        candidates.append({"label": label, "art": cache[str(cached_tid)], "matched": cached_tid, "default": True})

    for tid, rank in zip(chain, rank_labels):
        if tid == cached_tid:
            continue  # already shown above as the cached pick
        try:
            qid = wikidata_qid_for_inat(tid)
            art = commons_illustration(qid) if qid else None
        except Exception as e:
            print(f"  commons lookup failed for {rank}: {e}")
            art = None
        if art:
            art["wikidata"] = qid
            candidates.append({"label": f"Commons ({rank})", "art": art, "matched": tid, "default": False})

    try:
        art = phylopic_silhouette(obs["taxon"]["name"])
    except Exception as e:
        print(f"  phylopic lookup failed: {e}")
        art = None
    if art:
        candidates.append({"label": "PhyloPic silhouette", "art": art, "matched": obs["taxon"]["id"], "default": False})

    photo_art = inat_photo_art(obs)
    if photo_art:
        candidates.append({"label": "Your iNat photo", "art": photo_art, "matched": obs["taxon"]["id"], "default": False})

    return candidates


def reveal(path):
    """Open a file in Finder/Preview (macOS `open`); never fatal if that's unavailable."""
    try:
        subprocess.run(["open", str(path)], check=False)
    except FileNotFoundError:
        print(f"  (open this yourself: {path})")


# ---------------------------------------------------------------- image processing

BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0


def to_four_gray(img, box, paper_cut=0.40, ink_cut=0.02, dither=True, mode="line_art"):
    """Fit art into box, push paper to white, quantize to 4 grays with ordered dither.
    paper_cut: in line art the paper covers most of the plate, so anything brighter than
    this percentile is treated as paper and becomes pure white. Raise it (0.6-0.8) for
    densely shaded plates or paintings; lower it for sparse line drawings.
    Ordered dither compresses far better than Floyd-Steinberg, which matters for 90 KB.

    mode="photo": a real photo has no blank page to find - the "background" is scene
    content (dirt, leaves, sky), so paper_cut's assumption that the brightest pixels are
    blank paper blows out real detail instead. Use a plain symmetric contrast stretch
    instead, and skip the median filter (that's for killing paper grain, not texture)."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        # PhyloPic silhouettes are black-on-transparent; dropping alpha straight to
        # grayscale reads the hidden black RGB and paints the whole box solid black.
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img.convert("RGBA")).convert("RGB")
    g = ImageOps.grayscale(img)
    if mode != "photo":
        g = g.filter(ImageFilter.MedianFilter(3))   # kill paper grain at full res, before
    g = ImageOps.contain(g, box, Image.LANCZOS)  # downscaling, so fine hatching survives
    g = g.filter(ImageFilter.UnsharpMask(radius=1.2, percent=80 if mode == "photo" else 120, threshold=2))
    a = np.asarray(g, dtype=np.float32) / 255.0
    if mode == "photo":
        lo, hi = np.quantile(a, 0.02), np.quantile(a, 0.98)
    else:
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


def wrap_capped(draw, text, font, width, max_lines):
    """wrap(), but truncated to max_lines - trimming any trailing comma/space first and
    marking it with an ellipsis so a cut-off line doesn't just dangle on a comma as if
    the text had ended there."""
    lines = wrap(draw, text, font, width)
    if len(lines) <= max_lines:
        return lines
    lines = lines[:max_lines]
    lines[-1] = lines[-1].rstrip(" ,") + "…"
    return lines


def format_date(s):
    """"2020-07-25" -> "July 25, 2020"; anything that doesn't parse is shown as-is."""
    if not s:
        return s
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d").strftime("%B %-d, %Y")
    except ValueError:
        return s


def clean_place_guess(s):
    """iNat place names occasionally have a doubled or trailing comma when an admin
    level is blank (e.g. "Some Park, , CA")."""
    if not s:
        return s
    return re.sub(r",\s*,", ",", s).strip(" ,")


def compose(obs, art, art_img, matched_rank_note, layout="left"):
    """layout="left" (default) puts the plate on the left, text on the right; "right"
    mirrors it - handy when a plate's subject faces right and reads better with the
    text on the other side."""
    screen = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(screen)

    if layout == "right":
        art_x0, text_x0, text_x1, divider_x = W - ART_W, 24, W - ART_W - 34, W - ART_W - 14
    else:
        art_x0, text_x0, text_x1, divider_x = 0, ART_W + 34, W - 24, ART_W + 14

    # The plate (or a quiet placeholder)
    if art_img is not None:
        x = art_x0 + (ART_W - art_img.width) // 2
        y = (H - art_img.height) // 2
        screen.paste(art_img, (x, y))
    else:
        d.text((art_x0 + 40, H // 2), "no plate found", font=load_font(18, italic=True), fill=170)
    d.line([(divider_x, 30), (divider_x, H - 30)], fill=85, width=1)

    # Observation metadata
    t = obs["taxon"]
    x0, x1 = text_x0, text_x1
    y = 36
    common = (t.get("preferred_common_name") or t["name"]).title()
    for line in wrap(d, common, load_font(30, bold=True), x1 - x0):
        d.text((x0, y), line, font=load_font(30, bold=True), fill=0)
        y += 36
    d.text((x0, y + 2), t["name"], font=load_font(20, italic=True), fill=0)
    y += 44

    rows = [
        ("Observed", format_date(obs.get("observed_on")) or "unknown date", 1),
        ("Location", clean_place_guess(obs.get("place_guess")) or "location hidden", 3),
        ("By", obs.get("user", {}).get("login", ""), 1),
    ]
    small = load_font(16)
    label = load_font(13, bold=True)
    for k, v, max_lines in rows:
        if not v:
            continue
        d.text((x0, y), k.upper(), font=label, fill=85)
        y += 18
        for line in wrap_capped(d, v, small, x1 - x0, max_lines):
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
    ap.add_argument("--layout", choices=["left", "right"], default="left",
                     help="which side the plate appears on (default left); in --curate, "
                          "append l/r to your pick instead, e.g. '2r'")
    ap.add_argument("--count", type=int, default=1,
                     help="process this many of the most recent observations in one run (--user only)")
    ap.add_argument("--new", action="store_true",
                     help="process every recent observation not already in library/ - "
                          "the usual way to pick up where you left off (--user only)")
    ap.add_argument("--photo", action="store_true",
                     help="use the observation's own photo instead of searching for historical art")
    ap.add_argument("--no-photo-fallback", action="store_true",
                     help="when no historical art is found, show text-only instead of the observation's photo")
    ap.add_argument("--force", action="store_true",
                     help="reprocess and overwrite an observation already in library/ (default: skip it)")
    ap.add_argument("--review", action="store_true",
                     help="write out/contact_sheet.png, a grid of every library entry for review, and exit")
    ap.add_argument("--skip", type=int, metavar="OBS_ID",
                     help="mark an archived observation to be left out of device rotation, and exit")
    ap.add_argument("--unskip", type=int, metavar="OBS_ID",
                     help="clear that mark for an archived observation, and exit")
    ap.add_argument("--curate", action="store_true",
                     help="interactive, one observation at a time: see every art option "
                          "rendered as it would look on the device, pick one, build it (--user only)")
    ap.add_argument("--observation", type=int, metavar="OBS_ID",
                     help="target one specific observation directly, fetched by id from iNat")
    ap.add_argument("--order", choices=["asc", "desc"], default="desc",
                     help="oldest-first (asc) or newest-first (desc, default) for "
                          "--new/--curate/--count")
    ap.add_argument("--all", action="store_true",
                     help="with --new/--curate, fetch every matching observation "
                          "(paginated) instead of capping at 200 - for a one-time "
                          "full-collection pass")
    ap.add_argument("--reset", choices=["library", "all"], metavar="{library,all}",
                     help="library: clear library/ (archived files + index.json) to "
                          "re-review everything, keeping cache.json's curated species "
                          "picks; all: also clear cache.json, for a truly blank slate. "
                          "Either way .api_cache.json (just a lookup performance cache) "
                          "is left alone. Asks to confirm, then exits.")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    LIBRARY_DIR.mkdir(exist_ok=True)

    if args.review:
        return write_contact_sheet()
    if args.skip is not None or args.unskip is not None:
        return set_skip_rotation(args.skip if args.skip is not None else args.unskip,
                                  skip=args.skip is not None)
    if args.curate:
        return run_curate(args)
    if args.reset:
        return run_reset(args.reset)

    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    OUT_DIR.mkdir(exist_ok=True)

    if args.fixture:
        observations = [json.loads(Path(args.fixture).read_text())]
    elif args.observation is not None:
        data = http_json(f"https://api.inaturalist.org/v1/observations/{args.observation}")
        results = data.get("results") or []
        if not results:
            sys.exit(f"No observation found with id {args.observation}.")
        observations = [results[0]]
    elif args.new:
        if not args.user:
            sys.exit("--new requires --user.")
        observations = pick_recent(args.user, 100_000 if args.all else 200, order=args.order)
    elif args.count > 1:
        if not args.user:
            sys.exit("--count requires --user.")
        observations = pick_recent(args.user, args.count, order=args.order)
    elif args.user or args.lat is not None:
        observations = [pick_observation(args.user, args.lat, args.lng, args.radius)]
    else:
        sys.exit("Give --user, --lat/--lng, or --fixture.")

    # Process oldest-to-newest so index.json ends up newest-first: each run inserts
    # at position 0, so processing newest last puts it on top.
    results = [process_observation(obs, cache, args) for obs in reversed(observations)]

    CACHE_PATH.write_text(json.dumps(cache, indent=2))
    print_summary(results)


def render_candidate_sheet(candidates):
    """One row (wrapping at 3) of every art candidate for one observation, art-only - no
    metadata panel, since that's identical regardless of which image wins and composing
    it per-candidate would be wasted work. Each tile is run through the real to_four_gray
    pipeline, so what you see is what would actually end up on the device, then upscaled
    for visibility. Labeled with its menu number so the image maps to what you type."""
    box = (ART_W - 30, H - 40)
    tile = 480
    label_h = 50
    cols = min(3, len(candidates)) or 1
    rows = -(-len(candidates) // cols)
    sheet = Image.new("L", (tile * cols, (tile + label_h) * rows), 255)
    d = ImageDraw.Draw(sheet)
    font = load_font(15, bold=True)
    for i, cand in enumerate(candidates):
        r, c = divmod(i, cols)
        x, y = c * tile, r * (tile + label_h)
        art = cand["art"]
        try:
            raw = Image.open(io.BytesIO(http_bytes(art["url"])))
            rotate = art.get("rotate") or 0
            if rotate:
                raw = raw.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                                      270: Image.Transpose.ROTATE_90}[rotate])
            mode = "photo" if art.get("source") == "inat" else "line_art"
            art_img = to_four_gray(raw, box, mode=mode)
            canvas = Image.new("L", box, 255)
            canvas.paste(art_img, ((box[0] - art_img.width) // 2, (box[1] - art_img.height) // 2))
            sheet.paste(canvas.resize((tile, tile), Image.NEAREST), (x, y))
        except Exception as e:
            d.text((x + 8, y + 8), f"(couldn't load: {e})", font=font, fill=0)
        d.rectangle([x, y, x + tile - 1, y + tile + label_h - 1], outline=170)
        star = " *" if cand.get("default") else ""
        d.text((x + 6, y + tile + 4), f"[{i + 1}] {cand['label']}{star}", font=font, fill=0)
    REVIEW_DIR.mkdir(exist_ok=True)
    out_path = REVIEW_DIR / "candidates.png"
    sheet.save(out_path)
    return out_path


def write_contact_sheet():
    """A grid of every library entry, each labeled with exactly what you'd need to find
    and reference it: the filename (the key for --skip/cache.json lookups), the species,
    the art source, and any flags. The single "look at everything at once" review tool."""
    entries = json.loads(LIBRARY_INDEX.read_text()) if LIBRARY_INDEX.exists() else []
    if not entries:
        print("library/ is empty; nothing to review.")
        return
    cols = 3
    thumb_w, thumb_h, label_h = 360, 216, 48
    cell_w, cell_h = thumb_w, thumb_h + label_h
    rows = -(-len(entries) // cols)
    sheet = Image.new("L", (cell_w * cols, cell_h * rows), 255)
    d = ImageDraw.Draw(sheet)
    font = load_font(12)
    for i, e in enumerate(entries):
        r, c = divmod(i, cols)
        x, y = c * cell_w, r * cell_h
        path = LIBRARY_DIR / e["file"]
        if path.exists():
            thumb = Image.open(path).convert("L").resize((thumb_w, thumb_h), Image.NEAREST)
            sheet.paste(thumb, (x, y))
        else:
            d.text((x + 8, y + 8), "(missing file)", font=font, fill=0)
        src = e.get("art", {}).get("source") if e.get("art") else "none"
        flags = " [skip]" if e.get("skip_rotation") else ""
        d.rectangle([x, y, x + cell_w - 1, y + cell_h - 1], outline=170)
        d.text((x + 4, y + thumb_h + 2), e["file"], font=font, fill=0)
        d.text((x + 4, y + thumb_h + 22),
                f"{e['taxon']['common_name']} - obs {e['observation']['id']} - {src}{flags}",
                font=font, fill=85)
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / "contact_sheet.png"
    sheet.save(out_path)
    print(f"Wrote {out_path} ({len(entries)} entries, {cols} per row)")


def run_reset(scope):
    """Clear library/ (archived files + index.json) to re-review everything from
    scratch. scope="all" also clears cache.json; either way .api_cache.json (a lookup
    performance cache, not curated content) is left alone - nothing is lost by keeping
    it, and repopulating it from scratch would just be slower for no benefit."""
    pngs = sorted(p for p in LIBRARY_DIR.glob("*.png") if p.name != "latest.png")
    print(f"This will delete {len(pngs)} file(s) in library/ and reset index.json.")
    if scope == "all":
        print("It will also clear cache.json (your curated species picks).")
    print("(.api_cache.json, the lookup performance cache, is left alone either way.)")
    if input("Type 'yes' to confirm: ").strip().lower() != "yes":
        print("Not confirmed; nothing changed.")
        return
    for p in pngs:
        p.unlink()
    latest = LIBRARY_DIR / "latest.png"
    if latest.exists():
        latest.unlink()
    LIBRARY_INDEX.write_text("[]")
    if scope == "all":
        CACHE_PATH.write_text("{}")
    print(f"Cleared library/ ({len(pngs)} file(s) removed)."
          + (" Cleared cache.json too." if scope == "all" else " cache.json left as-is."))


def set_skip_rotation(obs_id, skip):
    """Mark (or clear) a library entry to be excluded from device rotation, without
    deleting it - for entries that aren't worth fixing but are fine to keep on record."""
    entries = json.loads(LIBRARY_INDEX.read_text()) if LIBRARY_INDEX.exists() else []
    matches = [e for e in entries if e["observation"]["id"] == obs_id]
    if not matches:
        print(f"No library entry found for observation {obs_id}.")
        return
    for e in matches:
        if skip:
            e["skip_rotation"] = True
        else:
            e.pop("skip_rotation", None)
    LIBRARY_INDEX.write_text(json.dumps(entries, indent=2))
    verb = "Marked" if skip else "Cleared"
    for e in matches:
        print(f"{verb} skip_rotation on {e['file']}")


def print_summary(results):
    added = [r for r in results if not r["skipped"]]
    skipped = [r for r in results if r["skipped"]]
    print("\n==== Summary ====")
    print(f"{len(results)} observation(s) processed: {len(added)} added, "
          f"{len(skipped)} already in library (skipped)")
    for r in added:
        print(f"  + {r['file']}  ({r['art_source']})")
    for r in skipped:
        print(f"  = {r['file']}  (already in library; use --force to redo)")
    if added:
        counts = {}
        for r in added:
            counts[r["art_source"]] = counts.get(r["art_source"], 0) + 1
        print("Art sources: " + ", ".join(f"{n} {src}" for src, n in counts.items()))
    total = len(json.loads(LIBRARY_INDEX.read_text())) if LIBRARY_INDEX.exists() else 0
    print(f"library/ now has {total} entr{'y' if total == 1 else 'ies'} total.")


def library_filename(obs):
    """One observation = one file: named by the observation's own date and id, not the
    run date, so re-running over the same observations is a no-op rather than a dupe."""
    common = obs["taxon"].get("preferred_common_name") or obs["taxon"]["name"]
    observed_on = obs.get("observed_on") or "undated"
    return f"{observed_on}-{slugify(common)}-{obs.get('id', 'fixture')}.png"


def process_observation(obs, cache, args):
    common = obs["taxon"].get("preferred_common_name") or obs["taxon"]["name"]
    print(f"Observation: {common} ({obs['taxon']['name']})")

    filename = library_filename(obs)
    if (LIBRARY_DIR / filename).exists() and not args.force:
        print(f"  Already in library as {filename}; skipping (use --force to redo).")
        return {"skipped": True, "file": filename}

    if args.art:
        art = {"source": "local", "credit": Path(args.art).stem.replace("_", " ")}
        matched = obs["taxon"]["id"]
    elif args.photo:
        art = inat_photo_art(obs)
        matched = obs["taxon"]["id"]
    else:
        art, matched = find_art(obs, cache)
        if art is None and not args.no_photo_fallback:
            art = inat_photo_art(obs)
            matched = obs["taxon"]["id"] if art else None

    return build_and_archive(obs, art, matched, cache, args)


def build_and_archive(obs, art, matched, cache, args, layout=None, rotate_override=None):
    """Given a chosen art candidate (or None), render the final screen, save it to
    library/, update latest.png and index.json. Shared by the normal batch flow
    (process_observation) and --curate, once the curator has picked a candidate.
    layout defaults to args.layout ("left") unless --curate's per-observation l/r
    override passed one in explicitly. rotate_override folds into the art dict itself
    (same field cache.json already uses), so a Commons/PhyloPic fix promotes to that
    species' cache entry through the same path an automatic pick does."""
    if layout is None:
        layout = getattr(args, "layout", "left")
    if art and rotate_override is not None:
        art = {**art, "rotate": rotate_override}
    common = obs["taxon"].get("preferred_common_name") or obs["taxon"]["name"]
    filename = library_filename(obs)
    target = LIBRARY_DIR / filename

    raw = None
    if art:
        raw = Image.open(args.art) if art.get("source") == "local" else \
            Image.open(io.BytesIO(http_bytes(art["url"])))
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

    mode = "photo" if art and art.get("source") == "inat" else "line_art"
    art_img = to_four_gray(raw, (ART_W - 30, H - 40), mode=mode) if raw is not None else None
    screen = compose(obs, art, art_img, note, layout=layout)

    size = save_2bit(screen, target)
    if size > MAX_BYTES and art_img is not None:
        print(f"  {size:,} bytes is over the limit; retrying without dither")
        art_img = to_four_gray(raw, (ART_W - 30, H - 40), mode=mode, dither=False)
        size = save_2bit(compose(obs, art, art_img, note, layout=layout), target)
    print(f"Wrote {target} ({size:,} bytes; limit {MAX_BYTES:,})"
          + ("  ** STILL OVER LIMIT **" if size > MAX_BYTES else ""))

    # Fixed, predictable filename for TRMNL's Alias plugin to point at.
    latest = LIBRARY_DIR / "latest.png"
    shutil.copyfile(target, latest)
    print(f"Updated {latest}")

    # 2x preview so you can judge it on a normal monitor; local only, not archived.
    Image.open(target).convert("L").resize((W * 2, H * 2), Image.NEAREST).save(OUT_DIR / "preview@2x.png")

    # A manually chosen Commons/PhyloPic pick becomes that species' default too (same
    # promotion find_art already does for automatic picks) - but not an iNat photo or a
    # local file, which are specific to this one sighting, not the species.
    if art and art.get("source") in ("commons", "phylopic") and matched is not None:
        cache[str(matched)] = art

    entries = json.loads(LIBRARY_INDEX.read_text()) if LIBRARY_INDEX.exists() else []
    entries = [e for e in entries if e.get("file") != filename]  # replacing on --force
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
        "layout": layout,
    })
    LIBRARY_INDEX.write_text(json.dumps(entries, indent=2))

    return {"skipped": False, "file": filename, "art_source": art.get("source") if art else "none"}


def run_curate(args):
    """Interactive, one observation at a time: see every art option rendered as it would
    look on the device, pick one, build it, then move on."""
    if not args.user:
        sys.exit("--curate requires --user.")
    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    OUT_DIR.mkdir(exist_ok=True)

    count = 100_000 if args.all else 200
    queue = [o for o in pick_recent(args.user, count, order=args.order)
             if not (LIBRARY_DIR / library_filename(o)).exists()]
    if not queue:
        print("Nothing new to review.")
        return
    print(f"{len(queue)} observation(s) to review.\n")

    opened_sheet_once = False
    built = skipped = 0
    for idx, obs in enumerate(queue, 1):
        common = obs["taxon"].get("preferred_common_name") or obs["taxon"]["name"]
        print(f"[{idx}/{len(queue)}] {common} ({obs['taxon']['name']}) "
              f"- observed {obs.get('observed_on', 'unknown date')}")

        candidates = find_all_art_candidates(obs, cache)
        if not candidates:
            print("  No art candidates at all (no photo, no historical art) - archiving text-only.")
            result = build_and_archive(obs, None, None, cache, args)
            print(f"  -> {result['file']}  (none)\n")
            built += 1
            continue

        sheet_path = render_candidate_sheet(candidates)
        if not opened_sheet_once:
            reveal(sheet_path)
            opened_sheet_once = True
        print(f"  Candidates: {sheet_path} (Quick Look it again each round - spacebar in Finder)")
        for n, cand in enumerate(candidates, 1):
            star = "  (cached pick)" if cand.get("default") else ""
            print(f"    [{n}] {cand['label']}{star}")

        # Invalid input re-prompts for this same observation - never silently abandons it.
        # Space-separated tokens in any order: a candidate number, l/r for image side,
        # and/or a rotation (90/180/270, or 0 to cancel one) - e.g. "2 r 90", or just "90"
        # alone to rotate the default pick. No separate step; it rides on the same answer.
        picked, quit_now, layout, rotate_override = None, False, None, None
        while picked is None and not quit_now:
            raw = input(f"  Pick 1-{len(candidates)} (Enter = default, s = skip, q = quit; "
                        f"add l/r for side, 90/180/270 to rotate, e.g. '2 r 90'): ").strip().lower()
            command, num_token, layout, rotate_override, invalid = None, None, None, None, False
            for tok in raw.split():
                if tok in ("q", "s"):
                    command = tok
                elif tok in ("l", "r"):
                    layout = {"l": "left", "r": "right"}[tok]
                elif tok in ("0", "90", "180", "270"):
                    rotate_override = int(tok)
                elif tok.isdigit():
                    num_token = tok
                else:
                    invalid = True
            if invalid or (command and (num_token or layout or rotate_override is not None)):
                print(f"  Not a valid choice - pick 1-{len(candidates)}, s, or q "
                      f"(optionally with l/r and/or a rotation).")
            elif command == "q":
                quit_now = True
            elif command == "s":
                skipped += 1
                picked = "skip"
            elif num_token is None:
                picked = next((c for c in candidates if c.get("default")), candidates[0])
            else:
                try:
                    picked = candidates[int(num_token) - 1]
                except IndexError:
                    print(f"  Not a valid choice - pick 1-{len(candidates)}, s, or q "
                          f"(optionally with l/r and/or a rotation).")
        if quit_now:
            break
        if picked == "skip":
            continue

        result = build_and_archive(obs, picked["art"], picked["matched"], cache, args,
                                    layout=layout, rotate_override=rotate_override)
        used_layout = layout or args.layout
        used_rotate = rotate_override if rotate_override is not None else ((picked["art"] or {}).get("rotate") or 0)
        print(f"  -> {result['file']}  ({result['art_source']}, {used_layout} layout"
              + (f", rotated {used_rotate}" if used_rotate else "") + ")\n")
        built += 1

        action = None
        while action not in ("n", "q"):
            action = input("  [n]ext, [r]eview this one, [o]rient (rotate), [q]uit: ").strip().lower()
            if action == "r":
                reveal(OUT_DIR / "preview@2x.png")
            elif action == "o":
                rot_raw = input("    Rotate to 0/90/180/270: ").strip()
                if rot_raw in ("0", "90", "180", "270"):
                    rotate_override = int(rot_raw)
                    result = build_and_archive(obs, picked["art"], picked["matched"], cache, args,
                                                layout=layout, rotate_override=rotate_override)
                    print(f"  -> rebuilt {result['file']} rotated {rotate_override}\n")
                else:
                    print("    Not a valid rotation - 0, 90, 180, or 270.")
        if action == "q":
            break

    CACHE_PATH.write_text(json.dumps(cache, indent=2))
    done = built + skipped
    left = len(queue) - done
    print(f"\nSession complete. Built {built}, skipped {skipped} ({done} of {len(queue)} reviewed)."
          + (f" {left} left for next time." if left else ""))


if __name__ == "__main__":
    main()
