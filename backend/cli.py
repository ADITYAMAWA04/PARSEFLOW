"""Offline batch conversion: one Markdown file per PDF. No web server and no API key required.

    python -m backend.cli ./input_pdfs --output ./markdown_output [--workers 4] [--json] [--annotate]

Sub-folders are included and mirrored in the output (use --top-only to skip them).

Exit codes: 0 = every file converted, 1 = at least one file failed (others still converted),
2 = bad arguments / no input files.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from processing import config, formats, markdown, pipeline  # noqa: E402

SUPPORTED = tuple(sorted(formats.SUPPORTED))   # PDF, images, Word/Excel/PowerPoint, text, HTML, EPUB, ...


def safe_stem(rel_path):
    """Filesystem-safe output name that keeps the meaningful part of the original name.
    report.pdf -> report; other formats keep their extension (report.docx -> report.docx) so files
    that differ only by extension do not overwrite each other's Markdown."""
    stem, ext = os.path.splitext(rel_path)
    if ext.lower() != ".pdf":
        stem = rel_path
    parts = [re.sub(r"[^\w.\-() ]+", "_", p).strip(" .") or "_" for p in Path(stem).parts]
    return os.path.join(*parts)


def find_inputs(root, recursive):
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED else []
    it = root.rglob("*") if recursive else root.iterdir()
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in SUPPORTED)


def main(argv=None):
    config.load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m backend.cli", description="Convert a folder of documents (PDF, Word, Excel, PowerPoint, images, text, HTML, ...) to Markdown, one .md per file.")
    ap.add_argument("input", help="input folder (or a single PDF)")
    ap.add_argument("--output", "-o", required=True, help="output folder")
    ap.add_argument("--recursive", "-r", action="store_true", help=argparse.SUPPRESS)   # default since v0.7; kept for old scripts
    ap.add_argument("--top-only", action="store_true", help="only the PDFs directly in the input folder, not in sub-folders")
    ap.add_argument("--workers", "-j", type=int, default=1, help="convert this many files in parallel (default 1)")
    ap.add_argument("--json", action="store_true", help="also write the Digital Twin JSON (<name>.json) with provenance")
    ap.add_argument("--annotate", action="store_true", help="put provenance comments (block id, page, bbox, confidence) in the Markdown")
    ap.add_argument("--include-headers", action="store_true", help="keep running headers/footers/page numbers in the Markdown body")
    ap.add_argument("--work-dir", help="keep per-page checkpoints here so an interrupted run can resume")
    ap.add_argument("--engine", choices=["parseflow", "vlm"], default="parseflow",
                    help="parseflow = offline pipeline (default); vlm = every page through the OCR model at VLM_SERVER "
                         "(e.g. olmOCR-2), falling back to parseflow for pages the model cannot do")
    ap.add_argument("--quiet", "-q", action="store_true")
    args = ap.parse_args(argv)

    src = Path(args.input)
    if not src.exists():
        print(f"error: input not found: {src}", file=sys.stderr)
        return 2
    files = find_inputs(src, not args.top_only)
    if not files:
        print(f"error: no supported documents in {src} (PDF, Word, Excel, PowerPoint, images, text, HTML, ...)", file=sys.stderr)
        return 2
    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)
    base = src if src.is_dir() else src.parent
    failures, t_all = [], time.perf_counter()
    if args.workers > 1 and args.engine == "parseflow" and len(files) > 1:
        return _parallel(files, base, out_root, args, t_all)
    for i, f in enumerate(files, 1):
        rel = os.path.relpath(f, base)
        target = out_root / (safe_stem(rel) + ".md")
        target.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        try:
            work = os.path.join(args.work_dir, safe_stem(rel)) if args.work_dir else None
            prog = pipeline.Progress(os.path.join(work, "status.json") if work else None)
            if args.engine == "vlm" and f.suffix.lower() == ".pdf":
                from processing import vlm_ocr
                if not vlm_ocr.settings()["configured"]:
                    print("error: --engine vlm needs VLM_SERVER (and usually VLM_API_KEY) in .env; see docs/BENCHMARK.md", file=sys.stderr)
                    return 2

                def local(path, _f=f, _rel=rel):
                    d = pipeline.process(str(_f), _f.name, safe_stem(_rel), None, pipeline.Progress(None), write_outputs=False)
                    return {p["n"]: markdown.render(dict(d, blocks=[b for b in d["blocks"] if b["page"] == p["n"]], pages=[p]))
                            for p in d["pages"]}
                pages_md, st = vlm_ocr.convert_pdf(str(f), local_pages=local)
                md = "\n\n".join(pages_md[k] for k in sorted(pages_md))
                doc = dict(pages=[{"n": k} for k in sorted(pages_md)], vlm=st, processing_status="complete", failed_pages=[])
                if st["fallback_pages"]:
                    print(f"  note: pages {st['fallback_pages']} used the offline pipeline (OCR model failed)", file=sys.stderr)
            else:
                doc = pipeline.process(str(f), f.name, safe_stem(rel), work, prog, write_outputs=False)
                md = markdown.render(doc, annotate=args.annotate, include_headers=args.include_headers)
            target.write_text(md, encoding="utf-8")
            if args.json:
                doc.pop("_markdown", None)
                (target.with_suffix(".json")).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            note = f" ({doc['processing_status']}, failed pages {doc['failed_pages']})" if doc.get("failed_pages") else ""
            if doc.get("failed_pages"):
                failures.append((rel, f"failed pages {doc['failed_pages']}"))
            if not args.quiet:
                print(f"[{i}/{len(files)}] OK   {rel} -> {target} ({len(doc['pages'])} pages, {time.perf_counter() - t0:.1f}s){note}")
        except pipeline.PFError as e:
            failures.append((rel, f"{e.code} {e.title}: {e.message}"))
            print(f"[{i}/{len(files)}] FAIL {rel}: {e.code} {e.title} - {e.message}", file=sys.stderr)
        except Exception as e:  # keep going: one bad file must not stop the batch
            failures.append((rel, f"{type(e).__name__}: {e}"))
            print(f"[{i}/{len(files)}] FAIL {rel}: {type(e).__name__}: {e}", file=sys.stderr)
    if not args.quiet:
        print(f"Done: {len(files) - len(failures)}/{len(files)} converted in {time.perf_counter() - t_all:.1f}s -> {out_root}")
    if failures:
        print(f"{len(failures)} file(s) failed or were only partially converted:", file=sys.stderr)
        for rel, why in failures:
            print(f"  {rel}: {why}", file=sys.stderr)
        return 1
    return 0


