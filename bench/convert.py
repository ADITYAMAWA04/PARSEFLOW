"""Convert olmOCR-Bench PDFs to Markdown in the layout the OFFICIAL scorer expects:
    <bench_dir>/<candidate>/<relative/path/stem>_pg<page>_repeat1.md

Then score with AllenAI's unmodified scorer:
    python -m olmocr.bench.benchmark --dir <bench_dir> --candidate <candidate>

Engines
  parseflow : the current pipeline in ./backend (offline, CPU)
  vlm       : every page through a document OCR model (default AllenAI olmOCR-2-7B) over an
              OpenAI-compatible API - set VLM_SERVER / VLM_API_KEY / VLM_MODEL (see docs/BENCHMARK.md).
              Pages the model cannot do fall back to the parseflow engine and are counted.
  baseline  : the original pipeline + original frontend md() (pass --baseline-backend pointing at a
              checkout of the original commit, e.g. `git worktree add ../pf_baseline c3a5917`)

Timing methodology (written to <out>/_timings.jsonl and summarised in <out>/_run.json):
  * each PDF is converted in a fresh call inside a worker process; wall-clock time covers opening
    the PDF through producing Markdown for every page (rendering, OCR, tables, math, assembly);
  * seconds_per_page = document wall time / page count; the median and percentiles are taken over
    ALL documents that were attempted (failures count with their time until failure);
  * use --workers 1 for latency numbers that are not affected by CPU contention.
"""
import argparse
import glob
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

try:                      # not available on Windows
    import resource
except ImportError:
    resource = None

REPO = Path(__file__).resolve().parent.parent
_ENGINE = None


def _init(engine, baseline_backend):
    global _ENGINE
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # keep tesseract single-threaded per worker
    os.environ.setdefault("OCR_WORKERS", "1")
    if engine == "baseline":
        sys.path.insert(0, str(Path(baseline_backend).resolve()))
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from processing import pipeline  # noqa
        import baseline_md  # noqa

        def run(pdf):
            prog = pipeline.Progress()
            import tempfile
            out_dir = os.path.join(tempfile.gettempdir(), "pf_baseline_tmp", str(os.getpid()))
            doc = pipeline.process(pdf, os.path.basename(pdf), "bench", out_dir, prog)
            pages = sorted({b["page"] for b in doc["blocks"]} | {p["n"] for p in doc["pages"]})
            out = {}
            for n in pages:
                d = dict(doc, blocks=[b for b in doc["blocks"] if b["page"] == n])
                out[n] = baseline_md.md(d)
            return out, len(doc["pages"])
    else:
        sys.path.insert(0, str(REPO / "backend"))
        from processing import config, pipeline, markdown  # noqa
        config.load_dotenv()

        def local(pdf):
            doc = pipeline.process_file(pdf)
            out = {}
            for p in doc["pages"]:
                n = p["n"]
                d = dict(doc, blocks=[b for b in doc["blocks"] if b["page"] == n], pages=[p])
                out[n] = markdown.render(d)
            return out

        if engine == "vlm":
            from processing import vlm_ocr  # noqa

            def run(pdf):
                pages, st = vlm_ocr.convert_pdf(pdf, local_pages=local)
                return pages, st["pages"], st
        else:
            def run(pdf):
                out = local(pdf)
                return out, len(out)
    _ENGINE = run


def _convert(pdf, rel, out_dir):
    t0 = time.perf_counter()
    rec = dict(pdf=rel, ok=False)
    try:
        res = _ENGINE(pdf)
        pages_md, n = res[0], res[1]
        if len(res) > 2:
            rec["vlm"] = res[2]
        stem = os.path.splitext(rel)[0]
        for page, text in pages_md.items():
            target = os.path.join(out_dir, f"{stem}_pg{page}_repeat1.md")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(text)
        rec.update(ok=True, pages=n)
    except Exception as e:  # a failing PDF must not stop the run
        rec.update(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-800:], pages=1)
    rec["seconds"] = time.perf_counter() - t0
    rec["seconds_per_page"] = rec["seconds"] / max(1, rec.get("pages") or 1)
    rec["worker_peak_rss_mb"] = _peak_rss_mb()
    return rec


