# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A-share (Chinese stock market) pattern search tool. Users draw a curve on a canvas, and the backend finds the 5000+ A-share stocks whose recent 60-day price history most closely matches the drawn shape. Results are ranked using a three-stage pipeline: Pearson correlation → Euclidean distance pre-filter → DTW (Sakoe-Chiba band) final ranking.

## Running Locally

```bash
./start.sh          # Creates .venv, installs deps, starts Flask on :5001
```

Or manually:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python backend.py
```

The server starts at `http://localhost:5001`. On first run it downloads `stock_cache.pkl` (~3 MB gz) from the GitHub Release at `v1.0`. Subsequent runs load the local cache instantly.

## Rebuilding the Stock Cache

```bash
.venv/bin/python rebuild_cache.py   # fetches all stocks via yfinance, takes ~10-20 min
```

This atomically replaces `stock_cache.pkl` (writes to `.tmp` then `os.replace`). Intended to run post-market (e.g., 16:30 daily via cron). The cache holds the 60-day z-normalized price matrix (`stock_matrix`, shape `[N_stocks, 60]`, float32) plus `stock_list` with raw prices and dates for display.

## Deployment

**Render.com** (primary): `render.yaml` defines a free-tier web service. The build step runs `download_cache.py` to bake the cache into the image. Gunicorn starts with 1 worker and a 300 s timeout.

**Self-hosted**: `deploy.sh` installs dependencies, runs `download_cache.py`, and installs a systemd service (`stock-search.service`) bound to `:5001`.

## Architecture

The project is intentionally minimal — no build system, no framework router, no test suite.

```
backend.py        Flask API + algorithm logic (single file)
index.html        Entire frontend (single file — HTML + CSS + JS + Canvas)
stocks_list.json  Static list of ~5000 A-share codes and names (committed to repo)
stock_cache.pkl   Runtime cache (gitignored) — built by rebuild_cache.py or downloaded
rebuild_cache.py  Offline cache builder (yfinance, 20 threads)
download_cache.py Pulls pre-built cache from GitHub Release (used at deploy time)
```

### Backend (`backend.py`)

- Module-level `_init()` runs on import — loads `stock_cache.pkl` if present, else spawns a background thread to call `build_cache()` (which downloads from GitHub Release).
- `stock_matrix` is a NumPy float32 array of z-normalized 60-day price vectors, loaded into memory once and reused for every search.
- `POST /api/search` — main search endpoint:
  1. Resample the user's drawn points to 60 samples via `np.interp`.
  2. Z-normalize (`z_norm`).
  3. Vectorized Pearson + Euclidean over the full matrix (one matrix multiply).
  4. Take top 100 candidates, run pure-Python DTW on each.
  5. Final score: `0.5 * pearson_score + 0.5 * dtw_score`, where `DTW_REF = sqrt(60) * 2 ≈ 15.5` anchors the DTW component.
- `POST /api/refresh` — triggers `build_cache()` in a daemon thread.
- `GET /api/status` — returns current loading state (thread-safe via `state_lock`).

### Frontend (`index.html`)

Single-file, no bundler. Key sections (all in `<script>`):
- **Canvas drawing**: captures mouse/touch points into `rawPoints[]`, applies Catmull-Rom spline smoothing before sending to the API.
- **Y-axis auto-expand**: the canvas Y-axis starts at 0 % and dynamically expands up to ±200 % as the user draws near the top. Points are stored in normalized canvas coordinates and re-mapped on the fly.
- **`doSearch()`**: serializes points as `{x, y}` pairs (y is 0-at-bottom in canvas space) and POSTs to `/api/search`. Results render as mini candlestick-style line cards that link to Eastmoney (东方财富) for each stock code.
- **Status polling**: polls `/api/status` every 2 s until `status === "ready"`, updating a status pill and progress bar.

### Data Flow

```
stocks_list.json  ──▶  rebuild_cache.py  ──▶  stock_cache.pkl
                               │ (yfinance, ~20 threads)
                   GitHub Release v1.0  ──▶  download_cache.py  ──▶  stock_cache.pkl

stock_cache.pkl  ──▶  backend.py (_init)  ──▶  in-memory stock_matrix + stock_list
                                                       │
User draws curve  ──▶  POST /api/search  ──▶  Pearson/Euclidean/DTW  ──▶  JSON results
```

## Key Conventions

- **Stock code routing**: codes starting with `"6"` get `.SS` suffix (Shanghai); all others get `.SZ` (Shenzhen/ChiNext).
- **N_DAYS = 60**: all price vectors are exactly 60 trading days. This constant is shared conceptually between `backend.py` and `rebuild_cache.py` — keep them in sync if changed.
- **Z-normalization**: search is shape-based, not price-level-based. Both the stored matrix and the query are z-normalized before comparison.
- **`stocks_list.json` is committed** — it is the static universe of searchable stocks and must stay in the repo (see `.gitignore` comment).
- **`stock_cache.pkl` is gitignored** — it is a large binary artifact regenerated or downloaded at runtime/deploy.
- The frontend sends canvas Y coordinates as `1.0 - p.y` to flip the axis (canvas y=0 is top, but up means price increase).
