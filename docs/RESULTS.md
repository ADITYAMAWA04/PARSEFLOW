# Measured results

Everything on this page was measured, not estimated. Where a number comes from a partial or
synthetic evaluation it says so. **No full olmOCR-Bench score has been measured yet, so the 80 %
target is not shown to be met.** The offline engine scores 78.8 % on the 20-PDF sample (v0.7.0). The `vlm`
engine (olmOCR-2 through an API) is built and checked against AllenAI's code, but it could not be run
against a real model from the build sandbox (see "The high-accuracy engine" below).

## Environment

| | |
|---|---|
| Machine | Linux 6.18 (cloud sandbox), Intel Xeon @ 2.80 GHz, **2 vCPU**, 7 GB RAM |
| Python | 3.13 |
| Key libraries | PyMuPDF 1.26.5 / MuPDF 1.26.10 (built from source), numpy 2.5.3, scipy 1.18.1, Pillow 12.3.0, pytesseract 0.3.13, FastAPI 0.143, httpx 0.28.1 |
| OCR | Tesseract 5.3.4 (LSTM, `eng`) |
| Models | none (no vision / LLM key configured; no model downloads; fully offline) |
| Scorer | AllenAI `olmocr` at commit `f7cfe4c2`, `python -m olmocr.bench.benchmark`, unmodified. KaTeX rendering via Playwright Chromium. `rapidfuzz` ran in its pure-Python implementation (same algorithm as the C extension). |

## olmOCR-Bench: what could and could not be run

* The **full benchmark (1,403 PDFs)** is hosted on Hugging Face, which is blocked from this
  sandbox. **It was not run.** No full-benchmark score is claimed.
* The official repository ships a **sample set** (`olmocr/bench/sample_data`: 21 PDFs, 23 pages,
  99 tests in `dataset.jsonl` plus auto-generated baseline tests). Both systems below were scored
  on it with the official scorer and the same command.
* The sample was also used as the **development set** while improving the pipeline, and it may
  overlap the full benchmark. Treat the sample numbers as optimistic. For calibration, the olmOCR
  outputs bundled with the sample score **87.9 %** on it versus 82.4 % on the full benchmark.

### Sample-set scores (official scorer, `dataset.jsonl`)

| | Original (c3a5917) | This version |
|---|---|---|
| **dataset.jsonl** | **34.3 %** (34/99) | **69.7 %** (69/99) |
| absent (headers/footers, page numbers) | 37.5 % | 87.5 % |
| present (text) | 30.0 % | 36.7 % |
| order (reading order) | 46.2 % | 84.6 % |
| table | 50.0 % | 75.0 % |
| math | 0.0 % | 91.7 % |
| baseline (auto) | 95.2 % | 100 % (20/20) |

Reproduce:

```bash
git clone --depth 1 https://github.com/allenai/olmocr.git ../olmocr && pip install -e "../olmocr[bench]" && playwright install chromium
cp -r ../olmocr/olmocr/bench/sample_data bench_data_sample
python bench/convert.py bench_data_sample --candidate parseflow --workers 1
python -m olmocr.bench.benchmark --dir bench_data_sample --candidate parseflow
# original system (for the baseline column)
git worktree add ../pf_baseline c3a5917
python bench/convert.py bench_data_sample --engine baseline --baseline-backend ../pf_baseline/backend --candidate pf_baseline --workers 1
python -m olmocr.bench.benchmark --dir bench_data_sample --candidate pf_baseline
```

Notes on the sample result:

* Remaining failures are dominated by handwritten pages (3 PDFs) and graphical/poster text, which
  need a recognition model (vision path is implemented but no key was available), plus two complex
  tables and one multi-column page.
* Scorer quirk observed and **not** exploited: `present` tests use `rapidfuzz.partial_ratio`, which
  returns 100 when the *output* is shorter than the expected sentence and happens to be a
  substring of it, so a near-empty output can pass. An intermediate version of this pipeline scored
  76.8 % partly for that reason (handwritten pages produced almost nothing); the current version
  outputs its real (poor) OCR of those pages; that version scored 72.7 % honestly (see below for the current figure).

### Category mapping to the full benchmark (expectation, not measurement)

