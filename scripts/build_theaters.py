#!/usr/bin/env python3
"""Build the movie theater database from OpenStreetMap + Overture Maps.

Pipeline: fetch -> normalize -> match -> merge -> apply overrides -> check -> publish.

Usage:
    python scripts/build_theaters.py            # normal run
    python scripts/build_theaters.py --force    # skip the "count dropped" safeguard

Outputs (only written when every check passes):
    public/theaters.json   - the database the page reads
    public/report.md       - what changed and what needs a human look
"""

import argparse
import datetime as dt
import difflib
import json
import math
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGIONS_FILE = ROOT / "data" / "regions.json"
OVERRIDES_FILE = ROOT / "data" / "overrides.json"
OUT_FILE = ROOT / "public" / "theaters.json"
REPORT_FILE = ROOT / "public" / "report.md"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "cinemas-pilot/0.1 (+https://github.com/ksawebdevstudio/cinemas-pilot)"

MATCH_RADIUS_M = 300          # max distance for OSM <-> Overture pairing
NAME_SIMILARITY = 0.6         # difflib ratio on normalized names
MAX_DROP = 0.10               # refuse to publish if count drops more than this

# Overture category values we treat as a movie theater (confirmed in report).
OVERTURE_CINEMA_CATS = {"movie_theater", "cinema", "drive_in_theater", "drive_in_movie_theater"}
# Names that look like cinemas; used only for the diagnostics section of the report.
CINEMA_NAME_HINT = re.compile(r"cinema|theat|movie|film|imax|regal|cinemark|pickford|drive.?in", re.I)

NAME_NOISE = {"the", "theatre", "theater", "theatres", "theaters", "cinema", "cinemas",
              "movie", "movies", "and", "at", "&"}


# ---------------------------------------------------------------- helpers

def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def norm_text(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).split()


def norm_name(s):
    return " ".join(w for w in norm_text(s) if w not in NAME_NOISE)


def similar(a, b):
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    if difflib.SequenceMatcher(None, na, nb).ratio() >= NAME_SIMILARITY:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return len(ta & tb) / max(1, min(len(ta), len(tb))) >= 0.5


def slugify(s):
    return "-".join(norm_text(s)) or "theater"


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


# ---------------------------------------------------------------- sources

