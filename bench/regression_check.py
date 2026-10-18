"""Quality gate for every code change: convert the official olmOCR-Bench SAMPLE set (shipped inside
the allenai/olmocr repository), score it with the unmodified official scorer, and fail if the score
drops below the recorded floor in bench/expected_sample_score.json.

This is a regression check on a 20-PDF development sample, NOT the official benchmark score.

    python bench/regression_check.py --olmocr /path/to/olmocr/checkout
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--olmocr", required=True, help="path to a checkout of https://github.com/allenai/olmocr")
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()
    sample = Path(args.olmocr) / "olmocr" / "bench" / "sample_data"
    floor = json.loads((REPO / "bench" / "expected_sample_score.json").read_text())
    with tempfile.TemporaryDirectory() as td:
        bench = Path(td) / "sample"
        shutil.copytree(sample / "pdfs", bench / "pdfs")
        for j in sample.glob("*.jsonl"):
            shutil.copy(j, bench / j.name)
        subprocess.run([sys.executable, str(REPO / "bench" / "convert.py"), str(bench), "--candidate", "parseflow",
                        "--workers", str(args.workers)], check=True)
        r = subprocess.run([sys.executable, "-m", "olmocr.bench.benchmark", "--dir", str(bench), "--candidate", "parseflow"],
                           capture_output=True, text=True, cwd=str(Path(args.olmocr)))
        out = r.stdout + r.stderr
    m = re.search(r"dataset\.jsonl\s*:\s*([\d.]+)%", out)
    if not m:
        print(out[-3000:])
        print("could not parse the scorer output", file=sys.stderr)
        return 2
    score = float(m.group(1))
    print(f"sample dataset.jsonl score: {score:.1f}% (floor {floor['dataset_jsonl_min']}%)")
    if score < floor["dataset_jsonl_min"]:
        print("REGRESSION: score fell below the recorded floor", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