The sample contains no "old scans math" and little "long tiny text". The text-layer math
reconstruction cannot help scanned math (Old Scans Math stays near 0 offline), and Old Scans /
Long Tiny Text are limited by Tesseract. A realistic full-benchmark result for the offline
pipeline is far below 80 %; only measuring it will tell.

### Why the sample score went from 72.7 % to 69.7 % when visual-text OCR was added

The previous version scored 72.7 %. Adding OCR for text inside images changed exactly one page,
`test-graphical-text.pdf` (a poster: text over a photograph). Before, ParseFlow output nothing for
it; its three `present` tests passed only because the scorer's `partial_ratio` accepts an empty
output, while its baseline test (non-empty output) failed. Now the page outputs what Tesseract
can read ("ER OF STO", "FOR LEADERS"): the baseline test passes and the three `present` tests
fail on the real, partial text. No other test changed. Text over photographs needs a scene-text
model, which could not be installed here.

## Latency (same sample, single worker process)

`python bench/convert.py <dir> --workers 1`; per-document wall time (open PDF → Markdown for all
pages, including OCR) divided by its page count; statistics over **all 21 documents / 23 pages**,
including failures and slow pages.

| s / page | median | p25 | p75 | p90 | p99 | max | mean |
|---|---|---|---|---|---|---|---|
| Original | 0.493 | 0.336 | 0.841 | 1.694 | 9.561 | 11.122 | 1.213 |
| This version | **0.396** | 0.315 | 1.104 | 2.089 | 9.232 | 10.096 | 1.313 |

Measured on a **2-vCPU** sandbox, not the 8-core laptop of the target. Digital pages take
~0.05-0.4 s; scanned pages take 1-10 s (Tesseract at up to 300 dpi), so the median depends on the
digital/scanned mix of the corpus. To measure on your laptop: run the command above with
`--workers 1` and read `<dir>/parseflow/_run.json`.

## Formula recognition, measured separately from OCR

`python bench/math_eval.py` builds 24 equations with pdflatex (Computer Modern), runs the
pipeline, and checks each with the official `MathTest` comparator. Synthetic, cleanly typeset
input — an upper bound for real arXiv pages.

| category | passed |
|---|---|
| summation | 3/3 |
| integral | 3/3 |
| scripts | 3/3 |
| greek | 3/3 |
| multiline | 2/2 |
| fraction | 2/3 |
| relation | 1/2 |
| sqrt | 1/2 |
| limit | 0/1 |
| matrix | 0/2 (not supported) |
| **total** | **18/24** |

## Large documents

`python bench/large_doc.py --pages 400 --scan-every 50` (400 pages: 392 digital incl. 40 with
tables, 8 scanned image pages; `OCR_WORKERS=2`):

| | Original (MAX_PAGES lifted to 400) | This version |
|---|---|---|
| Pages processed by default | 60 (truncated) | 400 |
| Wall time | 59.1 s | **22.7 s** (18.8 s on re-run with `MAX_PAGES=60` in the environment, which is now ignored) |
| Peak RSS | **3,284 MB** | **312 MB** |
| Status | complete | complete, 0 failed pages |
| Completeness | - | all 392 digital pages' text present; 8/8 scanned pages OCR'd; 400 checkpoints |

The original's peak memory exceeds the 2 GB limit of a Vercel Hobby function.

## The high-accuracy engine (`--engine vlm`)

| | status |
|---|---|
| Request identical to `olmocr.pipeline.build_page_query` (prompt, 1288-px render, image bytes, rotations 0/90/180/270) | checked against AllenAI's code |
| Front-matter parsing identical to AllenAI's `FrontMatterParser`, retries as in `process_page` | checked against AllenAI's code |
| End-to-end plumbing: a stand-in server that returns olmOCR's published outputs for the sample gives identical files and the identical official score (87.9 %, `dataset.jsonl`) | measured |
| Score with a real olmOCR-2 model on the full benchmark | **not measured**: Hugging Face and every model provider are unreachable from the build sandbox |

AllenAI reports 82.4 ± 1.1 % for olmOCR-2-7B-1025 on the full benchmark. Because ParseFlow sends the
identical request, a result close to that is expected, but only a real run counts.
[BENCHMARK.md](BENCHMARK.md) has the steps. Report the run's `_official_score.txt` and `_run.json`,
and state that the page text comes from olmOCR-2.

