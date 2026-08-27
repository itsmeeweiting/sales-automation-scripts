"""
cadence_store.py
-----------------
Tiny JSON-backed CRUD store for the cadences you want the Salesloft
pipeline able to target. This is the backing data for the new
"Cadence settings" page -- add a cadence once here (label + the
numeric Salesloft cadence ID), and every future import/staging run
picks from this list instead of a hardcoded cadence name/regex.

Only the numeric `id` is ever trusted at automation time -- the
pipeline always re-fetches the cadence's current *name* live from
Salesloft (GET /v2/cadences/{id}) right before it needs to match
text on the profile page, so a rename in Salesloft can never make
this store stale in a way that breaks the automation. The `name`
field stored here is just a friendly label for the settings UI.

Drop this file next to your Flask app.py (or wherever your other
*_store-style modules live) and wire up the routes described in
INTEGRATION_NOTES.md.
"""
import json
from pathlib import Path
from threading import Lock

STORE_PATH = Path(__file__).parent / "cadences.json"
_lock = Lock()


def _load():
    if not STORE_PATH.exists():
        return []
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(cadences):
    STORE_PATH.write_text(json.dumps(cadences, indent=2, ensure_ascii=False), encoding="utf-8")


def list_cadences():
    """Returns all saved cadences, most-recently-added first."""
    return list(reversed(_load()))


def get_cadence(cadence_id):
    cadence_id = int(cadence_id)
    for c in _load():
        if c["id"] == cadence_id:
            return c
    return None


def add_cadence(label, cadence_id, name=""):
    label = (label or "").strip()
    cadence_id = int(cadence_id)
    if not label:
        raise ValueError("Label can't be empty.")
    with _lock:
        cadences = _load()
        if any(c["id"] == cadence_id for c in cadences):
            raise ValueError(f"Cadence ID {cadence_id} is already saved.")
        cadences.append({"id": cadence_id, "label": label, "name": (name or "").strip()})
        _save(cadences)
    return get_cadence(cadence_id)


def update_cadence(cadence_id, label=None, name=None):
    cadence_id = int(cadence_id)
    with _lock:
        cadences = _load()
        found = None
        for c in cadences:
            if c["id"] == cadence_id:
                if label is not None:
                    c["label"] = label.strip()
                if name is not None:
                    c["name"] = name.strip()
                found = c
                break
        if found is None:
            raise ValueError(f"Cadence ID {cadence_id} not found.")
        _save(cadences)
    return found


def delete_cadence(cadence_id):
    cadence_id = int(cadence_id)
    with _lock:
        cadences = _load()
        remaining = [c for c in cadences if c["id"] != cadence_id]
        if len(remaining) == len(cadences):
            raise ValueError(f"Cadence ID {cadence_id} not found.")
        _save(remaining)
    return remaining
