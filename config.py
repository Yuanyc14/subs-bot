from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


ROOT = Path(__file__).resolve().parent
_load_dotenv(ROOT / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8787").rstrip("/")
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8787"))
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "subs.db")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
