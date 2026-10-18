import io

import pymupdf
from PIL import Image, ImageDraw

from conftest import image_with_text, needs_tesseract, scanned_pdf, text_pdf
from processing import pipeline, scan

LINES = ["Annual maintenance report for the water treatment plant.",
         "The filters were replaced in March and the pumps were serviced.",
         "No leaks were found during the pressure test of the main line."]


@needs_tesseract()
def test_scanned_page_ocr_with_word_confidence(tmp_path):
    out = pipeline.process_file(scanned_pdf(tmp_path / "s.pdf", image_with_text(LINES)))
    md = out["_markdown"]
    assert "water treatment plant" in md and "pressure test" in md
    b = [b for b in out["blocks"] if b["extractor"] == "ocr" and b.get("content")][0]
    assert b["word_boxes"] and 0 < b["confidence"] <= 1
    assert b["verification"] == "review"  # single OCR engine is not independent verification
    assert out["pages"][0]["ocr"][0]["words"] > 10


@needs_tesseract()
def test_skewed_scan_is_deskewed(tmp_path):
    img = image_with_text(LINES, angle=2.5)
    pre, info = scan.preprocess(img)
    assert abs(abs(info["skew_deg"]) - 2.5) < 0.6 and info.get("deskewed")
    out = pipeline.process_file(scanned_pdf(tmp_path / "skew.pdf", img))
    assert "filters were replaced" in out["_markdown"]


def test_unskewed_page_is_left_alone():
    _, info = scan.preprocess(image_with_text(LINES))
    assert abs(info["skew_deg"]) < 0.3 and not info.get("deskewed")


@needs_tesseract()
def test_text_inside_figure_is_extracted(tmp_path):
    fig = image_with_text(["INPUT", "PROCESS", "OUTPUT"], size=(900, 600), font_px=60, origin=(250, 120), line_gap=2.2)
    d = ImageDraw.Draw(fig)
    d.rectangle((200, 100, 700, 520), outline=0, width=5)
    buf = io.BytesIO()
    fig.save(buf, "PNG")
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 80), "Figure 1 shows the processing stages of the system in order.", fontsize=11)
    p.insert_image(pymupdf.Rect(150, 120, 450, 320), stream=buf.getvalue())
    p.insert_text((150, 340), "Figure 1: Pipeline overview.", fontsize=10)
    path = tmp_path / "fig.pdf"
    doc.save(str(path))
    out = pipeline.process_file(str(path))
    figs = [b for b in out["blocks"] if b["type"] == "figure"]
    assert figs and figs[0].get("detected_text")
    words = " ".join(d["text"] for d in figs[0]["detected_text"])
    assert "INPUT" in words and "OUTPUT" in words
    assert figs[0]["preserved_image"] and figs[0]["image_reference"]
    cap = [b for b in out["blocks"] if b["type"] == "caption"]
    assert cap and any(r.startswith("caption_of:") for r in cap[0]["relationships"])
    assert "INPUT" in out["_markdown"]


@needs_tesseract()
def test_photo_without_text_gets_no_ocr_text(tmp_path):
    img = Image.new("RGB", (800, 600), (90, 140, 200))
    d = ImageDraw.Draw(img)
    d.ellipse((200, 150, 600, 450), fill=(250, 210, 60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 80), "A photograph of the sun over the lake.", fontsize=11)
    p.insert_image(pymupdf.Rect(100, 120, 500, 420), stream=buf.getvalue())
    path = tmp_path / "photo.pdf"
    doc.save(str(path))
    out = pipeline.process_file(str(path))
    figs = [b for b in out["blocks"] if b["type"] == "figure"]
    assert figs and not figs[0].get("ocr_text")
    assert figs[0]["confidence"] < 0.6  # no evidence -> not presented as a confident extraction


def test_figure_caption_is_linked_to_its_figure(tmp_path):
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 90), "Rainfall was measured at three stations during the survey period.", fontsize=11)
    for i, h in enumerate((80, 140, 110, 170)):  # a small vector bar chart
        p.draw_rect(pymupdf.Rect(150 + i * 70, 380 - h, 190 + i * 70, 380), color=(0, 0, 0), fill=(0.4, 0.4, 0.8))
    p.draw_line((140, 380), (440, 380))
    p.insert_text((150, 410), "Figure 1. Monthly rainfall at the northern station.", fontsize=10)
    path = tmp_path / "fig.pdf"
    doc.save(str(path))
    out = pipeline.process_file(str(path))
    caps = [b for b in out["blocks"] if b["type"] == "caption"]
    figs = [b for b in out["blocks"] if b["type"] in ("figure", "chart", "diagram", "unknown") and b["page"] == 1]
    assert caps and figs
    link = [r for r in caps[0]["relationships"] if r.startswith("caption_of:")]
    assert link and link[0].split(":", 1)[1] in {f["block_id"] for f in figs}
    assert "Monthly rainfall at the northern station" in out["_markdown"]


# --- text inside visuals (local OCR, no model) ------------------------------------------------
from conftest import FONT_TTF  # noqa: E402
from processing import visual  # noqa: E402
from PIL import ImageFont  # noqa: E402


def _flowchart():
    im = Image.new("RGB", (900, 420), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT_TTF, 30)
    for i, (x, t) in enumerate([(40, "Input"), (340, "Parser"), (640, "Output")]):
        d.rectangle((x, 150, x + 220, 250), outline="black", width=4)
        d.text((x + 40, 183), t, fill="black", font=f)
        if i < 2:
            d.line((x + 220, 200, x + 300, 200), fill="black", width=4)
    return im


