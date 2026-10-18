"""Start ParseFlow and open it in the browser - safely.

Why this exists: the old launcher opened http://localhost:8000 before the server had started. When
an older ParseFlow was still running on port 8000, the new one failed to bind the port and the
browser silently showed the OLD app. This launcher:
  * checks what is already listening on 8000-8010,
  * reuses a running ParseFlow only if it is this exact version,
  * otherwise warns loudly and starts on the next free port,
  * opens the browser only after the server answers, at the port it really uses.

    python backend/start.py            (run.bat / run.sh call this)
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from processing import config  # noqa: E402

VERSION = config.VERSION


def _health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":   # like uvicorn: a just-closed server (TIME_WAIT) does not block the port
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _banner(lines):
    w = max(len(x) for x in lines) + 4
    print("\n" + "=" * w)
    for x in lines:
        print("  " + x)
    print("=" * w + "\n", flush=True)


def ensure_tesseract():
    """Tesseract (free OCR engine) reads scanned pages and text in images offline. If it is missing,
    install it once with the system's package manager (Windows: winget, macOS: Homebrew), asking
    nothing more than the installer itself does. Set PARSEFLOW_NO_AUTO_INSTALL=1 to skip."""
    import shutil
    import subprocess
    from processing import ocr
    if ocr.available() or os.getenv("PARSEFLOW_NO_AUTO_INSTALL") == "1":
        return bool(ocr.available())
    marker = HERE.parent / "data" / ".tesseract_install_tried"
    if marker.exists() and time.time() - marker.stat().st_mtime < 7 * 86400:
        return False                          # tried recently: don't block every start
    cmd = None
    if os.name == "nt" and shutil.which("winget"):
        cmd = ["winget", "install", "--id", "UB-Mannheim.TesseractOCR", "-e", "--silent",
               "--accept-package-agreements", "--accept-source-agreements"]
    elif sys.platform == "darwin" and shutil.which("brew"):
        cmd = ["brew", "install", "tesseract"]
    if not cmd:
        print("Tip: install Tesseract OCR to read scanned pages offline "
              "(Linux: sudo apt install tesseract-ocr; Windows: https://github.com/UB-Mannheim/tesseract/wiki).")
        return False
    print("Installing the Tesseract OCR engine (one time, about 1 minute). If Windows asks for permission, click Yes.",
          flush=True)
    try:
        subprocess.run(cmd, timeout=600, check=False)
    except Exception as e:
        print(f"Could not install Tesseract automatically ({type(e).__name__}); ParseFlow works without it.")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()))
    except OSError:
        pass
    ocr._version.cache_clear()
    ocr._locate_tesseract()
    ok = bool(ocr.available())
    print("Tesseract OCR is ready." if ok else "Tesseract was not found after installing; scanned pages are read by AI when it is connected.")
    return ok


def main():
    no_browser = "--no-browser" in sys.argv
    if "--no-install" not in sys.argv:
        ensure_tesseract()
    port = None
    for p in range(8000, 8011):
        if _free(p):
            port = p
            break
        h = _health(p)
        if h and h.get("version") == VERSION:
            _banner([f"ParseFlow {VERSION} is already running: http://localhost:{p}"])
            if not no_browser:
                webbrowser.open(f"http://localhost:{p}")
            return 0
        if h:
            _banner([f"WARNING: an OLDER ParseFlow ({h.get('version') or 'before 0.4'}) is running on port {p}.",
                     "Close that terminal window to stop it. This version starts on another port."])
    if port is None:
        print("No free port between 8000 and 8010. Close other servers and try again.")
        return 1
    url = f"http://localhost:{port}"

    def open_when_ready():
        for _ in range(120):
            h = _health(port)
            if h and h.get("version") == VERSION:
                ai = h.get("llm") or {}
                _banner([f"ParseFlow {VERSION} is running:  {url}",
                         "OCR (Tesseract): " + (h.get("ocr") or "not installed (scanned pages are read by AI when it is connected)"),
                         "AI: " + (f"{ai.get('provider')} / {ai.get('model')}" if ai.get("configured")
                                   else "not connected - click 'Connect AI' in the app for answers and handwriting"),
                         "Keep this window open. Press Ctrl+C to stop."])
                if not no_browser:
                    webbrowser.open(url)
                return
            time.sleep(0.5)
        print("The server did not start; see the messages above.")

    threading.Thread(target=open_when_ready, daemon=True).start()
    os.chdir(HERE)
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
