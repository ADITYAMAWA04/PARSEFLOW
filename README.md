# ParseFlow

**ParseFlow** is an evidence-preserving document-intelligence system developed by **Team DEADLOCK**
for the **DQCL ParseAnything** problem statement (DataQuest 3.0) further taken by Aditya Mawa.

It turns PDFs and images into Markdown plus a **Digital Twin** (JSON in which every extracted block
keeps its page, bounding box, extractor, confidence and verification evidence), and answers
questions about the document with citations to those blocks.

> **Status of the olmOCR-Bench target.** ParseFlow has two page engines:
>
> * `parseflow` (offline, CPU, no keys): **78.8 %** (v0.7.0; 69.7 % at v0.6) on the official 20-PDF *sample* set with AllenAI's
>   unmodified scorer (from 34.3 %), median 0.44 s/page. Not measured on the full benchmark.
> * `vlm`: pages are read by a document OCR model, by default **AllenAI olmOCR-2-7B** (82.4 % on the
>   public leaderboard), through any OpenAI-compatible API, with ParseFlow's pipeline as the per-page
>   fallback. The request is byte-identical to AllenAI's own pipeline. **Its full-benchmark score has
>   not been measured yet**: the build environment could not reach the benchmark data or any model
>   provider. [docs/BENCHMARK.md](docs/BENCHMARK.md) gives the one-command run (under 1 US dollar on a
>   hosted provider). With this engine the page text comes from olmOCR-2, so say so when you report it.
>
> See [docs/RESULTS.md](docs/RESULTS.md) for every number and how it was measured.

---

For the internship submission steps (one command, official score, latency), see [SUBMISSION.md](SUBMISSION.md).

## Quick start

```bash
git clone https://github.com/ADITYAMAWA04/PARSERFLOW.git && cd PARSERFLOW
bash run.sh            # Linux / macOS   (Windows: double-click run.bat)
# opens the browser itself once the server is up (normally http://localhost:8000)
```

`run.sh` / `run.bat` create `.venv`, install `requirements.txt`, install the free Tesseract OCR engine once if it is missing (Windows: winget; macOS: Homebrew), copy `.env.example` to `.env` and start
the server through `backend/start.py`, which refuses to hide behind an older ParseFlow: if another
version is still running on port 8000 it says so and starts on the next free port, then opens the
browser at the port it really uses. The status badge in the app shows the running version. Tesseract is needed for scanned pages:
`sudo apt install tesseract-ocr` / `brew install tesseract` /
[UB-Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki) on Windows.

Manual install:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # runtime
pip install -r requirements-dev.txt        # + tests
cd backend && python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

No API key is needed for extraction. **To read handwriting and every image with AI and get
ChatGPT-style answers in Ask, click "Connect AI" in the app** and choose one of:

* **Local AI (Ollama)**: free, offline, no key. Install https://ollama.com, pull a model that can read
  images (one marked *vision* in the Ollama library, e.g. `ollama pull gemma3:4b`), then pick it in
  Connect AI. ParseFlow also uses a running Ollama automatically when no key is set. On a laptop CPU
  expect roughly a minute per image, so local models read images on demand ("Read with AI" /
  "Read all with AI"); set `LOCAL_AI_DURING_PROCESSING=1` to read them during upload instead.
* **Google Gemini**: free-tier key from https://aistudio.google.com/apikey. When a model is
  overloaded (HTTP 503), ParseFlow moves through every Flash model the key can use, waits a few
  seconds and retries once. "Test all models" in Connect AI (or `python backend/ai_check.py`) shows
  what Google answers for each model.
* **Groq**: free-tier key from https://console.groq.com/keys. A good second route when Google is busy.
* **Anthropic Claude** or **OpenAI**: paid keys. Any other OpenAI-compatible server works through
  `AI_BASE_URL` in `.env`. Model names are discovered from the provider, so retired models are
  replaced automatically. The key is tested, saved to `.env` on the