def _chart():
    im = Image.new("RGB", (900, 500), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT_TTF, 24)
    d.text((300, 20), "Revenue by Quarter", fill="black", font=ImageFont.truetype(FONT_TTF, 30))
    d.line((100, 420, 850, 420), fill="black", width=3)
    d.line((100, 80, 100, 420), fill="black", width=3)
    for i, (q, h) in enumerate([("Q1", 120), ("Q2", 200), ("Q3", 160), ("Q4", 280)]):
        x = 160 + i * 170
        d.rectangle((x, 420 - h, x + 90, 420), fill=(70, 110, 200))
        d.text((x + 25, 430), q, fill="black", font=f)
        d.text((x + 15, 420 - h - 32), str(h), fill="black", font=f)
    return im


def _pdf_with_images(path, images, scanned=False):
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "This report describes the document pipeline and quarterly revenue.", fontsize=11)
    y = 100
    for im in images:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        h = 468 * im.size[1] / im.size[0]
        p.insert_image(pymupdf.Rect(72, y, 540, y + h), stream=buf.getvalue())
        y += h + 40
    if scanned:
        pix = p.get_pixmap(dpi=200)
        doc = pymupdf.open()
        q = doc.new_page()
        q.insert_image(q.rect, stream=pix.tobytes("png"))
    doc.save(str(path))
    return str(path)


@needs_tesseract()
def test_labels_inside_boxes_are_read():
    items, info = visual.image_text(_flowchart().convert("L"))
    texts = [d["text"] for d in items]
    assert {"Input", "Parser", "Output"} <= set(texts)
    assert info["has_h"] and "psm11_lines_removed" in info["passes"]


@needs_tesseract()
def test_figure_text_appears_in_ui_content_and_markdown(tmp_path):
    out = pipeline.process_file(_pdf_with_images(tmp_path / "v.pdf", [_flowchart(), _chart()]))
    figs = [b for b in out["blocks"] if b["type"] == "figure"]
    assert len(figs) == 2 and all(b["content"] for b in figs)
    assert "Input" in figs[0]["content"] and figs[0]["visual_type"] == "diagram"
    assert figs[1]["visual_type"] == "chart" and "Revenue by Quarter" in figs[1]["description"]
    assert "heuristic" in figs[1]["description_source"]
    md = out["_markdown"]
    assert md.count("Revenue by Quarter") == 1 and "Parser" in md          # no duplicated figure text


@needs_tesseract()
def test_scanned_page_visual_labels_read_in_position(tmp_path):
    out = pipeline.process_file(_pdf_with_images(tmp_path / "s.pdf", [_flowchart()], scanned=True))
    md = out["_markdown"]
    assert "Parser" in md and "This report describes" in md
    assert md.index("This report describes") < md.index("Parser")


def test_sequence_label_correction_needs_siblings_and_keeps_original():
    items = [dict(text=t, bbox=(i * 10, 0, i * 10 + 8, 8), confidence=0.9) for i, t in enumerate(["Ql", "Q2", "Q3", "Q4"])]
    assert visual.fix_sequence_labels(items) == 1
    assert items[0]["text"] == "Q1" and items[0]["ocr_raw"] == "Ql"
    lone = [dict(text="Al", bbox=(0, 0, 8, 8), confidence=0.9)]          # no sibling evidence: unchanged
    assert visual.fix_sequence_labels(lone) == 0 and lone[0]["text"] == "Al"


def test_junk_fragments_from_drawings_are_not_reported():
    assert not visual.plausible("Lu") and not visual.plausible("ry)") and not visual.plausible("4|")
    assert not visual.plausible("VVVV") and not visual.plausible("WMW") and not visual.plausible("llll")
    assert visual.plausible("Input") and visual.plausible("Q1") and visual.plausible("280") and visual.plausible("12.5%")


def test_picture_without_text_gets_no_text():
    assert visual.classify([], False, False)[0] == "picture"


def _notes_image(ruled=True):
    im = Image.new("RGB", (1200, 700), "white")
    d = ImageDraw.Draw(im)
    if ruled:
        for y in range(60, 700, 36):
            d.line((0, y, 1200, y), fill=(205, 205, 205), width=1)
    f = ImageFont.truetype(FONT_TTF, 30)
    d.text((80, 100), "Balance factor = height(L) - height(R)", fill=(30, 90, 200), font=f)
    d.ellipse((500, 300, 560, 360), outline="black", width=3)
    return im


def test_ruled_notebook_lines_detected_and_printed_page_not():
    assert visual.ruled_lines(_notes_image(True).convert("L")) >= 10
    assert visual.ruled_lines(_notes_image(False).convert("L")) == 0
    assert visual.ruled_lines(image_with_text(LINES)) == 0


@needs_tesseract()
def test_ruled_note_image_goes_to_handwritten_with_rough_label(tmp_path):
    out = pipeline.process_file(_pdf_with_images(tmp_path / "n.pdf", [_notes_image(True)]))
    figs = [b for b in out["blocks"] if b["type"] == "figure"]
    assert figs and figs[0]["handwritten"] and "ruled notebook lines" in figs[0]["handwritten_evidence"]
    assert any(w.startswith("Looks handwritten") for w in figs[0]["warnings"])
    out = pipeline.process_file(_pdf_with_images(tmp_path / "c.pdf", [_chart()]))     # a printed chart is not
    assert not any(b.get("handwritten") for b in out["blocks"])
