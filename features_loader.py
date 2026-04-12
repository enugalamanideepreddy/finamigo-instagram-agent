"""
Fetches and caches the FEATURES.md document from the FinAmigo Flutter repo.
This is the single source of truth for all Instagram content claims.
"""

import os
import subprocess

FEATURES_URL = os.environ.get(
    "FEATURES_URL",
    "https://raw.githubusercontent.com/enugalamanideepreddy/FinAmigo/main/FEATURES.md",
)
CACHE_PATH = os.path.join(os.path.dirname(__file__), "FEATURES_CACHE.md")


def fetch_features() -> str:
    """Fetch FEATURES.md from GitHub, fall back to local cache."""
    try:
        print("[Features] Fetching FEATURES.md from GitHub...")
        result = subprocess.run(
            ["curl", "-s", "-m", "15", "-f", FEATURES_URL],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0 and result.stdout.strip():
            text = result.stdout.strip()
            with open(CACHE_PATH, "w") as f:
                f.write(text)
            print(f"[Features] Loaded ({len(text)} chars), cache updated.")
            return text
        else:
            raise RuntimeError(f"curl returned {result.returncode}")
    except Exception as e:
        print(f"[Features] GitHub fetch failed: {e}")

    # Fall back to local cache
    if os.path.exists(CACHE_PATH):
        print("[Features] Using local cache.")
        with open(CACHE_PATH) as f:
            return f.read().strip()

    raise RuntimeError(
        "Cannot load FEATURES.md — GitHub fetch failed and no local cache exists. "
        "Run with FEATURES_URL set or place FEATURES_CACHE.md alongside this script."
    )
