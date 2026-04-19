"""
GitHub Gist-based persistent draft storage.

Replaces ephemeral GitHub Actions artifacts with a private Gist that persists
across workflow runs indefinitely. The Gist ID is tracked in agent_state.json.
"""

import json
import os
import requests

GH_PAT = os.environ.get("GH_PAT", "")
GIST_FILENAME = "finamigo_draft.json"
GIST_DESCRIPTION = "FinAmigo Instagram Agent — Active Draft"


def _headers() -> dict:
    return {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def save_draft_to_gist(draft: dict, gist_id: str = None) -> str:
    """Create or update a private Gist with the draft JSON. Returns the Gist ID."""
    if not GH_PAT:
        raise RuntimeError("GH_PAT not set — cannot save draft to Gist.")

    payload = {
        "description": GIST_DESCRIPTION,
        "public": False,
        "files": {GIST_FILENAME: {"content": json.dumps(draft, indent=2, ensure_ascii=False)}},
    }
    if gist_id:
        r = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            json=payload, headers=_headers(), timeout=20,
        )
    else:
        r = requests.post(
            "https://api.github.com/gists",
            json=payload, headers=_headers(), timeout=20,
        )
    r.raise_for_status()
    new_id = r.json()["id"]
    print(f"[Gist] Draft {'updated' if gist_id else 'created'}: {new_id}")
    return new_id


def load_draft_from_gist(gist_id: str) -> dict:
    """Fetch the draft JSON from an existing Gist."""
    if not GH_PAT:
        raise RuntimeError("GH_PAT not set — cannot load draft from Gist.")

    r = requests.get(
        f"https://api.github.com/gists/{gist_id}",
        headers=_headers(), timeout=20,
    )
    r.raise_for_status()
    files = r.json().get("files", {})
    if GIST_FILENAME not in files:
        raise RuntimeError(f"Gist {gist_id} does not contain {GIST_FILENAME}.")
    content = files[GIST_FILENAME]["content"]
    return json.loads(content)


def delete_draft_gist(gist_id: str) -> None:
    """Delete the draft Gist after posting or abandoning."""
    if not GH_PAT:
        print("[Gist] GH_PAT not set — cannot delete Gist.")
        return
    r = requests.delete(
        f"https://api.github.com/gists/{gist_id}",
        headers=_headers(), timeout=20,
    )
    if r.status_code == 204:
        print(f"[Gist] Draft Gist {gist_id} deleted.")
    else:
        print(f"[Gist] Warning: delete returned {r.status_code}: {r.text}")
