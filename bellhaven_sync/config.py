"""Runtime configuration. Everything is overridable with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Minimal .env loader (no extra dependency). Real env vars win.
_env = ROOT / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

SITE_BASE = os.environ.get("BH_SITE_BASE", "https://analyst-assessment-production.up.railway.app").rstrip("/")
CRM_BASE = os.environ.get("BH_CRM_BASE", SITE_BASE + "/api/v1").rstrip("/")
CRM_TOKEN = os.environ.get("BH_CRM_TOKEN", "")
DB_PATH = Path(os.environ.get("BH_DB_PATH", ROOT / "state" / "bellhaven_sync.db"))

# The parent company account we are reconciling against. Looked up by name at
# runtime (not hard-coded by id) so the pipeline works on any copy of the CRM.
TARGET_PARENT_NAME = os.environ.get("BH_PARENT_NAME", "Bellhaven Senior Living (Parent Account)")

# Operators the website says were folded into Bellhaven ("About Us" page).
# Accounts still sitting under these parents are the most likely stale links,
# and they rank above unrelated parents when choosing a duplicate survivor.
ACQUIRED_PARENT_NAMES = [
    s.strip() for s in os.environ.get(
        "BH_ACQUIRED_PARENTS",
        "Harborview Care Group (Parent Account),Cedar Trail Communities (Parent Account)",
    ).split(",") if s.strip()
]

REQUEST_TIMEOUT = float(os.environ.get("BH_TIMEOUT", "30"))
NOTE_TAG = "[bellhaven-sync]"
