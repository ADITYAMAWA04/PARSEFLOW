"""Line joining and de-hyphenation with explicit evidence rules.

A hyphen at the end of a line is ambiguous: it is either a typesetting break inside one word
("docu-\\nment" -> "document") or a genuine compound split at its hyphen ("self-\\nconsistent").
We decide with evidence in this order:
  1. the joined form appears elsewhere in the document  -> join
  2. the hyphenated form appears elsewhere mid-line     -> keep the hyphen
  3. the left part is a common compound prefix           -> keep the hyphen
  4. the right part starts lowercase                     -> join (typesetting break)
  5. otherwise                                           -> keep the hyphen
"""
import re
import unicodedata

HYPHENS = "-‐‑­"
_COMPOUND_PREFIXES = {
    "self", "non", "well", "cross", "half", "all", "ex", "co", "multi", "semi", "anti", "pre", "post",
    "quasi", "pseudo", "inter", "intra", "sub", "super", "over", "under", "high", "low", "long", "short",
    "full", "two", "three", "four", "one", "first", "second", "third", "state", "real", "time", "large",
    "small", "end", "top", "bottom", "left", "right", "open", "closed", "data", "model", "user", "case",
}
_WORD = re.compile(r"[A-Za-zÀ-ɏ]+")


class Vocabulary:
    """Words seen anywhere in the document; used as evidence for de-hyphenation."""

    def __init__(self):
        self.words = set()
        self.hyphenated = set()

    def add_text(self, text):
        for w in _WORD.findall(text or ""):
            self.words.add(w.lower())
        for m in re.finditer(r"([A-Za-z]+)-([A-Za-z]+)", text or ""):
            self.hyphenated.add((m.group(1).lower(), m.group(2).lower()))


def _decide(left_word, right_word, vocab):
    lw, rw = left_word.lower(), right_word.lower()
    if vocab is not None:
        if (lw + rw) in vocab.words:
            return "join", "joined form occurs elsewhere in document"
        if (lw, rw) in vocab.hyphenated:
            return "keep", "hyphenated form occurs elsewhere in document"
    if lw in _COMPOUND_PREFIXES:
        return "keep", "common compound prefix"
    if right_word[:1].islower():
        return "join", "line-break hyphen before lowercase continuation"
    return "keep", "continuation is capitalised or non-alphabetic"


def join_lines(lines, vocab=None, decisions=None):
    """Join text lines of one paragraph into a single string, resolving line-end hyphens.
    `decisions` (optional list) receives (left, right, action, reason) tuples as evidence."""
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        if out[-1] in HYPHENS and len(out) >= 2 and out[-2].isalpha():
            m_left = re.search(r"([A-Za-zÀ-ɏ]+)[" + HYPHENS + r"]$", out)
            m_right = re.match(r"([A-Za-zÀ-ɏ]+)", line)
            if m_left and m_right:
                action, reason = _decide(m_left.group(1), m_right.group(1), vocab)
                if decisions is not None:
                    decisions.append((m_left.group(1), m_right.group(1), action, reason))
                if action == "join":
                    out = out[:-1] + line
                else:
                    out = out[:-1] + "-" + line
                continue
            out = out + line  # hyphen followed by non-letter: keep as-is, no space
            continue
        out = out + " " + line
    return re.sub(r"[ \t]+", " ", out)


# TeX and some other typesetters draw an accented letter as two glyphs: a spacing accent and the
# letter ("J´an Min´aˇc"). Recombine them ("Ján Mináč"). Only the Unicode spacing-accent characters
# are used, never ASCII ^ ~ ` (they are common in code and maths).
_SPACING_ACCENTS = {"\u00b4": "\u0301", "\u02ca": "\u0301", "\u02cb": "\u0300", "\u02c6": "\u0302", "\u02dc": "\u0303",
                    "\u00a8": "\u0308", "\u02c7": "\u030c", "\u02d8": "\u0306", "\u02d9": "\u0307", "\u02da": "\u030a",
                    "\u02dd": "\u030b", "\u00af": "\u0304", "\u00b8": "\u0327", "\u02db": "\u0328"}
_ACCENT_RE = re.compile("([" + "".join(_SPACING_ACCENTS) + "]) ?([A-Za-z\u0131\u0237])")
_ACCENT_AFTER_RE = re.compile("([A-Za-z])([\u00b8\u02db])")     # cedilla / ogonek follow the letter


def compose_accents(s):
    if not s or not any(ch in s for ch in _SPACING_ACCENTS):
        return s

    def comb(letter, acc):
        letter = {"\u0131": "i", "\u0237": "j"}.get(letter, letter)
        out = unicodedata.normalize("NFC", letter + _SPACING_ACCENTS[acc])
        return out if len(out) == 1 else letter + acc
    s = _ACCENT_AFTER_RE.sub(lambda m: comb(m.group(1), m.group(2)), s)
    return _ACCENT_RE.sub(lambda m: comb(m.group(2), m.group(1)), s)


def normalize_ligatures(s):
    # two typographic single quotes are how some typesetters draw a double quote
    s = (s or "").replace("\u2018\u2018", "\u201c").replace("\u2019\u2019", "\u201d").replace("ﬀ", "ff").replace("ﬁ", "fi").replace("ﬂ", "fl") \
        .replace("ﬃ", "ffi").replace("ﬄ", "ffl").replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    return compose_accents(s)
