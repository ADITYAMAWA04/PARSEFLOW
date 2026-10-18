# Running the official olmOCR-Bench evaluation

This page explains how to measure ParseFlow on the full olmOCR-Bench (1,403 PDFs) with AllenAI's
unmodified scorer, and what the result does and does not mean.

## Two engines, and why there are two

| engine | what reads the pages | needs | measured |
|---|---|---|---|
| `parseflow` | ParseFlow's own pipeline: PDF text layer, tables, LaTeX from glyphs, Tesseract OCR | nothing (offline, CPU) | 78.8 % on the 20-PDF sample at v0.7.0 (not the full set) |
| `vlm` | a document OCR model, by default **AllenAI olmOCR-2-7B**, called page by page over an OpenAI-compatible API; ParseFlow's pipeline is the fallback for any page the model cannot do | an API key for a hosted provider, or a GPU running vLLM | to be measured by you, see below |

Every system that scores above 80 % on the public leaderboard is a vision model trained specifically
for document conversion (for example Chandra, LightOnOCR-2, olmOCR-2 at 82.4 %, PaddleOCR-VL at 80.0 %).
General chat models score far lower (Gemini Flash 2 63.8 %, GPT-4o 69.9 %), and no Tesseract-based CPU
pipeline is close. The `vlm` engine is how ParseFlow reaches that level.

**Be explicit about this in your report:** with the `vlm` engine the page text comes from olmOCR-2;
ParseFlow contributes the orchestration, rendering, retries, fallback, provenance, verification,
Markdown/JSON output, the app and Ask. Run both engines and report both numbers.

### How we know the engine reproduces olmOCR exactly

* `vlm_ocr.build_request` produces a request **byte-identical** to `olmocr.pipeline.build_page_query`
  (same prompt, same 1288-px rendering, same image bytes, at rotations 0/90/180/270), checked
  against AllenAI's code.
* Front-matter parsing agrees with AllenAI's `FrontMatterParser` on all test cases, including
  malformed ones; retries follow `process_page` (temperature ladder, rotation feedback, context check).
* Plumbing test: a stand-in server that returns olmOCR's own published outputs for the 20-PDF
  sample produced files identical to those outputs and the identical official score (87.9 %
  `dataset.jsonl`). That proves the pipeline, not a model score.

Rendering: olmOCR renders with Poppler's `pdftoppm`. ParseFlow uses it when it is on PATH and
otherwise PyMuPDF at the same resolution (same size within 1 px, small anti-aliasing differences).
For a result you report, install Poppler so the images are identical. `_run.json` records which
renderer was used.

## Step by step (Windows; macOS/Linux are the same with `/` paths)

Open a terminal in the ParseFlow folder and activate its environment:

```bat
.venv\Scripts\activate
```

### 1. Get access to olmOCR-2 (no GPU needed)

AllenAI lists hosted providers that serve `olmOCR-2-7B-1025`. Sign up with one, add a little credit,
and create an API key:

| provider | `VLM_SERVER` | `VLM_MODEL` |
|---|---|---|
| DeepInfra | `https://api.deepinfra.com/v1/openai` | `allenai/olmOCR-2-7B-1025` |
| Parasail | `https://api.parasail.io/v1` | `allenai/olmOCR-2-7B-1025` |
| Cirrascale | `https://ai2endpoints.cirrascale.ai/api` | `olmOCR-2-7B-1025` |

