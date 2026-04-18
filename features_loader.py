"""
Fetches and caches the FEATURES.md document from the FinAmigo Flutter repo.
This is the single source of truth for all Instagram content claims.
"""

import os
import subprocess
import tempfile

FEATURES_URL = os.environ.get(
    "FEATURES_URL",
    "https://raw.githubusercontent.com/enugalamanideepreddy/FinAmigo/main/FEATURES.md",
)
CACHE_PATH = os.path.join(os.path.dirname(__file__), "FEATURES_CACHE.md")


def fetch_features() -> str:
    """Fetch FEATURES.md from GitHub, fall back to local cache."""
    try:
        print("[Features] Fetching FEATURES.md from GitHub...")
        gh_pat = os.environ.get("GH_PAT", "")

        # Write the auth header to a temp file so the PAT is not visible in the process list
        header_file = None
        cmd = ["curl", "-s", "-m", "15", "-f"]
        if gh_pat:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".hdr", delete=False) as hf:
                hf.write(f"Authorization: token {gh_pat}\n")
                header_file = hf.name
            cmd += ["--header", f"@{header_file}"]
        cmd.append(FEATURES_URL)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        finally:
            if header_file:
                try:
                    os.unlink(header_file)
                except OSError:
                    pass

        if result.returncode == 0 and result.stdout.strip():
            text = result.stdout.strip()
            try:
                with open(CACHE_PATH, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"[Features] Loaded ({len(text)} chars), cache updated.")
            except OSError as e:
                print(f"[Features] WARNING: fetch succeeded but cache write failed: {e}")
            return text
        else:
            raise RuntimeError(f"curl returned {result.returncode}")

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
        f"Cannot load FEATURES.md — GitHub fetch failed and no local cache exists at {CACHE_PATH}."
    )
