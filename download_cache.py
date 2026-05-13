#!/usr/bin/env python3
"""Download stock cache during Render build phase (not at runtime)."""
import gzip, os, sys

CACHE_URL = ("https://github.com/drunksnow1988/stock-pattern-search"
             "/releases/download/v1.0/stock_cache.pkl.gz")
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_cache.pkl")

def main():
    if os.path.exists(CACHE_FILE):
        print(f"Cache already exists ({os.path.getsize(CACHE_FILE):,} bytes), skipping.")
        return

    import requests
    print(f"Downloading {CACHE_URL} ...")
    resp = requests.get(CACHE_URL, stream=True, timeout=300,
                        headers={"Accept-Encoding": "identity"})
    resp.raise_for_status()

    chunks, downloaded = [], 0
    for chunk in resp.iter_content(chunk_size=65536):
        if chunk:
            chunks.append(chunk)
            downloaded += len(chunk)
            if downloaded % (256 * 1024) < 65536:
                print(f"  {downloaded // 1024} KB ...", flush=True)

    print("Decompressing ...")
    raw = gzip.decompress(b"".join(chunks))

    with open(CACHE_FILE, "wb") as f:
        f.write(raw)
    print(f"Cache saved: {os.path.getsize(CACHE_FILE):,} bytes")

if __name__ == "__main__":
    main()