Prices listed in AllenAI's README are about $0.07-0.10 per million input tokens and $0.15-0.20 per
million output tokens. One full benchmark run is roughly 3 M input + 1.5 M output tokens, i.e.
**well under 1 US dollar** (check the provider's current price before you start).

With your own NVIDIA GPU (12 GB or more) you can instead run `vllm serve allenai/olmOCR-2-7B-1025-FP8
--max-model-len 16384` and use `VLM_SERVER=http://localhost:8000/v1` with no key.

Add to `.env` (never commit this file):

```
VLM_SERVER=https://api.deepinfra.com/v1/openai
VLM_API_KEY=your-key-here
VLM_MODEL=allenai/olmOCR-2-7B-1025
```

### 2. Install the official scorer (once)

```bat
git clone https://github.com/allenai/olmocr.git ..\olmocr
pip install -e "..\olmocr[bench]"
playwright install chromium
```

Optional, for identical page images: install a Windows build of Poppler (for example the one linked
from the pdf2image project's README) and add its `bin` folder to PATH; `pdftoppm -v` should work.

### 3. Download the benchmark data (once, about 1,403 PDFs)

```bat
pip install -U huggingface_hub
huggingface-cli download --repo-type dataset allenai/olmOCR-bench --local-dir olmOCR-bench
```

(Newer versions of the tool call the command `hf download` instead of `huggingface-cli download`.)
The folder you need is `olmOCR-bench\bench_data` (it contains `pdfs\` and the `*.jsonl` test files).

### 4. Try it on a few pages first

```bat
python -m backend.cli ..\olmocr\olmocr\bench\sample_data\pdfs --output test_out --engine vlm
```

If that writes Markdown files without errors, the key and model work.

### 5. Run the full benchmark

```bat
python bench\run_benchmark.py olmOCR-bench\bench_data --engine vlm --workers 8
python bench\run_benchmark.py olmOCR-bench\bench_data --engine parseflow --workers 4
```

Each run converts every PDF, runs `python -m olmocr.bench.benchmark` unmodified, prints the
per-category table and the overall score, and saves:

* `olmOCR-bench\bench_data\pf_vlm\_official_score.txt`: the scorer's complete output
* `olmOCR-bench\bench_data\pf_vlm\_run.json`: git commit, Python, OS, CPU, model, server,
  renderer, tokens, pages read by the model vs. pages that fell back, failures, latency
  percentiles (seconds per page = document wall time / pages, over all documents)

The "Official overall score" line appears only when the folder holds the full set (1,403 PDFs,
all test files); anything smaller is labelled as a partial run.

### When the app seems stuck at "high-accuracy OCR"

When `VLM_SERVER` is set, the app also sends every page to the OCR model. Since v0.6.1:

* the Processing screen shows progress and elapsed time, and a **Skip and use the fast result**
  button that finishes at once with ParseFlow's own reading;
* each page gives up after `VLM_APP_PAGE_BUDGET_S` (default 120 s), and the whole pass stops at once on
  a problem that cannot fix itself: a rejected key, an unknown model, or an account without credit
  (HTTP 402);
* the Overview tab only says that high-accuracy OCR was not available. The technical reason is kept
  in the document JSON (`high_accuracy_ocr.reason`), and `backend/ai_check.py` prints it;
* when the server fails for a reason that does not fix itself (bad address, key, model or credit),
  later uploads skip it for 10 minutes (`VLM_RETRY_AFTER_S`) instead of waiting again;
* a `VLM_SERVER` without `https://` is completed automatically, and a value that is not an address
  at all is ignored.

To see what the server says, run `python backend\ai_check.py`. It sends one small test page and prints
the server's answer, for example `HTTP 402: Insufficient balance` or `HTTP 404: model not found`.
To keep `VLM_*` for the benchmark but not use it in the app, add `HIGH_ACCURACY_OCR=0` to `.env`.

### Latency

With the `vlm` engine the time per page is dominated by the network and the provider's GPU (often
several seconds per page, with `--workers` requests in flight). It is not comparable with the
0.4 s/page CPU target, which applies to the offline `parseflow` engine (median 0.44 s/page on a
2-vCPU machine on the sample). Report both, with the hardware.

## Rules we follow

* The scorer is never modified; scores are only ever copied from its output.
* No PDF is excluded; failures are counted in `_run.json`.
* The engine was not tuned on the benchmark: the model, prompt and settings are AllenAI's.
