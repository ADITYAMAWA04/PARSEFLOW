"""Pack a benchmark run for analysis (small: Markdown outputs and test files, no PDFs):

    python bench/pack_results.py olmOCR-bench/bench_data --candidate pf_parseflow

Writes parseflow_results_<candidate>.zip with the *.jsonl test files, the candidate's Markdown,
_run.json and _official_score.txt. Share that zip to get the failing tests analysed."""
import argparse
import zipfile
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bench_dir")
    ap.add_argument("--candidate", default="pf_parseflow")
    a = ap.parse_args()
    bench = Path(a.bench_dir)
    cand = bench / a.candidate
    if not cand.is_dir():
        raise SystemExit(f"no such folder: {cand} (run bench/run_benchmark.py first)")
    out = Path(f"parseflow_results_{a.candidate}.zip")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for j in bench.glob("*.jsonl"):
            z.write(j, j.name)
        for f in cand.rglob("*"):
            if f.is_file() and f.suffix in (".md", ".json", ".txt", ".jsonl"):
                z.write(f, str(Path(a.candidate) / f.relative_to(cand)))
                n += 1
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {n} files)")


if __name__ == "__main__":
    main()
