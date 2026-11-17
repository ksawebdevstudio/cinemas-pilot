# cinemas-pilot

Pilot: find movie theaters near the visitor (by radius) and show showtimes where available.

Status: experimental. The live page for the four Coachella Valley theaters stays at
https://ksawebdevstudio.github.io/movies/ until this pilot replaces it.

## How it works

One static database of theaters, rebuilt monthly by a script from open data, with manual
fixes on top. The page only reads the finished file.

```
fetch -> normalize -> match -> merge -> apply overrides -> check -> publish
```

- `data/regions.json` — regions to build (id, name, bbox). Adding a region = one line.
- `data/overrides.json` — manual fixes; the script never overwrites it.
- `scripts/build_theaters.py` — the whole pipeline (OpenStreetMap + Overture Maps).
- `public/` — what gets published: `theaters.json`, `report.md`, later the page itself.
- `.github/workflows/theaters.yml` — monthly build + manual run. The only GitHub-specific part.

Run locally (any machine with Python 3.11+):

```bash
pip install -r requirements.txt
python scripts/build_theaters.py
```

## Safeguards

- If both sources return nothing, or the theater count drops by more than 10%, nothing is
  published and the previous file stays (`--force` to override after checking).
- Every build writes `public/report.md`: matched / only-in-one-source / closed / changes.

## Data license

`public/theaters.json` contains data from OpenStreetMap and is published under the
[ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/). See `LICENSE-DATA.md`.
