# ParseFlow engineering audit

Audited commit: `c3a5917` (state of `main` before this work). Environment used for reproduction:
Linux, Python 3.13, 2 vCPU (Intel Xeon @ 2.80 GHz), 7 GB RAM, Tesseract 5.3.4, PyMuPDF 1.26.5
(built from source because PyPI is not reachable from the audit sandbox).

## How the original system was reproduced

The repository contained no benchmark script. The original backend returns JSON and the browser
builds Markdown with `md()` in `frontend/index.html`, so the baseline was reproduced by running the
original `pipeline.process()` (no vision key, Tesseract available) and feeding its JSON through a
line-for-line Python port of that `md()` function (`bench/baseline_md.py`). Outputs were scored with
AllenAI's **unmodified** scorer (`python -m olmocr.bench.benchmark`).

The full benchmark data (1,403 PDFs, hosted on Hugging Face) could not be downloaded in the audit
sandbox (Hugging Face is blocked there), so the reproduction used the **20-PDF / 99-test sample that
ships inside the official `olmocr` repository** (`olmocr/bench/sample_data`). Results:

| Candidate (sample set, `dataset.jsonl`) | Score |
|---|---|
| Original ParseFlow (reproduced) | **34.3 %** (34/99) |
| olmOCR pipeline outputs bundled with the sample, same scorer | 87.9 % (87/99) |

The original's reported full-benchmark score was 36.7; the sample result (34.3) is consistent with it.
The sample is easier than the full benchmark (olmOCR scores 87.9 here vs 82.4 on the full set), so
sample numbers must never be read as full-benchmark numbers.

## A. Features that genuinely work

* Upload with streamed size check (50 MB), typed error codes (PF-2xx), background job + status polling.
* Digital text extraction via PyMuPDF `get_text("dict")` with font-size heading detection.
* Ruled-table extraction via `page.find_tables()` with an empty-cell filter.
* Tesseract OCR for pages without a text layer, keeping word boxes and confidences.
* Ink-region detection on scanned pages (handwriting / drawings) that OCR could not explain.
* Optional vision adapter (Anthropic / Gemini) with JSON-only prompt, one retry, never raises.
* Verifier that refuses to upgrade model output to "verified" without independent evidence.
* Frontend: page viewer with region overlays, inspector, JSON view, demo, and a pdf.js browser mode.

## B. Features that partially work

* **Reading order**: one heuristic split at `W/2`. Fails on 3+ columns, sidebars, articles laid out
  side by side, and titles that span columns (sample `multi_column_miss`: order tests fail).
* **Paragraphs**: PyMuPDF blocks are emitted 1:1. Scans with a hidden OCR layer produce one block per
  line, so sentences are cut across blocks (sample `small_page_size`: best match 0.79-0.83).
* **Markdown**: built only in the browser; every block is preceded by an HTML provenance comment
  (`<!-- txt_1_02 · p1 · bbox [58,59,...] ... -->`). These comments sit inside sentences that span
  blocks and inject digits that defeat page-number absence tests.
* **Confidence**: digital text is a constant 0.99; fallback regions a constant 0.3; tables
  `0.6 + 0.35*fill`. OCR uses real Tesseract word confidence. Thresholds are documented (0.85 / 0.60).
* **Visual regions**: without a vision key, figures get `content = null`; legible text inside images is
  OCR'd only into `ocr_text`, which never reaches Markdown. Vector-figure labels found in the text layer
  are discarded (`absorb_labels`).
* **Ask**: client-side keyword overlap with hard-coded synonyms tuned to the demo document (AVL
  rotation, FY24 revenue). Returns the best-matching block verbatim; no generation, no multi-block
  synthesis, no LLM.
* **Handwriting / equations**: only through the optional vision API (max 12 regions per document).

## C. Features that are broken

* **Page limit / memory**: every page is rendered at 2x and kept in a Python list, and a PNG is
  encoded for every page *before* analysis starts (O(pages) memory, ~5.8 MB raw per page; a 400-page
  file needs >2 GB just for images). `MAX_PAGES=60` silently truncates with only a warning.
* **De-hyphenation** (`re.sub(r"(\w)- (\w)")`) runs on space-joined lines and destroys genuine
  compounds ("self- consistent" -> "selfconsistent") with no evidence.