machine running ParseFlow, and never sent back to the browser (the settings endpoint only accepts
requests from that machine). Or set `AI_API_KEY` in `.env` yourself; see `.env.example`.

Tesseract is found on PATH or in its default install folder (on Windows
`C:\Program Files\Tesseract-OCR`); set `TESSERACT_CMD` if it is elsewhere.

### Documents it opens

PDF, Word (DOCX, and DOC/ODT with LibreOffice), Excel (XLSX, and XLS/ODS with LibreOffice), CSV/TSV,
PowerPoint (PPTX, and PPT/ODP with LibreOffice), RTF, plain text, Markdown, HTML, JSON, XML, EPUB, XPS,
FB2, MOBI, SVG, and images: PNG, JPG, TIFF (multi-page), BMP, GIF and WebP. Everything that is not a
PDF or a PNG/JPG is converted to PDF first (`backend/processing/formats.py`), so the page viewer,
tables, OCR and provenance work the same for every format. LibreOffice is optional: when it is
installed, Word and PowerPoint keep their real layout and pictures.

### Speed: the document opens first, images are read after

In the web app a document opens as soon as its text, tables and equations are ready. The text
inside pictures, screenshots and handwriting is read afterwards in the background, several regions
at once. A chip in the top bar shows the progress, and the open document refreshes itself when
that is done. On the 16-page AVL lecture PDF, the document now opens in about 2 s instead of
about 37 s, and everything is read in about 9 s. Tesseract runs single-threaded per call
(`OMP_THREAD_LIMIT=1`), about 3 times faster here. Page images are JPEG, and the thumbnails are
small and load lazily.

### Accounts and saved documents

**Log in** (top right) opens a page to create an account with a username and password, or to log
in. No email is needed.

* Logged in: documents are saved in the server's database until you delete them, open from any
  computer, and are private to you. Documents you converted in that browser before logging in move
  into your account.
* Not logged in: documents are listed for your browser only, and deleted after 24 hours
  (`GUEST_RETENTION_HOURS`).

Passwords are stored as salted scrypt hashes, and repeated wrong passwords are slowed down. To run
ParseFlow on a cloud server with a persistent disk (Render blueprint and Dockerfile included), see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Convert a folder of PDFs to Markdown (no server, no keys)

```bash
python -m backend.cli ./input_pdfs --output ./markdown_output
python -m backend.cli ./input_pdfs --output ./markdown_output --workers 4 --json   # + Digital Twin JSON, 4 files at a time
```

One `.md` per input file (sub-folders included and mirrored; `--top-only` skips them). Other flags: `--annotate`
(provenance comments in the Markdown), `--include-headers`, `--work-dir DIR` (page checkpoints so
an interrupted run resumes). Failures are reported per file; the exit code is 1 if any file failed
or was only partially converted, 2 for bad arguments.

## Tests and evaluation

```bash
python -m pytest -q                                   # 89 tests (+1 expected failure: matrices)
python bench/math_eval.py                             # formula recognition with the official MathTest comparator
python bench/large_doc.py --pages 400                 # memory / runtime / completeness on a 400-page PDF
```

Official olmOCR-Bench (full data from Hugging Face):

```bash
git clone https://github.com/allenai/olmocr.git ../olmocr
pip install -e "../olmocr[bench]" && playwright install chromium
huggingface-cli download --repo-type dataset allenai/olmOCR-bench --local-dir ./olmOCR-bench
python bench/convert.py ./olmOCR-bench/bench_data --candidate parseflow --workers 1
python -m olmocr.bench.benchmark --dir ./olmOCR-bench/bench_data --candidate parseflow
```

`bench/convert.py` writes `<candidate>/<name>_pg<N>_repeat1.md` (the layout the scorer expects),
plus `_timings.jsonl` (per-document seconds, pages, failures) and `_run.json` (environment,
git commit, median / p25 / p75 / p90 / p99 seconds per page, peak RSS). Use `--workers 1` for
latency figures. The original system can be scored the same way with `--engine baseline` (see
docs/RESULTS.md). `bench/regression_check.py` is the per-commit quality gate on the bundled sample
set (CI: `.github/workflows/ci.yml`).

