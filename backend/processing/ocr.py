"""OCR via Tesseract (local). Returns text, bbox and confidence; coordinates are never discarded and
low-confidence words are kept (their confidence travels with them) rather than silently dropped."""
import contextlib
import functools
import os
import re
import statistics
import tempfile

import pytesseract
from pytesseract import Output
from PIL import ImageOps


def _locate_tesseract():
    """pytesseract only looks on PATH. The Windows installer does not add itself to PATH by default,
    so also look in the usual install folders (and honour TESSERACT_CMD)."""
    import os
    import shutil
    cand = [os.getenv("TESSERACT_CMD")]
    if not shutil.which("tesseract"):
        local = os.getenv("LOCALAPPDATA", "")
        cand += [r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                 os.path.join(local, "Programs", "Tesseract-OCR", "tesseract.exe") if local else None,
                 os.path.join(local, "Tesseract-OCR", "tesseract.exe") if local else None,
                 "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/opt/local/bin/tesseract"]
    for c in cand:
        if c and os.path.isfile(c):
            pytesseract.pytesseract.tesseract_cmd = c
            return c
    return None


_locate_tesseract()

# Tesseract's own OpenMP threads fight each other (and our page-level parallelism): one thread per
# Tesseract process is about 3x faster on a page here. Set OMP_THREAD_LIMIT yourself to override.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")


@contextlib.contextmanager
def _as_file(img):
    """Hand Tesseract an uncompressed BMP instead of letting pytesseract PNG-encode every image
    (PNG encoding of a full page costs about a second)."""
    fd, path = tempfile.mkstemp(suffix=".bmp")
    os.close(fd)
    try:
        img.save(path, "BMP")
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@functools.lru_cache(maxsize=1)
def _version():
    try:
        return "tesseract " + str(pytesseract.get_tesseract_version())
    except Exception:
        return None


def available():
    return _version()


def words(img, psm=3, lang="eng", timeout=0, oem=1, preprocessed=False):
    g = img if preprocessed else ImageOps.autocontrast(img.convert("L"))
    cfg = f"--oem {oem} --psm {psm}"
    with _as_file(g) as path:
        d = pytesseract.image_to_data(path, lang=lang, output_type=Output.DICT, config=cfg, timeout=timeout)
    out = []
    for i, t in enumerate(d["text"]):
        try:
            c = float(d["conf"][i])
        except Exception:
            continue
        if t.strip() and c >= 0:
            x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            out.append(dict(text=t.strip(), conf=c / 100, box=(x, y, x + w, y + h),
                            key=(d["block_num"][i], d["par_num"][i]),
                            line=(d["block_num"][i], d["par_num"][i], d["line_num"][i]), h=h, order=i))
    return out


def stats(ws):
    if not ws:
        return dict(words=0, mean_conf=0.0, low_conf_frac=1.0)
    cs = [w["conf"] for w in ws]
    return dict(words=len(ws), mean_conf=round(sum(cs) / len(cs), 3), low_conf_frac=round(sum(c < 0.5 for c in cs) / len(cs), 3))


def osd_rotation(img, timeout=0):
    """Tesseract orientation detection -> (rotation degrees 0/90/180/270, confidence)."""
    try:
        with _as_file(img.convert("L")) as path:
            r = pytesseract.image_to_osd(path, output_type=Output.DICT, config="--psm 0", timeout=timeout)
        return int(r.get("rotate", 0)), float(r.get("orientation_conf", 0))
    except Exception:
        return 0, 0.0


def paragraphs(ws, min_conf=0.0):
    """Group words into Tesseract paragraphs, preserving Tesseract's reading order."""
    groups, first = {}, {}
    for w in ws:
        if w["conf"] >= min_conf:
            groups.setdefault(w["key"], []).append(w)
            first.setdefault(w["key"], w.get("order", 0))
    res = []
    for k in sorted(groups, key=lambda k: first[k]):
        g = groups[k]
        lines = {}
        for w in g:
            lines.setdefault(w["line"], []).append(w)
        line_txt = [" ".join(x["text"] for x in sorted(l, key=lambda z: z["box"][0])) for _, l in sorted(lines.items())]
        bb = (min(w["box"][0] for w in g), min(w["box"][1] for w in g), max(w["box"][2] for w in g), max(w["box"][3] for w in g))
        res.append(dict(lines=line_txt, text="\n".join(line_txt), bbox=bb, conf=sum(w["conf"] for w in g) / len(g),
                        min_conf=min(w["conf"] for w in g), height=statistics.median(sorted(w["h"] for w in g)),
                        words=len(g), n_lines=len(lines),
                        word_boxes=[dict(text=w["text"], bbox=w["box"], confidence=round(w["conf"], 3)) for w in g]))
    return res


def quick_text(img):
    try:
        return pytesseract.image_to_string(ImageOps.autocontrast(img.convert("L")), config="--psm 6").strip()
    except Exception:
        return ""


def quick_conf(img):
    ws = words(img, 6)
    return (sum(w["conf"] for w in ws) / len(ws), " ".join(w["text"] for w in ws)) if ws else (0.0, "")


def region_text(img, psm=11, min_conf=0.5, lang="eng", timeout=0):
    """Text inside a figure crop: one entry per text line, boxes in crop pixel coordinates."""
    try:
        ws = words(img, psm=psm, lang=lang, timeout=timeout)
    except Exception:
        return []
    ws = [w for w in ws if re.search(r"[A-Za-z0-9]", w["text"])]
    lines = {}
    for w in ws:
        lines.setdefault(w["line"], []).append(w)
    out = []
    for _, l in sorted(lines.items(), key=lambda kv: min(w["order"] for w in kv[1])):
        l.sort(key=lambda w: w["box"][0])
        conf = sum(w["conf"] for w in l) / len(l)
        bb = (min(w["box"][0] for w in l), min(w["box"][1] for w in l), max(w["box"][2] for w in l), max(w["box"][3] for w in l))
        out.append(dict(text=" ".join(w["text"] for w in l), bbox=bb, confidence=round(conf, 3), keep=conf >= min_conf))
    return out