* **List detection** has an operator-precedence bug (`bullets and len>=1 and len>1 or (...)`).
* **No header/footer handling at all**: running heads, page numbers and "Downloaded from ..." lines
  are emitted as body text (sample: all 10 header/footer absence tests failed).
* **No math**: equations in the text layer are flattened into Unicode soup, never LaTeX
  (ArXiv Math 0.0, Old Scans Math 0.0).
* **Jobs are process-local** (`JOBS = {}`): lost on restart and invisible to other workers/instances.
  Uploaded files and outputs under `data/` are never cleaned up.
* **File handles**: `open_source()` documents are never closed.
* Dead code: `info_t = info` in VERIFY, `pass` branch in EXTRACT.

## D. Bottlenecks responsible for the 36.7 benchmark score

In decreasing order of estimated impact (from the failing sample tests and the code):

1. **Math = 0** on two whole categories: no LaTeX is ever produced offline.
2. **Headers & footers** are never removed; every absence test fails unless the page has none.
3. **Provenance comments inside Markdown** break sentence-level presence/order tests and add digits.
4. **Line-level blocks** on scans with OCR layers split sentences; no paragraph merging.
5. **Tables**: borderless tables are not detected (no ruling lines -> no table), Markdown pipes are not
   escaped, merged cells are lost (no HTML output), tables inside images are not read.
6. **Reading order**: only a W/2 two-column split.
7. **OCR**: low-confidence words (<0.6) are dropped entirely, losing text; no deskew, orientation or
   illumination correction; 144-dpi rendering is too coarse for long tiny text.

## E. Features that cannot work in the current deployment architecture (Vercel)

Verified against current Vercel documentation (Python runtime, FastAPI, function limits):

* **Import-time crash**: `app.py` does `DATA.mkdir()` under the project root. Vercel functions can
  only write to `/tmp`; this raises at import, so every route fails.
* **Background jobs**: work submitted to a `ThreadPoolExecutor` after the response is returned is not
  guaranteed to run, and `/api/status` may hit a different instance whose in-memory `JOBS` is empty
  ("Unknown Document").
* **Upload size**: Vercel rejects request bodies over **4.5 MB** (`413 FUNCTION_PAYLOAD_TOO_LARGE`)
  before the app sees them; the UI advertised 50 MB.
* **Duration**: 300 s maximum on Hobby (800 s Pro). OCR of long scanned documents does not fit.
* **Tesseract** is a system binary and is not present in the Vercel Python runtime.
* **Dependencies**: `pyproject.toml` declared `dependencies = []`. Vercel accepts dependencies from
  `pyproject.toml` *or* `requirements.txt`; the documentation does not state which wins when both
  exist, so an empty `pyproject.toml` list risks deploying without FastAPI (a likely cause of the
  observed runtime failures - not verified, no build logs were available). The entry point used the
  legacy `[project.scripts] app = ...` form; the documented form is
  `[tool.vercel] entrypoint = "backend.app:app"`.
* `run.sh` / `run.bat` reference `.env.example`, which is correct, but there is no `vercel.json`
  to raise `maxDuration` or exclude test data from the bundle.

## F. Required architectural changes

1. Page-streaming pipeline with per-page checkpoints, resumability, cancellation and bounded OCR
   concurrency; page images rendered on demand instead of up front.
2. Durable job state on disk (`status.json` per job) instead of a process-local dict.
3. Backend-generated Markdown (clean by default; provenance in JSON / optional annotated mode).
4. Document-level passes: header/footer roles, paragraph merging, reading order (XY-cut).
5. A CLI (`python -m backend.cli`) that needs no web server or API key.
6. Serverless mode: synchronous processing for small files, explicit limits, `/tmp` storage, and a
   documented separate worker (or local/CLI processing) for large documents.
7. Grounded Ask on the server: retrieval over blocks + optional LLM adapter with citations.

## G. Dependencies that may cause installation or deployment failures

* `pytesseract` needs the `tesseract` binary (not pip-installable; absent on Vercel).
* `pymupdf` wheels are ~20 MB and exist for common platforms; fine on Vercel, but building from source
  (as needed in the audit sandbox) requires SWIG and libclang.
* `scipy` is large (~35 MB) but within Vercel's 500 MB Python bundle limit.
* `uvicorn[standard]` pulls `uvloop`/`httptools` (compiled); not needed on Vercel.
* Unpinned versions everywhere (`requirements.txt` has no versions) make results non-reproducible.
