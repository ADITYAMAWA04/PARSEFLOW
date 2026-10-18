"""Large-document test: build an N-page PDF (digital text, tables, and a scanned page every
`--scan-every` pages), process it page by page with checkpoints, and report wall time, peak RSS
and output completeness.

    python bench/large_doc.py --pages 400 [--scan-every 50] [--out bench_out/large]
"""
import argparse
import io
import json
import os
import resource
import sys
import time
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

TEXT = ("Page {n}. The survey team recorded measurements at station {n} under clear conditions. "
        "Values were checked twice and entered into the shared log before the end of the shift. ")


def build(path, n, scan_every):
    doc = pymupdf.open()
    font = None
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",):
        if os.path.exists(p):
            font = ImageFont.truetype(p, 40)
    for i in range(1, n + 1):
        page = doc.new_page()
        if scan_every and i % scan_every == 0:
            img = Image.new("L", (1700, 2200), 255)
            d = ImageDraw.Draw(img)
            for k in range(8):
                d.text((150, 200 + 70 * k), f"Scanned page {i} line {k + 1}: field notes and sample ids.", fill=0, font=font)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            page.insert_image(page.rect, stream=buf.getvalue())
            continue
        page.insert_text((72, 60), f"Chapter {1 + i // 20}", fontsize=16)
        y = 100
        for k in range(14):
            page.insert_text((72, y), (TEXT.format(n=i))[k * 6 % 60:][:95], fontsize=10)
            y += 14
        if i % 10 == 0:
            for r in range(5):
                for c in range(3):
                    page.insert_text((72 + c * 140, 340 + r * 18), f"r{r}c{c}-{i}" if r else f"Col{c}", fontsize=10)
        page.insert_text((300, 770), str(i), fontsize=9)
    doc.save(str(path))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=400)
    ap.add_argument("--scan-every", type=int, default=50)
    ap.add_argument("--out", default=str(REPO / "bench_out" / "large"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pdf = out / f"large_{args.pages}.pdf"
    build(pdf, args.pages, args.scan_every)
    from processing import pipeline
    work = out / "work"
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    prog = pipeline.Progress(str(work / "status.json"))
    doc = pipeline.process(str(pdf), pdf.name, "large", str(work), prog)
    wall = time.perf_counter() - t0
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    md = (work / "document.md").read_text()
    missing = [i for i in range(1, args.pages + 1) if not (args.scan_every and i % args.scan_every == 0) and f"Page {i}." not in md]
    scanned_ok = [i for i in range(1, args.pages + 1) if args.scan_every and i % args.scan_every == 0 and f"Scanned page {i}" in md]
    res = dict(pages=args.pages, scanned_pages=args.pages // args.scan_every if args.scan_every else 0, wall_seconds=round(wall, 1),
               seconds_per_page=round(wall / args.pages, 3), peak_rss_mb=round(peak / 1024, 1), rss_before_mb=round(rss0 / 1024, 1),
               status=doc["processing_status"], failed_pages=doc["failed_pages"], blocks=len(doc["blocks"]),
               digital_pages_missing_text=missing, scanned_pages_with_text=len(scanned_ok), checkpoints=len(os.listdir(work / "pages")),
               markdown_chars=len(md), cpu_count=os.cpu_count())
    (out / "large_result.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