## Tests

`python -m pytest -q` → **136 passed, 1 xfailed** at v0.8.0 (the xfail is the documented matrix
limitation). Regression gate at v0.6.0: sample `dataset.jsonl` **69.7 %** (floor 67.7 %), unchanged.

## Frontend check

Driven with Playwright Chromium against the running server (real upload of `earnings.pdf` from the
sample set, then the Overview, Markdown, Tables, Visuals and Ask tabs) at **1366×900** and
**390×844**: no JavaScript errors (the only failed request is Google Fonts, blocked in the
sandbox), no horizontal page overflow at either width. Fixed in the process: the mobile header
widened the whole app to 637 px, and wide tables were clipped instead of scrolling in their card.

## Note on the scorer's summary line

The scorer's headline "Average Score" is the mean of the per-JSONL scores, which on the sample
includes `blanks.jsonl` (1 test) and the auto-generated baseline tests; it prints a higher figure (89.2 % at 72.7 %) for this
version. That number is not used anywhere in this repository: the figure reported above is the
`dataset.jsonl` score (**69.7 %**, 69/99), which is the one comparable to the benchmark's
category tests.

## AI features: what was and was not verified

`tests/test_ai.py` checks the AI paths with the provider **mocked** (no key, no network): provider
detection from the key, Gemini model rediscovery after a 404, no retry on a rejected key, AI reading
of an image when Tesseract is missing, verification only when local OCR agrees, the on-demand
`/api/visual` endpoint, the Connect-AI endpoint (local-only, key tested before saving, never echoed),
and chat answers with history; a local Ollama (auto-detection, vision-model choice, on-demand
handwriting reading, missing-model message) with its HTTP API mocked. The UI was driven in Chromium against a server whose provider was
replaced by canned responses. v0.6.0 adds tests for: a wait-and-retry round when every Gemini
model answers 503; Groq/OpenAI/OpenRouter/custom OpenAI-compatible servers (key inference, model
discovery from `/models`, a retired model replaced by another, busy retries, image parts, rejected
keys). **No real Gemini, Groq, OpenAI, Anthropic or Ollama call was made** (no key was available
and Google's API is not reachable from the build sandbox); answer quality depends on the model.

### Handwriting without a model (measured on the user's AVL lecture PDF)

Offline Tesseract output for the tablet-handwritten pages is poor (e.g. "Balance factoe Height of left
subtvee", "Stwuck Node"). These pages need a vision model (local Ollama or an API). Without one, Ask
falls back to quoting the best-matching sentences, weighted by extraction confidence so printed
text outranks noisy handwriting OCR (e.g. "what is the height of bst" -> "The height of BST may be
log(n) or n. [S1]").

## Latency after the visual-text and handwriting changes (v0.5.0)

Same sample, same command (`bench/convert.py --workers 1`, 2 vCPU sandbox), all 21 documents / 23
pages: **median 0.442 s/page** (p25 0.229, p75 0.823, p90 3.05, p99 10.3, mean 1.42). The earlier
0.396 s median was measured before figure OCR gained its second pass (box/axis lines removed) and the
handwriting checks; the cost is mostly on pages with raster figures. Sample score unchanged (69.7 %).

## Handwriting detection without a model (v0.5.0)

Images are marked "Looks handwritten" when they carry ruled notebook lines (>= 5 faint full-width
lines at a regular spacing) or when OCR confidence is typical of handwriting (< 0.75 over >= 3
lines). Scanned pages with ruling are merged into one handwritten block so one AI reading sees the
whole page. On the user's AVL lecture PDF this flags pages 4, 5, 6, 7, 8, 9, 10, 13 and 15 (the
handwritten ones) and none of the printed slides (1-3, 11, 12, 14, 16). The text shown offline is
labelled a rough OCR reading; a proper transcription needs "Read with AI".

## History (v0.6.0; sign-in removed in v0.6.3)

`tests/test_history.py` checks that each browser has its own list, that results still open by ID,
that only the browser which converted a document can delete it, and that deleting removes the stored
results. The optional Google/GitHub sign-in added in v0.6.0 was removed in v0.6.3 at the owner's request.

## The app no longer waits indefinitely for the OCR server (v0.6.1)

Checked against stand-in servers on this machine. A server that never answers: the 2-page document
finished by itself after the page budget (20 s in that test), with the warning "no answer within
20 s". The Skip button opened the document 1.4 s after it was clicked. A server answering HTTP 402
"Insufficient balance": the pass stopped after the first answer for every page, and
`backend/ai_check.py` printed the server's message. `tests/test_vlm.py` covers these cases.

## v0.7.0: fixes from the sample failure analysis

The failing sample tests at v0.6.3 were listed by type with AllenAI's own test classes. Only
fixes expected to generalise were made:

| Failure | Fix |
|---|---|
| Sentence split across a column break: column read in the wrong place, or blank line inside the sentence | A paragraph ending mid-sentence is followed by the column that starts in lower case; Markdown joins the two |
| Drop cap emitted as a heading `# O` + "ver the past" | Drop caps attach to paragraphs that wrap under them |
| "Table1 Compositionofthepretraining" (font without space glyphs) | Word gaps of 0.1 em count when glyph boxes inside words touch |
| "J´an Min´aˇc" (TeX accents drawn as separate glyphs) | Spacing accents are recombined: "Ján Mináč" |
| `\lambda_{g}'` for λ_{g′} | A prime after a subscript stays in the subscript; raised primes are `\prime` |
| "Year Ended" above three date columns emitted as a heading | Becomes a spanning header row (`colspan`) of the table |
| Table cells shifted or swallowed by stray grid edges | Columns come from a complete row; spans only cover empty grid positions |
| Numbered sections (1, 2 \| 3, 4) laid out as a grid, read row by row | Numbering that runs down the columns decides column order |

Sample `dataset.jsonl`: **69.7 % → 78.8 %** (78/99). By type: table 18/20, order 12/13, absent
24/24, math 12/12, present 15/30. Most remaining failures are handwriting (three handwritten
pages) and text drawn as graphics, which need a recognition model. Latency on the sample was
unchanged (median 0.61 s/page with 2 workers on 2 vCPUs, the same as before the changes).
**The sample was used to find these failures, so its score overstates the full benchmark.**

## v0.8.0: loading speed

The time is measured on this 2-vCPU machine (Tesseract 5.3.4). "Opens" is when the web app shows the
document. "All read" is when the text inside images and handwriting has also been read in the
background.

| Document | Before (v0.7.0) | v0.8.0 opens | v0.8.0 all read |
|---|---|---|---|
| AVL lecture, 16 pages, 12 screenshots | 34.9 s | **1.6 s** (2.0 s in the browser, including upload) | 9.0 s |
| lincoln_letter (scanned handwriting, 1 page) | 24.5 s | 4.9 s | 7.0 s |
| buildingnotes (scanned, 1 page) | 13.8 s | 2.4 s | 4.9 s |
| earnings (digital, 1 page) | 0.8 s | 0.7 s | 0.7 s |

Causes found by profiling: 95 % of the time was Tesseract on figures and handwriting. Tesseract's own
OpenMP threads made each call about 3 times slower here, and PNG-encoding every image for Tesseract
cost about 1 s per full page. A second full-page OCR pass also ran on handwriting, where it never
helps. The fixes are the background phase, `OMP_THREAD_LIMIT=1`, uncompressed BMP input, and running
the second pass only for degraded print. Page images are now JPEG, and thumbnails are 200 px and lazy.

Benchmark path (CLI, no background phase): sample `dataset.jsonl` unchanged at **78.8 %**; sample
seconds per page: mean 1.70 → 1.16, max 13.1 → 6.4, median unchanged (0.62 with 2 workers on 2 vCPUs).

Formats: DOCX, XLSX, PPTX, CSV, TXT (UTF-8 and UTF-16), MD, HTML, JSON, RTF, multi-page TIFF, WebP
and BMP are converted and read in `tests/test_formats_speed.py` and in a browser test. DOCX, PPTX and
RTF were also converted through LibreOffice 24.2 here. The Dockerfile could not be built (no Docker
daemon in the build environment).
