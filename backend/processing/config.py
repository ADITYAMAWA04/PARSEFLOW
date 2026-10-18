"""Central configuration. Every tunable lives here and is read from the environment, so the CLI, the
web server and the benchmark runner behave identically for the same environment."""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


def _bool(name, default):
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_dotenv(path=None):
    """Minimal .env loader (no extra dependency). Never overrides variables already set."""
    p = Path(path) if path else ROOT / ".env"
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = re.sub(r"\s+#.*$", "", v.strip()).strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)
    except (FileNotFoundError, PermissionError):
        pass


def on_serverless():
    return bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


def data_dir():
    """Where uploads, checkpoints and outputs live. Serverless platforms only allow writes to /tmp,
    and /tmp there is ephemeral: anything stored is lost when the instance is recycled."""
    d = os.getenv("PARSEFLOW_DATA_DIR")
    if d:
        return Path(d)
    if on_serverless():
        return Path("/tmp/parseflow")
    return ROOT / "data"


VERSION = "0.8.1"


class Settings:
    """Snapshot of the configuration (read at call time so tests can monkeypatch the environment)."""

    def __init__(self):
        # No application-level page limit by default. The old MAX_PAGES variable (60 in earlier
        # .env files) is deliberately IGNORED so a stale .env cannot silently truncate documents;
        # PARSEFLOW_PAGE_LIMIT is the explicit opt-in cap for constrained hosts (0 = none).
        self.max_pages = _int("PARSEFLOW_PAGE_LIMIT", 0)
        self.legacy_max_pages_ignored = os.getenv("MAX_PAGES") not in (None, "", "0")
        # Cost guard for the optional paid vision API, not a page limit.
        self.max_vision_regions = _int("MAX_VISION_REGIONS", 40)   # images read by AI during processing; more on demand
        self.max_upload_mb = _int("MAX_UPLOAD_MB", 4 if on_serverless() else 200)
        self.render_scale = _float("RENDER_SCALE", 2.0)          # viewer / bbox coordinate scale (2.0 = 144 dpi)
        self.ocr_dpi = _int("OCR_DPI", 300)                       # target DPI for OCR of scanned pages
        self.ocr_lang = os.getenv("OCR_LANG", "eng")
        self.ocr_workers = max(1, _int("OCR_WORKERS", min(4, os.cpu_count() or 1)))
        self.page_timeout_s = _float("PAGE_TIMEOUT_S", 120.0)
        self.strip_headers = _bool("STRIP_HEADERS_FOOTERS", True)
        self.math_from_text_layer = _bool("MATH_FROM_TEXT_LAYER", True)
        self.borderless_tables = _bool("BORDERLESS_TABLES", True)
        self.figure_ocr = _bool("FIGURE_OCR", True)
        self.vision_enabled = _bool("VISION_ENABLED", True)        # only matters when a key is configured
        self.sync_max_pages = _int("SYNC_MAX_PAGES", 25)            # serverless: process inline up to this many pages


def settings():
    return Settings()
