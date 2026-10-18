"""Shared fixtures: small synthetic PDFs generated on the fly (no binary fixtures in the repo).
Synthetic documents test specific behaviours; they do not replace the official benchmark."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

FONT_TTF = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                             "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                             "/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf") if os.path.exists(p)), None)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    for k in ("VISION_API_KEY", "LLM_API_KEY", "LLM_PROVIDER", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MAX_PAGES", "PARSEFLOW_PAGE_LIMIT", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL",
              "VISION_PROVIDER", "VISION_MODEL", "LLM_MODEL", "MAX_VISION_REGIONS", "AI_VISION_MODEL",
              "LOCAL_AI_DURING_PROCESSING", "OLLAMA_URL", "VLM_SERVER", "VLM_API_KEY", "VLM_MODEL", "VLM_MAX_RETRIES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PARSEFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PARSEFLOW_ENV_FILE", str(tmp_path / "test.env"))   # never touch the real .env
    monkeypatch.setenv("OLLAMA_AUTODETECT", "0")                            # no local model unless a test mocks one
    from processing import llm
    llm._OLLAMA.update(t=0.0, models=None)
    llm._resolved.clear(); llm._GEM_LIST.clear(); llm._BUSY.clear(); llm._COMPAT_LIST.clear()
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    from processing import vlm_ocr
    vlm_ocr._DOWN.update(until=0.0, why=None, server=None)
    for k in ("AI_BASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    for k in ("TESSERACT_CMD", "DATABASE_URL", "POSTGRES_URL", "POSTGRES_URL_NON_POOLING", "VERCEL", "RENDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OCR_WORKERS", "1")
    yield


def text_pdf(path, pages):
    """pages: list of lists of (x, y, text, size[, font]). Coordinates in points on a letter page."""
    doc = pymupdf.open()
    for items in pages:
        p = doc.new_page(width=612, height=792)
        for it in items:
            x, y, t, size = it[:4]
            font = it[4] if len(it) > 4 else "helv"
            p.insert_text((x, y), t, fontsize=size, fontname=font)
    doc.save(str(path))
    doc.close()
    return str(path)


def para_lines(text, width_chars=90):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width_chars:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def image_with_text(lines, size=(1700, 2200), font_px=42, origin=(150, 150), line_gap=1.6, angle=0):
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_TTF, font_px) if FONT_TTF else ImageFont.load_default()
    x, y = origin
    for ln in lines:
        d.text((x, y), ln, fill=0, font=font)
        y += int(font_px * line_gap)
    if angle:
        img = img.rotate(angle, fillcolor=255, expand=False)
    return img


def scanned_pdf(path, img, dpi=200):
    doc = pymupdf.open()
    w, h = img.size[0] * 72 / dpi, img.size[1] * 72 / dpi
    p = doc.new_page(width=w, height=h)
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    p.insert_image(p.rect, stream=buf.getvalue())
    doc.save(str(path))
    doc.close()
    return str(path)


def needs_tesseract():
    return pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def latex_pdf(tmp_path, body, name="doc"):
    if not shutil.which("pdflatex"):
        pytest.skip("pdflatex not installed")
    tex = tmp_path / f"{name}.tex"
    tex.write_text("\\documentclass{article}\\usepackage{amsmath,amssymb}\\pagestyle{empty}\\begin{document}\n" + body + "\n\\end{document}\n")
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "-output-directory", str(tmp_path), str(tex)], capture_output=True, timeout=120)
    pdf = tmp_path / f"{name}.pdf"
    if not pdf.exists():
        pytest.skip("pdflatex failed")
    return str(pdf)