---

## Architecture

```text
INGEST ─ open PDF / image, reject empty / corrupt / encrypted (PF-2xx errors)
  │
  ▼  page by page (bounded memory; checkpoint per page; resumable; failures isolated per page)
CLASSIFY PAGE ─ text layer? invisible OCR layer? scanned? (pdf_parser.analyze)
  │
  ├─ digital page ──► lines + per-glyph geometry (PyMuPDF rawdict, missing spaces synthesised,
  │                    unmapped TeX glyphs recovered from the embedded font's glyph names)
  │                   ruled tables (find_tables, header-row recovery, currency-column folding, spans)
  │                   display / inline math from glyph geometry → LaTeX         (mathtext.py)
  │                   borderless tables from word alignment                      (tables.py)
  │                   figures: text-layer labels or OCR of raster images, optional vision
  │                   header / footer / page-number / footnote roles             (headers.py)
  │                   reading order: XY-cut with column / grid rules             (layout.py)
  │                   paragraph merging, evidence-based de-hyphenation           (textnorm.py)
  │
  └─ scanned page ──► resolution-aware rasterisation (≤ native dpi, capped pixels)
                      selective preprocessing: contrast, illumination, deskew   (scan.py)
                      Tesseract LSTM; OSD rotation and psm6 fallbacks only when the first
                      pass looks wrong; low-confidence text kept as evidence, not as body text
                      unexplained ink → optional vision model, else flagged region
  ▼
DOCUMENT PASSES ─ repeated running heads across pages, block ids, caption links
  ▼
VERIFY ─ independent checks per extractor → verified / review / failed     (verifier.py)
  ▼
DIGITAL TWIN JSON ──► Markdown (backend, authoritative)                     (markdown.py)
                 └──► Ask: section-aware chunks, BM25, optional LLM with
                      citation validation                                    (ask.py)
```

| file | role |
|---|---|
| `backend/app.py` | FastAPI API, durable jobs (`status.json`), serverless mode |
| `backend/cli.py` | folder → Markdown converter |
| `backend/processing/pipeline.py` | page-streaming driver, checkpoints, assembly |
| `backend/processing/pdf_parser.py` | text-layer lines/blocks, ruled tables, figures |
| `backend/processing/mathtext.py`, `glyphs.py` | LaTeX from text-layer glyphs; CMEX glyph names |
| `backend/processing/tables.py` | borderless tables, spans, Markdown/HTML rendering |
| `backend/processing/headers.py`, `layout.py`, `textnorm.py` | roles, reading order, joining |
| `backend/processing/ocr.py`, `scan.py` | Tesseract wrapper, preprocessing |
| `backend/processing/llm.py` | one AI adapter (Ollama local / Gemini / Anthropic): provider detection, retries, model discovery |
| `backend/processing/vision.py`, `ask.py` | AI reading of images; grounded chat answers with citations |
| `frontend/ai.js` | chat UI, Read with AI, Connect AI |
| `backend/processing/verifier.py`, `schema.py` | evidence and confidence semantics |
| `backend/processing/markdown.py` | Markdown renderer |
| `frontend/index.html` | single-page UI (viewer, inspector, tables, visuals, Markdown, Ask) |
| `bench/` | benchmark conversion, baseline reproduction, math / large-doc / regression checks |
| `tests/` | pytest suite with synthetic fixtures |
| `docs/` | audit, results, verification semantics, deployment |

### API

