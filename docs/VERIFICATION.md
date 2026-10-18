# Confidence, verification and provenance

Every block in the Digital Twin JSON carries:

| field | meaning |
|---|---|
| `block_id`, `type`, `page`, `bbox`, `reading_order` | identity and location (bbox in viewer pixels; `pages[n].scale` converts to PDF points) |
| `content` / `latex` / `rows` / `cells` / `detected_text` | extracted content in its native structure |
| `extractor` | which method produced it (`digital_text_parser`, `table_engine`, `borderless_table`, `text_layer_math`, `ocr`, `figure_ocr`, `figure_text_layer`, `vision_*`, `fallback`) |
| `confidence`, `confidence_type` | a number **and** where it comes from (see below) |
| `confidence_level` | `high` (≥ 0.85), `uncertain` (0.60-0.85), `low` (< 0.60) |
| `verification` | `verified`, `review` or `failed` (see below) |
| `evidence`, `evidence_checks` | the check that decided `verification`, with its measured result |
| `role`, `role_evidence`, `in_markdown`, `markdown_exclusion` | header/footer/page-number/footnote classification and whether the block reached the Markdown body |
| `warnings`, `relationships` | problems found; links such as `caption_of:tbl_1_01` |

## Where confidence comes from

| extractor | confidence |
|---|---|
| digital text | 0.99 nominal; the meaningful signal is the verification check below and the page's `text_quality` (share of characters that are not unmapped glyphs) |
| ruled table | 0.6 + 0.35 × share of non-empty cells |
| borderless table | 0.70 (structure inferred from alignment only) |
| equation from text layer | 0.5 + 0.4 × glyph coverage (share of glyphs represented in the LaTeX) |
| OCR | mean Tesseract word confidence of the paragraph |
| figure text | mean OCR confidence of the kept lines; 0.3 when nothing was found |
| vision model | the model's self-reported value, capped at 0.95, labelled `model (self-reported)` |
| fallback | 0.3 / 0.0 – nothing was extracted |

## What "verified" means

`verified` is only set when an **independent** check agrees:

| extractor | check | verified when |
|---|---|---|
| digital text | re-read the text layer inside the block's bbox with `page.get_textbox` and compare tokens | ≥ 90 % token recall and text quality ≥ 0.98 |
| tables | each cell string searched in the page's text layer | ≥ 90 % of cells found (cell *structure* is still inferred) |
| OCR | tokens compared with an embedded OCR text layer, if the PDF has one | ≥ 85 % agreement and confidence ≥ 0.85 |
| vision text | independent Tesseract read of the same crop | ≥ 50 % token overlap, OCR conf ≥ 0.6, model conf ≥ 0.85 |
| vision tables | model cells found in the text layer | ≥ 90 % |

Everything else is `review` at best. In particular:

* a single OCR engine's confidence is never treated as verification;
* LaTeX reconstructed from the text layer is always `review`: glyph identities are read from the
  file, but the 2-D structure (fractions, scripts) is inferred, and valid LaTeX does not prove a
  correct reading;
* two reads of the same text layer are correlated; the evidence string says so ("text layer"
  re-read), it is a provenance/bbox check rather than proof that the producer's text is right.

`failed` means there is no usable content (empty block, unreadable region, page error) or a
bounding box outside the page. Failed regions are kept with their bbox so the page image can be
inspected.

## Tracing Markdown back to the source

* `GET /api/document/{id}/markdown?annotate=1` or `python -m backend.cli ... --annotate` puts a
  comment before every block: `<!-- pf:txt_3_02 p=3 bbox=[...] digital_text_parser conf=0.99 verified -->`.
* The clean Markdown (default, used for the benchmark) has no comments; use the `--json` sidecar:
  each block records `in_markdown` and, when excluded, `markdown_exclusion`.