def _one(f, base, out_root, opts):
    """Convert one file (worker process). Returns (rel, error or None, pages, seconds)."""
    rel = os.path.relpath(f, base)
    target = Path(out_root) / (safe_stem(rel) + ".md")
    target.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    try:
        doc = pipeline.process(str(f), Path(f).name, safe_stem(rel), None, pipeline.Progress(None), write_outputs=False)
        target.write_text(markdown.render(doc, annotate=opts["annotate"], include_headers=opts["include_headers"]), encoding="utf-8")
        if opts["json"]:
            doc.pop("_markdown", None)
            target.with_suffix(".json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        err = f"failed pages {doc['failed_pages']}" if doc.get("failed_pages") else None
        return rel, err, len(doc["pages"]), time.perf_counter() - t0
    except pipeline.PFError as e:
        return rel, f"{e.code} {e.title}: {e.message}", 0, time.perf_counter() - t0
    except Exception as e:
        return rel, f"{type(e).__name__}: {e}", 0, time.perf_counter() - t0


def _parallel(files, base, out_root, args, t_all):
    from concurrent.futures import ProcessPoolExecutor, as_completed
    opts = dict(annotate=args.annotate, include_headers=args.include_headers, json=args.json)
    failures = []
    with ProcessPoolExecutor(args.workers) as ex:
        futs = [ex.submit(_one, f, base, out_root, opts) for f in files]
        for i, fu in enumerate(as_completed(futs), 1):
            rel, err, pages, secs = fu.result()
            if err:
                failures.append((rel, err))
                print(f"[{i}/{len(files)}] FAIL {rel}: {err}", file=sys.stderr)
            elif not args.quiet:
                print(f"[{i}/{len(files)}] OK   {rel} ({pages} pages, {secs:.1f}s)")
    if not args.quiet:
        print(f"Done: {len(files) - len(failures)}/{len(files)} converted in {time.perf_counter() - t_all:.1f}s -> {out_root}")
    if failures:
        print(f"{len(failures)} file(s) failed or were only partially converted:", file=sys.stderr)
        for rel, why in failures:
            print(f"  {rel}: {why}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
