# Internship submission: how to reproduce

Everything below runs offline on a laptop CPU. No API key is needed.

## 1. Install (once)

```bat
git clone https://github.com/ADITYAMAWA04/PARSERFLOW.git
cd PARSERFLOW
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Tesseract is needed for scanned pages: on Windows, use the UB-Mannheim installer
(https://github.com/UB-Mannheim/tesseract/wiki). On Linux run `sudo apt install tesseract-ocr`;
on macOS run `brew install tesseract`.

## 2. One command: a folder of PDFs to one Markdown file per PDF

```bat
python -m backend.cli path\to\pdfs --output path\to\markdown --workers 4
```

* Each `name.pdf` becomes `name.md`, and sub-folders are mirrored. Use `--top-only` to skip
  sub-folders.
* `--workers 1` (the default) converts one file at a time.
* Exit code 0 means every file was converted. 1 means some files failed; they are listed at the end
  and the others are still written.

## 3. The official score

AllenAI's scorer, run without modification:

```bat
git clone https://github.com/allenai/olmocr.git ..\olmocr
pip install -e "..\olmocr[bench]"
playwright install chromium
pip install -U huggingface_hub
huggingface-cli download --repo-type dataset allenai/olmOCR-bench --local-dir olmOCR-bench
python bench\run_benchmark.py olmOCR-bench\bench_data --engine parseflow --workers 4
```

The last command converts all 1,403 PDFs with `bench/convert.py`. This writes one Markdown file per
page, in the layout the scorer expects. It then runs `python -m olmocr.bench.benchmark` and prints
the per-category table and the overall score. Two files are saved in
`olmOCR-bench\bench_data\pf_parseflow\`:

* `_official_score.txt`: the scorer's full output;
* `_run.json`: git commit, machine, timings (median / p90 seconds per page), and failures.

Seconds per page means document wall time divided by its pages, measured while `--workers`
documents run in parallel. For a latency figure comparable to a single-process parser, use
`--workers 1` (slower overall).

To share a run for analysis without the PDFs:

```bat
python bench\pack_results.py olmOCR-bench\bench_data --candidate pf_parseflow
```

## 4. Regression check (every change)

```bat
python bench\regression_check.py --olmocr ..\olmocr
```

This scores AllenAI's 20-PDF sample with the same scorer. It fails if the score drops below the
recorded floor (`bench/expected_sample_score.json`), and `python -m pytest -q` runs the unit tests.

## Honest notes

* The 20-PDF sample was used during development; its score (78.8 % at v0.7.0) overstates the full
  benchmark. Only the full run above counts.
* Handwriting, scanned equations and text drawn as graphics need a recognition model. The
  offline pipeline keeps and flags those regions, but reads little of them.
* An optional `vlm` engine (`--engine vlm`, see docs/BENCHMARK.md) sends pages to AllenAI's
  olmOCR-2 model through an API. It is not part of the offline result, it needs a key, and it is
  far slower than 0.4 s/page.
