"""Check the AI connections from the command line:   python backend/ai_check.py

1. The AI for Ask and images: shows which provider/key ParseFlow will use and tries a tiny request on
   every model the key can use, printing the provider's own answer (e.g. HTTP 503 "overloaded", 429 quota).
2. The high-accuracy OCR server (VLM_SERVER, e.g. olmOCR-2), if set: sends one small test page and
   prints the server's answer and how long it took."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from processing import config  # noqa: E402

config.load_dotenv()
from processing import llm  # noqa: E402


def check_vlm():
    import tempfile
    import time
    import httpx
    import pymupdf
    from processing import vlm_ocr
    cfg = vlm_ocr.app_settings()
    if not cfg["configured"]:
        print("High-accuracy OCR: not configured (VLM_SERVER is empty). Pages are read by ParseFlow's own pipeline.\n")
        return 0
    print(f"High-accuracy OCR server: {vlm_ocr.public()['server']}   model: {cfg['model']}")
    try:
        h = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
        r = httpx.get(cfg["server"] + "/models", headers=h, timeout=20)
        if r.status_code == 200:
            ids = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]
            print(f"  model list: HTTP 200, {len(ids)} models; '{cfg['model']}' " + ("is listed" if cfg["model"] in ids else "is NOT listed"))
        else:
            print(f"  model list: HTTP {r.status_code} {vlm_ocr._detail(r)}")
    except Exception as e:
        print(f"  model list: cannot reach the server ({type(e).__name__}: {str(e)[:100]})")
    with tempfile.TemporaryDirectory() as td:
        pdf = f"{td}/test.pdf"
        d = pymupdf.open()
        pg = d.new_page()
        pg.insert_text((72, 90), "ParseFlow OCR test page", fontsize=20)
        pg.insert_text((72, 130), "The quick brown fox jumps over the lazy dog.", fontsize=12)
        d.save(pdf)
        d.close()
        cfg.update(max_retries=1, backoff_tries=1, page_budget=120)
        t = time.time()
        try:
            res = vlm_ocr.transcribe_page(pdf, 1, cfg)
        except vlm_ocr.VLMError as e:
            print(f"  test page: FAILED after {time.time() - t:.1f} s: {e}\n")
            return 2
    if res["ok"]:
        print(f"  test page: OK in {time.time() - t:.1f} s: {res['text'][:80]!r}\n")
        return 0
    print(f"  test page: FAILED after {time.time() - t:.1f} s: {'; '.join(res['errors'])[:300]}\n")
    return 2


def main():
    vlm_rc = check_vlm()
    cfg = llm.config("text")
    if not cfg["configured"]:
        print("No AI configured for Ask and images. Click 'Connect AI' in the app, or set AI_API_KEY in .env.")
        return vlm_rc or 1
    print(f"Provider: {cfg['provider']}   default model: {cfg['model']}")
    print("Trying a tiny request on each model (this takes a few seconds)...\n")
    try:
        rows = llm.diagnose(cfg)
    except llm.AIError as e:
        print("Could not list models:", e)
        return 1
    for r in rows:
        mark = "OK  " if r["ok"] else "FAIL"
        print(f"{mark} {r['model']:<32} HTTP {r['status']}  {r['seconds']:>5}s  {r['detail'][:110]}")
    good = [r["model"] for r in rows if r["ok"]]
    print("\n" + (f"Working model(s): {', '.join(good)}. ParseFlow will use them automatically." if good else
                  "No model answered. If every line says 503/overloaded, Google is busy: try again later. "
                  "If every line says 429, the free-tier quota is used up for now."))
    return 0 if good else 2


if __name__ == "__main__":
    sys.exit(main())
