"""One command for the official olmOCR-Bench evaluation (works on Windows, macOS, Linux):

    python bench/run_benchmark.py <bench_data_dir> --engine vlm --workers 8
    python bench/run_benchmark.py <bench_data_dir> --engine parseflow --workers 4     (offline pipeline)

1. converts every PDF with bench/convert.py (one Markdown file per page, the layout the scorer expects),
2. runs AllenAI's UNMODIFIED scorer:  python -m olmocr.bench.benchmark --dir <dir> --candidate <name>,
3. saves the scorer's full output to <dir>/<candidate>/_official_score.txt next to _run.json
   (environment, git commit, model, renderer, tokens, fallback pages, latency percentiles).

Nothing here computes a score itself. See docs/BENCHMARK.md for setup.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bench_dir", help="the bench_data folder (contains pdfs/ and the *.jsonl test files)")
    ap.add_argument("--engine", choices=["vlm", "parseflow"], default="vlm")
    ap.add_argument("--candidate", help="output folder name (default: pf_<engine>)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--skip-convert", action="store_true", help="only re-run the scorer on existing output")
    args = ap.parse_args()
    bench = Path(args.bench_dir).resolve()
    if not (bench / "pdfs").is_dir() or not list(bench.glob("*.jsonl")):
        print(f"error: {bench} must contain pdfs/ and *.jsonl (the olmOCR-Bench bench_data folder)", file=sys.stderr)
        return 2
    cand = args.candidate or f"pf_{args.engine}"
    env = dict(os.environ)
    sys.path.insert(0, str(REPO / "backend"))
    from processing import config
    config.load_dotenv()
    env.update({k: v for k, v in os.environ.items() if k.startswith(("VLM_", "AI_", "OCR_"))})
    if args.engine == "vlm":
        from processing import vlm_ocr
        s = vlm_ocr.settings()
        if not s["configured"]:
            print("error: set VLM_SERVER, VLM_API_KEY and VLM_MODEL in .env first (docs/BENCHMARK.md)", file=sys.stderr)
            return 2
        print(f"OCR model: {s['model']} at {vlm_ocr.public()['server']}")
    try:
        import olmocr.bench.benchmark  # noqa: F401
    except Exception:
        print("error: the official scorer is not installed. Run:\n"
              "  git clone https://github.com/allenai/olmocr.git ../olmocr\n"
              "  pip install -e \"../olmocr[bench]\"\n  playwright install chromium", file=sys.stderr)
        return 2
    if not args.skip_convert:
        print(f"Converting {len(list((bench / 'pdfs').rglob('*.pdf')))} PDFs with engine '{args.engine}' ...", flush=True)
        r = subprocess.run([sys.executable, str(REPO / "bench" / "convert.py"), str(bench), "--candidate", cand,
                            "--engine", args.engine, "--workers", str(args.workers)], env=env)
        if r.returncode != 0:
            print("conversion failed", file=sys.stderr)
            return r.returncode
    print("Scoring with AllenAI's official scorer ...", flush=True)
    r = subprocess.run([sys.executable, "-m", "olmocr.bench.benchmark", "--dir", str(bench), "--candidate", cand],
                       capture_output=True, text=True, env=env)
    out = r.stdout + r.stderr
    target = bench / cand / "_official_score.txt"
    target.write_text(out, encoding="utf-8")
    summary = out[out.find("Final Summary"):] if "Final Summary" in out else out[-4000:]
    summary = "\n".join(l for l in summary.splitlines() if not re.match(r"\s*(Loading tests|Evaluating tests)", l)).strip()
    print(summary)
    run = bench / cand / "_run.json"
    if run.exists():
        d = json.loads(run.read_text())
        print("Pages:", d.get("total_pages"), " failed PDFs:", d.get("n_failed"),
              " median s/page:", round((d.get("seconds_per_page") or {}).get("median") or 0, 3))
        if d.get("vlm"):
            v = d["vlm"]
            print(f"OCR model pages: {v['pages_by_model']}, fell back to offline pipeline: {v['pages_fallback_to_parseflow']}, "
                  f"renderer(s): {', '.join(v['renderers'])}, tokens in/out: {v['input_tokens']}/{v['output_tokens']}")
    m = re.search(r"%s\s*:\s*Average Score:\s*([\d.]+)%%" % re.escape(cand), out)
    print(f"\nFull scorer output saved to {target}")
    jsonls = sorted(p.name for p in bench.glob("*.jsonl"))
    n_pdfs = len(list((bench / "pdfs").rglob("*.pdf")))
    full = n_pdfs >= 1400 and len(jsonls) >= 7
    print(f"Test files scored: {', '.join(jsonls)}  ({n_pdfs} PDFs)")
    if m and full:
        print(f"Official overall score for '{cand}': {m.group(1)} %  (the scorer's mean of the per-category scores, "
              f"as reported on the olmOCR-Bench leaderboard)")
    elif m:
        print(f"Scorer average for '{cand}': {m.group(1)} %  - this is NOT the full benchmark ({n_pdfs} PDFs, "
              f"{len(jsonls)} test files; the full set has 1,403 PDFs).")
    return 0 if r.returncode == 0 else r.returncode


if __name__ == "__main__":
    sys.exit(main())