def fetch_osm(region):
    w, s, e, n = region["bbox"]
    query = f'[out:json][timeout:90];nwr["amenity"="cinema"]({s},{w},{n},{e});out center tags;'
    data = urllib.parse.urlencode({"data": query}).encode()
    last_err = None
    for attempt in range(3):  # one endpoint, polite retries, no parallel requests
        try:
            req = urllib.request.Request(OVERPASS_URL, data=data, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.load(resp)
            break
        except Exception as err:  # noqa: BLE001
            last_err = err
            time.sleep(30 * (attempt + 1))
    else:
        raise RuntimeError(f"Overpass failed: {last_err}")

    out = []
    for el in payload.get("elements", []):
        t = el.get("tags", {})
        lat = el.get("lat", el.get("center", {}).get("lat"))
        lon = el.get("lon", el.get("center", {}).get("lon"))
        if lat is None or lon is None:
            continue
        street = " ".join(x for x in [t.get("addr:housenumber"), t.get("addr:street")] if x)
        city_line = ", ".join(x for x in [t.get("addr:city"), " ".join(
            x for x in [t.get("addr:state"), t.get("addr:postcode")] if x)] if x)
        out.append({
            "src_id": f'{el["type"]}/{el["id"]}',
            "name": t.get("name") or "",
            "brand": t.get("brand") or t.get("operator") or "",
            "lat": lat, "lon": lon,
            "address": ", ".join(x for x in [street, city_line] if x),
            "city": t.get("addr:city") or "",
            "website": t.get("website") or t.get("contact:website") or "",
            "imax": t.get("cinema:IMAX") == "yes",
            "closed": False,
        })
    return out


def fetch_overture(region, release):
    import shapely  # installed with overturemaps
    from overturemaps import core

    w, s, e, n = region["bbox"]
    reader = core.record_batch_reader("place", bbox=(w, s, e, n), release=release)
    if reader is None:
        raise RuntimeError("Overture returned no reader")

    cinemas, hints = [], []
    for batch in reader:
        cols = [c for c in batch.schema.names if c != "bbox"]
        for row in batch.select(cols).to_pylist():
            name = ((row.get("names") or {}).get("primary")) or ""
            tax = row.get("taxonomy") or {}
            cats = {row.get("basic_category"), tax.get("primary"), *(tax.get("hierarchy") or [])}
            cats.discard(None)
            is_cinema = bool(cats & OVERTURE_CINEMA_CATS)
            if not is_cinema and not CINEMA_NAME_HINT.search(name):
                continue
            geom = shapely.from_wkb(row["geometry"])
            pt = geom if geom.geom_type == "Point" else geom.representative_point()
            rec = {
                "src_id": row.get("id"),
                "name": name,
                "brand": (((row.get("brand") or {}).get("names") or {}).get("primary")) or "",
                "lat": pt.y, "lon": pt.x,
                "address": "",
                "city": "",
                "website": (row.get("websites") or [""])[0] or "",
                "imax": False,
                "closed": (row.get("operating_status") or "open") != "open",
                "operating_status": row.get("operating_status") or "",
                "confidence": row.get("confidence"),
                "datasets": sorted({(x or {}).get("dataset", "") for x in (row.get("sources") or [])} - {""}),
                "basic_category": row.get("basic_category"),
                "taxonomy_primary": tax.get("primary"),
            }
            addrs = row.get("addresses") or []
            if addrs:
                a = addrs[0] or {}
                rec["city"] = a.get("locality") or ""
                rec["address"] = ", ".join(x for x in [a.get("freeform"), a.get("locality"), " ".join(
                    x for x in [a.get("region"), a.get("postcode")] if x)] if x)
            (cinemas if is_cinema else hints).append(rec)
    return cinemas, hints


# ---------------------------------------------------------------- matching & merge

def match(osm, ovt, forced):
    """Return list of (osm_rec|None, ovt_rec|None)."""
    pairs, used_o, used_v = [], set(), set()
    osm_by_id = {r["src_id"]: r for r in osm}
    ovt_by_id = {r["src_id"]: r for r in ovt}
    for o_id, v_id in forced:
        if o_id in osm_by_id and v_id in ovt_by_id:
            pairs.append((osm_by_id[o_id], ovt_by_id[v_id]))
            used_o.add(o_id)
            used_v.add(v_id)

    candidates = []
    for o in osm:
        if o["src_id"] in used_o:
            continue
        for v in ovt:
            if v["src_id"] in used_v:
                continue
            d = haversine_m(o["lat"], o["lon"], v["lat"], v["lon"])
            if d > MATCH_RADIUS_M:
                continue
            brand_ok = bool(o["brand"]) and norm_name(o["brand"]) == norm_name(v["brand"])
            if brand_ok or similar(o["name"], v["name"]):
                candidates.append((d, o, v))
    for d, o, v in sorted(candidates, key=lambda x: x[0]):
        if o["src_id"] in used_o or v["src_id"] in used_v:
            continue
        pairs.append((o, v))
        used_o.add(o["src_id"])
        used_v.add(v["src_id"])

    pairs += [(o, None) for o in osm if o["src_id"] not in used_o]
    pairs += [(None, v) for v in ovt if v["src_id"] not in used_v]
    return pairs


def merge(o, v, region_id):
    first, second = (o, v) if o else (v, None)

    def pick(key):
        for r in (first, second):
            if r and r.get(key):
                return r[key]
        return first.get(key)

    return {
        "id": None,
        "name": pick("name"),
        "brand": pick("brand"),
        "lat": round(first["lat"], 6),
        "lon": round(first["lon"], 6),
        "address": pick("address"),
        "website": pick("website"),
        "imax": bool((o or {}).get("imax")),
        "sources": [s for s, r in (("osm", o), ("overture", v)) if r],
        "source_ids": {s: r["src_id"] for s, r in (("osm", o), ("overture", v)) if r},
        "confidence": "high" if (o and v) else "low",
        "region": region_id,
        "showtimes_key": None,
        "_closed": bool(v and v["closed"]),
        "_city": pick("city"),
    }


def assign_ids(theaters, previous):
    by_src = {}
    for p in previous:
        for s, sid in (p.get("source_ids") or {}).items():
            by_src[(s, sid)] = p["id"]
    taken = set()
    for t in theaters:  # reuse ids from the previous build
        for s, sid in t["source_ids"].items():
            pid = by_src.get((s, sid))
            if pid and pid not in taken:
                t["id"] = pid
                taken.add(pid)
                break
    for t in theaters:
        if t["id"]:
            continue
        base = slugify(t["name"])
        cand = base
        if cand in taken and t["_city"]:
            cand = f"{base}-{slugify(t['_city'])}"
        n = 2
        while cand in taken:
            cand = f"{base}-{n}"
            n += 1
        t["id"] = cand
        taken.add(cand)


def apply_overrides(theaters, overrides, region_ids):
    by_id = {t["id"]: t for t in theaters}
    for key, ov in overrides.items():
        if key.startswith("_") or not isinstance(ov, dict):
            continue
        if ov.get("add"):
            if key not in by_id:
                t = {"id": key, "name": ov.get("name", key), "brand": "", "lat": ov["lat"], "lon": ov["lon"],
                     "address": "", "website": "", "imax": False, "sources": ["manual"], "source_ids": {},
                     "confidence": "manual", "region": ov.get("region", region_ids[0]), "showtimes_key": None,
                     "_closed": False, "_city": ""}
                theaters.append(t)
                by_id[key] = t
        t = by_id.get(key)
        if not t:
            continue
        for f in ("name", "brand", "address", "website", "imax", "showtimes_key", "lat", "lon"):
            if f in ov:
                t[f] = ov[f]
        if ov.get("hidden"):
            t["_hidden"] = True
        if ov.get("show"):
            t["_closed"] = False
    return theaters


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="skip the count-drop safeguard")
    args = ap.parse_args()

    regions = load_json(REGIONS_FILE, [])
    overrides = load_json(OVERRIDES_FILE, {})
    previous_doc = load_json(OUT_FILE, {})
    previous = previous_doc.get("theaters", [])

    from overturemaps import core
    release = core.get_latest_release()

    forced = [(ov["match"].get("osm"), ov["match"].get("overture"))
              for k, ov in overrides.items()
              if not k.startswith("_") and isinstance(ov, dict) and isinstance(ov.get("match"), dict)]

    theaters, diag = [], {}
    for region in regions:
        osm = fetch_osm(region)
        time.sleep(5)
        ovt, hints = fetch_overture(region, release)
        pairs = match(osm, ovt, forced)
        theaters += [merge(o, v, region["id"]) for o, v in pairs]
        diag[region["id"]] = {"osm": osm, "overture": ovt, "hints": hints, "pairs": pairs}

    assign_ids(theaters, previous)
    apply_overrides(theaters, overrides, [r["id"] for r in regions])

    closed = [t for t in theaters if t["_closed"]]
    hidden = [t for t in theaters if t.get("_hidden")]
    public = [t for t in theaters if not t["_closed"] and not t.get("_hidden")]
    public.sort(key=lambda t: (t["region"], t["name"].lower()))
    clean = [{k: v for k, v in t.items() if not k.startswith("_")} for t in public]

    # ---- safeguards
    total_sources = sum(len(d["osm"]) + len(d["overture"]) for d in diag.values())
    if total_sources == 0:
        sys.exit("ABORT: both sources returned nothing; keeping the previous file.")
    if previous and not args.force and len(clean) < len(previous) * (1 - MAX_DROP):
        sys.exit(f"ABORT: count dropped from {len(previous)} to {len(clean)} (> {int(MAX_DROP*100)}%). "
                 "Run with --force after checking.")

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {
        "generated": now,
        "sources": {"osm": "Overpass API", "overture_release": release},
        "attribution": "© OpenStreetMap contributors (ODbL); Overture Maps Foundation",
        "license": "ODbL-1.0",
        "count": len(clean),
        "theaters": clean,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    REPORT_FILE.write_text(build_report(now, release, clean, previous, closed, hidden, diag), encoding="utf-8")
    print(f"OK: {len(clean)} theaters written (Overture {release}).")


def build_report(now, release, clean, previous, closed, hidden, diag):
    L = [f"# Theaters build report", "", f"Generated: {now} · Overture release: {release}", ""]
    for rid, d in diag.items():
        pairs = d["pairs"]
        both = [p for p in pairs if p[0] and p[1]]
        only_o = [p[0] for p in pairs if p[0] and not p[1]]
        only_v = [p[1] for p in pairs if p[1] and not p[0]]
        L += [f"## Region: {rid}", "",
              f"- OSM cinemas: {len(d['osm'])}", f"- Overture cinemas: {len(d['overture'])}",
              f"- Matched in both: {len(both)}", f"- Only OSM: {len(only_o)}", f"- Only Overture: {len(only_v)}", ""]
        L += ["### Matched", "", "| OSM name | Overture name | distance, m |", "|---|---|---|"]
        for o, v in both:
            L.append(f"| {o['name']} | {v['name']} | {haversine_m(o['lat'], o['lon'], v['lat'], v['lon']):.0f} |")
        L += ["", "### Only in OSM", ""] + [f"- {o['name'] or '(no name)'} — {o['src_id']} — {o['address']}" for o in only_o]
        L += ["", "### Only in Overture", ""] + [
            f"- {v['name']} — {v['address']} — status: {v['operating_status']}, conf: {v['confidence']}, "
            f"datasets: {', '.join(v['datasets'])}" for v in only_v]
        L += ["", "### Diagnostics: Overture places with cinema-like names but other categories", "",
              "| name | basic_category | taxonomy.primary | status |", "|---|---|---|---|"]
        for h in d["hints"]:
            L.append(f"| {h['name']} | {h['basic_category']} | {h['taxonomy_primary']} | {h['operating_status']} |")
        cats = sorted({(v["basic_category"], v["taxonomy_primary"]) for v in d["overture"]}, key=str)
        L += ["", "Category values seen on Overture cinemas: " + "; ".join(f"{a} / {b}" for a, b in cats), ""]

    L += ["## Hidden as closed (Overture operating_status)", ""] + [f"- {t['name']} ({t['id']})" for t in closed]
    L += ["", "## Hidden by overrides", ""] + [f"- {t['name']} ({t['id']})" for t in hidden]

    prev_ids = {p["id"]: p for p in previous}
    new_ids = {t["id"]: t for t in clean}
    added = [new_ids[i] for i in new_ids if i not in prev_ids]
    removed = [prev_ids[i] for i in prev_ids if i not in new_ids]
    changed = [i for i in new_ids if i in prev_ids and any(
        new_ids[i].get(f) != prev_ids[i].get(f) for f in ("name", "address", "website", "lat", "lon"))]
    L += ["", "## Changes since previous build", "",
          f"- Added: {len(added)}", *[f"  - {t['name']} ({t['id']})" for t in added],
          f"- Removed: {len(removed)}", *[f"  - {t['name']} ({t['id']})" for t in removed],
          f"- Changed: {len(changed)}", *[f"  - {i}" for i in changed], ""]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