def _peak_rss_mb():
    if resource is not None:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    try:
        import psutil
        mi = psutil.Process().memory_info()
        return getattr(mi, "peak_wset", mi.rss) / 1048576
    except Exception:
        return None


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def env_info(engine, args):
    def cmd(c):
        try:
            return subprocess.run(c, capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            return None
    info = dict(engine=engine, python=sys.version.split()[0], platform=platform.platform(), machine=platform.machine(),
                cpu_count=os.cpu_count(), cpu_model=None, mem_total_gb=None,
                git_commit=cmd(["git", "-C", str(REPO), "rev-parse", "HEAD"]),
                git_dirty=bool(cmd(["git", "-C", str(REPO), "status", "--porcelain"])),
                tesseract=cmd(["tesseract", "--version"]).splitlines()[0] if cmd(["tesseract", "--version"]) else None,
                workers=args.workers, env={k: v for k, v in os.environ.items() if k in (
                    "MAX_PAGES", "OCR_DPI", "OCR_WORKERS", "STRIP_HEADERS_FOOTERS", "MATH_FROM_TEXT_LAYER",
                    "BORDERLESS_TABLES", "FIGURE_OCR", "VISION_PROVIDER", "VISION_ENABLED", "VISION_MODEL", "VLM_MODEL",
                    "VLM_IMAGE_DIM", "VLM_MAX_RETRIES")},
                vision_key_configured=bool(os.getenv("VISION_API_KEY")))
    try:
        info["cpu_model"] = next(l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name"))
        info["mem_total_gb"] = round(int(next(l.split()[1] for l in open("/proc/meminfo") if l.startswith("MemTotal"))) / 1048576, 1)
    except Exception:
        pass
    try:
        import pymupdf
        info["pymupdf"] = pymupdf.VersionBind
        info["mupdf"] = pymupdf.VersionFitz
    except Exception:
        pass
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bench_dir", help="olmOCR-Bench data dir (contains pdfs/ and *.jsonl)")
    ap.add_argument("--candidate", default="parseflow")
    ap.add_argument("--engine", choices=["parseflow", "vlm", "baseline"], default="parseflow")
    ap.add_argument("--baseline-backend", default=str(REPO.parent / "pf_baseline" / "backend"))
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="only the first N PDFs (sorted) - partial run")
    args = ap.parse_args()

    pdf_root = os.path.join(args.bench_dir, "pdfs")
    pdfs = sorted(glob.glob(os.path.join(pdf_root, "**", "*.pdf"), recursive=True))
    if args.limit:
        pdfs = pdfs[: args.limit]
    out_dir = os.path.join(args.bench_dir, args.candidate)
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.perf_counter()
    recs = []
    with ProcessPoolExecutor(args.workers, initializer=_init, initargs=(args.engine, args.baseline_backend)) as ex:
        futs = {ex.submit(_convert, p, os.path.relpath(p, pdf_root), out_dir): p for p in pdfs}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            recs.append(r)
            if not r["ok"]:
                print(f"FAILED {r['pdf']}: {r.get('error')}", file=sys.stderr)
            if i % 25 == 0 or i == len(pdfs):
                print(f"{i}/{len(pdfs)} converted", file=sys.stderr)
    wall = time.perf_counter() - t0
    with open(os.path.join(out_dir, "_timings.jsonl"), "w") as f:
        for r in sorted(recs, key=lambda r: r["pdf"]):
            f.write(json.dumps(r) + "\n")
    spp = [r["seconds_per_page"] for r in recs]
    summary = dict(env_info(args.engine, args), n_pdfs=len(pdfs), n_failed=sum(not r["ok"] for r in recs),
                   failed=[r["pdf"] for r in recs if not r["ok"]], total_pages=sum(r.get("pages") or 0 for r in recs),
                   wall_seconds=round(wall, 2),
                   seconds_per_page=dict(median=_pct(spp, .5), p25=_pct(spp, .25), p75=_pct(spp, .75), p90=_pct(spp, .9),
                                         p99=_pct(spp, .99), max=max(spp) if spp else None,
                                         mean=statistics.mean(spp) if spp else None),
                   peak_worker_rss_mb=max((r["worker_peak_rss_mb"] or 0 for r in recs), default=None))
    if args.engine == "vlm":
        v = [r["vlm"] for r in recs if r.get("vlm")]
        sys.path.insert(0, str(REPO / "backend"))
        from processing import vlm_ocr  # noqa
        summary["vlm"] = dict(vlm_ocr.public(), renderers=sorted({x["renderer"] for x in v if x.get("renderer")}),
                              pages_by_model=sum(x["pages"] - len(x["fallback_pages"]) for x in v),
                              pages_fallback_to_parseflow=sum(len(x["fallback_pages"]) for x in v),
                              attempts=sum(x["attempts"] for x in v), input_tokens=sum(x["input_tokens"] for x in v),
                              output_tokens=sum(x["output_tokens"] for x in v))
    with open(os.path.join(out_dir, "_run.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: summary[k] for k in ("n_pdfs", "n_failed", "total_pages", "wall_seconds", "seconds_per_page", "vlm") if k in summary}, indent=2))


if __name__ == "__main__":
    main()