| method | path | |
|---|---|---|
| GET | `/api/health` | engines, mode (`worker` / `serverless`), limits; never secrets |
| POST | `/api/process` | multipart `file`; returns `document_id` (serverless: also the result) |
| GET | `/api/status/{id}` | `status` (running / complete / partial / failed / cancelled), `total_pages`, `completed_pages`, `current_page`, `failed_pages`, `progress_percent`, stages |
| POST | `/api/cancel/{id}` | cooperative cancellation between pages |
| GET | `/api/document/{id}` | Digital Twin JSON |
| GET | `/api/document/{id}/markdown` | Markdown (`?annotate=1` adds provenance comments, `?include_headers=1`) |
| GET | `/api/page/{id}/{n}.png` | page image, rendered on demand |
| POST | `/api/ask` | `{document_id, question, history?}` → answer (Markdown), `mode` (`llm` / `search`), cited sources |
| POST | `/api/visual/{id}/{block_id}` | read one image region with the AI model; updates JSON + Markdown |
| POST / DELETE | `/api/settings/ai` | save (after a test call) / remove the AI key or local model; local requests only |
| GET | `/api/settings/ai/local` | models pulled in a local Ollama and which can read images |
| POST | `/api/settings/ai/check` | tiny request to every model the key can use; per-model status |
| GET / DELETE | `/api/history`, `/api/history/{id}` | your (account's or browser's) documents; delete one with its results |
| GET / POST | `/api/me`, `/api/auth/register`, `/api/auth/login`, `/api/auth/logout`, `/api/auth/password` | username + password accounts |
| GET | `/api/page/{id}/{n}.jpg?w=` | page image as JPEG (`w` = preview width) |
| POST | `/api/skip-ocr/{id}` | stop waiting for the high-accuracy OCR server and finish with ParseFlow's own reading |

Errors are JSON `{"error": {code, title, message}}`; unexpected exceptions return `PF-500` without
stack traces or paths. Uploaded files are stored as `source.<ext>` (client filenames never touch the
filesystem), document ids are validated, and the server never fetches URLs from user input.

## What is and is not done

Implemented and tested: no page limit (`MAX_PAGES` from old `.env` files is ignored; `PARSEFLOW_PAGE_LIMIT` is the explicit opt-in cap), text inside images, charts and diagrams read with local OCR (box/axis lines removed before OCR, labels ordered, heuristic visual type and description, shown in the Visuals tab) (400 pages in 23 s / 312 MB peak on 2 vCPUs vs 3.3 GB for
the original), header/footer/page-number removal with evidence, XY-cut reading order,
ruled + borderless tables with HTML spans, LaTeX from the text layer (fractions, radicals,
scripts, big operators with limits, Greek, aligned derivations), OCR preprocessing with
selective fallbacks, text inside figures, backend Markdown, CLI, grounded Ask with optional LLM,
durable/resumable jobs, serverless mode.

Known limitations:

* **Handwriting, scanned equations and stylised/graphic text need a recognition model.** Offline,
  such regions are preserved and flagged; with `VISION_API_KEY` they go to the vision model
  (self-reported confidence, never auto-verified). No local handwriting / formula-image model is
  bundled (they need PyTorch + model downloads; not possible in the environment this was built in).
* Matrices / arrays are not reconstructed as LaTeX; equations stacked very closely can be split
  or merged imperfectly; glyphs without Unicode or a known glyph name are dropped and counted.
* Old scans and long tiny text are limited by Tesseract quality.
* Tables: nested headers and very irregular borderless tables are approximate.
* Vercel: 4.5 MB uploads, ≤ 25 pages per request by default, no OCR, no persistent storage (see
  [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)). Deployment configuration was checked against Vercel's
  documentation but **not verified with a live deployment**.
* Latency was measured on a 2-vCPU machine, not the target 8-core laptop.

More: [docs/AUDIT.md](docs/AUDIT.md) (original problems), [docs/RESULTS.md](docs/RESULTS.md),
[docs/VERIFICATION.md](docs/VERIFICATION.md), [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Team

**Team DEADLOCK** · Project: ParseFlow · Problem statement: DQCL ParseAnything · Event: DataQuest 3.0

Repository: https://github.com/ADITYAMAWA04/PARSERFLOW
