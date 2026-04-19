"""
Fetches and caches the FEATURES.md document from the FinAmigo Flutter repo.
This is the single source of truth for all Instagram content claims.
"""

import os
import requests

FEATURES_URL = os.environ.get(
    "FEATURES_URL",
    "https://raw.githubusercontent.com/enugalamanideepreddy/FinAmigo/main/FEATURES.md",
)
CACHE_PATH = os.path.join(os.path.dirname(__file__), "FEATURES_CACHE.md")


def fetch_features() -> str:
    """Fetch FEATURES.md from GitHub, fall back to local cache if fetch fails."""
    gh_pat = os.environ.get("GH_PAT", "")
    headers = {"Authorization": f"token {gh_pat}"} if gh_pat else {}

    try:
        print("[Features] Fetching FEATURES.md from GitHub...")
        r = requests.get(FEATURES_URL, headers=headers, timeout=20)
        r.raise_for_status()
        text = r.text.strip()
        if not text:
            raise RuntimeError("Empty response from GitHub.")
        # Update cache
        try:
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"[Features] Loaded ({len(text)} chars), cache updated.")
        except OSError as e:
            print(f"[Features] WARNING: cache write failed: {e}")
        return text
    except Exception as e:
        print(f"[Features] GitHub fetch failed: {e}")

    # Fall back to local cache
    if os.path.exists(CACHE_PATH):
        print("[Features] Using local cache.")
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                text = f.read().strip()
            if not text:
                raise RuntimeError("Cache file is empty.")
            return text
        except OSError as e:
            raise RuntimeError(f"Cache file exists but could not be read: {e}")

    raise RuntimeError(
        f"Cannot load FEATURES.md — GitHub fetch failed and no local cache at {CACHE_PATH}."
    )
