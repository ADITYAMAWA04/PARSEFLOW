"""Recover the identity of glyphs that the PDF does not map to Unicode (shown as U+FFFD).

TeX's extension font (CMEX) is usually embedded without a ToUnicode map, so summation, integral,
radical and big-delimiter glyphs come out as U+FFFD. Their identity is still recorded in the
embedded Type 1 font program as a glyph NAME (e.g. "summationdisplay"). We read that name from the
font program (eexec-decrypted CharStrings, in FreeType's glyph-index order) and map well-known
names to Unicode. This is a lookup of information present in the file, not a guess: glyphs whose
names are unknown stay unmapped.
"""
import re

NAME_TO_UNICODE = {
    "summation": "∑", "product": "∏", "coproduct": "∐", "integral": "∫", "contintegral": "∮",
    "union": "⋃", "intersection": "⋂", "unionmulti": "⨄", "circleplus": "⨁", "circlemultiply": "⨂",
    "circledot": "⨀", "logicaland": "⋀", "logicalor": "⋁", "radical": "√",
    "parenleft": "(", "parenright": ")", "bracketleft": "[", "bracketright": "]", "braceleft": "{",
    "braceright": "}", "angbracketleft": "⟨", "angbracketright": "⟩", "floorleft": "⌊", "floorright": "⌋",
    "ceilingleft": "⌈", "ceilingright": "⌉", "slash": "/", "backslash": "\\", "bar": "|", "bardbl": "‖",
}
# pieces of extensible delimiters: emit the delimiter once (top piece), drop the rest
PIECE_TOP = re.compile(r"^(parenleft|parenright|bracketleft|bracketright|braceleft|braceright)tp$")
PIECE_OTHER = re.compile(r"(bt|ex|mid|ext)$|^(brace|paren|bracket).*(bt|ex|mid)$|^radicalv|^radicalbt|^radicaltp|^arrowvertex|^bracevertex")

_cache = {}


def _decrypt(data, r=55665):
    out = bytearray()
    c1, c2 = 52845, 22719
    for b in data:
        out.append(b ^ (r >> 8))
        r = ((b + r) * c1 + c2) & 0xFFFF
    return bytes(out[4:])


def type1_glyph_names(buf):
    """Glyph names of a Type 1 font program in FreeType glyph-index order (.notdef first)."""
    i = buf.find(b"eexec")
    if i < 0:
        return []
    j = i + 5
    while j < len(buf) and buf[j:j + 1] in (b"\r", b"\n", b" ", b"\t"):
        j += 1
    enc = buf[j:]
    if re.fullmatch(rb"[0-9A-Fa-f\s]+", enc[:64] or b"x"):  # hex-encoded (PFA without binary section)
        hexs = re.sub(rb"\s+", b"", enc.split(b"0000000000")[0])
        enc = bytes.fromhex(hexs[: len(hexs) // 2 * 2].decode())
    dec = _decrypt(enc)
    k = dec.find(b"/CharStrings")
    if k < 0:
        return []
    names, pos = [], k
    pat = re.compile(rb"/([^\s/\[\]{}()<>%]+)\s+(\d+)\s+(RD|-\|)\s")
    while True:
        m = pat.search(dec, pos)
        if not m:
            break
        names.append(m.group(1).decode("latin-1"))
        pos = m.end() + int(m.group(2))
    if ".notdef" in names and names[0] != ".notdef":
        z = names.index(".notdef")
        names[0], names[z] = names[z], names[0]
    return names


def name_to_char(name):
    base = re.sub(r"(text|display)$", "", name)
    base = re.sub(r"(Bigg|bigg|Big|big)$", "", base)
    if base in NAME_TO_UNICODE:
        return NAME_TO_UNICODE[base]
    if PIECE_TOP.match(name):
        return NAME_TO_UNICODE.get(name[:-2])
    if PIECE_OTHER.search(name):
        return ""  # continuation piece of an extensible glyph: contributes no symbol of its own
    return None


def resolve_page(page):
    """Return {(round(x,1), round(y,1)): char} for unmapped glyphs whose name is known."""
    doc = page.parent
    fonts = {}
    for xref, ext, typ, basefont, refname, encoding in page.get_fonts():
        if typ in ("Type1", "Type1C") and ext in ("pfa", "pfb", "n/a", "cff"):
            fonts[basefont.split("+")[-1]] = xref
    out = {}
    try:
        trace = page.get_texttrace()
    except Exception:
        return out
    for sp in trace:
        bad = [c for c in sp["chars"] if c[0] == 0xFFFD]
        if not bad:
            continue
        fname = sp["font"].split("+")[-1]
        xref = fonts.get(fname)
        if not xref:
            continue
        key = (id(doc), xref)
        if key not in _cache:
            try:
                buf = doc.extract_font(xref)[3]
                _cache[key] = type1_glyph_names(buf) if buf else []
            except Exception:
                _cache[key] = []
        names = _cache[key]
        for c in bad:
            gid = c[1]
            if 0 <= gid < len(names):
                ch = name_to_char(names[gid])
                if ch is not None:
                    out[(round(c[2][0], 1), round(c[2][1], 1))] = ch
    return out
