"""Mathematical expressions reconstructed from the PDF text layer (no model, no guessing).

Digital PDFs produced by TeX, Word, etc. contain every math glyph with its font, size and position.
We recover LaTeX from that geometry:

  * glyph identity  : Unicode from the text layer, mapped to LaTeX commands (\\alpha, \\in, \\sum ...);
                      font families select alphabets (MSBM -> \\mathbb, EUFM -> \\mathfrak, CMSY caps -> \\mathcal)
  * scripts         : smaller glyphs raised / lowered relative to the current base -> ^{...} / _{...}
  * big operators   : glyphs stacked above / below sum/product/integral signs -> limits
  * fractions       : horizontal rules (from the page's vector drawings) with glyphs above and below
  * radicals        : a rule starting at a radical sign -> \\sqrt{...}

Display equations become `equation` blocks; inline math inside paragraphs is wrapped in $...$.
Glyphs the PDF does not map to Unicode (U+FFFD, e.g. some big delimiters) are NOT replaced by a
guess: they are omitted and counted, and the block's glyph_coverage records the loss.
Syntactically valid LaTeX is not proof of a correct reading; blocks stay in "review" (verifier.py).
"""
import re
import statistics
import unicodedata

MATH_FONT = re.compile(
    r"(CMMI|CMSY|CMEX|CMBSY|CMMIB|MSAM|MSBM|EUFM|EUSM|EUEX|EURM|RSFS|LMMath|Math|Symbol|STIX|Euclid|MTSY|MTEX|MTMI|"
    r"txsy|txex|txmi|pxsy|pxex|pxmi|MnSymbol|esint|wasy|stmary|Asana|XITS|cmmi|cmsy|cmex|CMR5\b|CMR6\b|CMR7\b|CMR8\b|"
    r"CMR5$|CMR6$|CMR7$|CMR8$|MathematicalPi|Mathematica|Universal-Greek|MT-Extra|MTExtra)", re.I)
BB_FONT = re.compile(r"MSBM|DoubleStruck|Blackboard|BBold|dsrom", re.I)
FRAK_FONT = re.compile(r"EUFM|EUFB|Fraktur", re.I)
CAL_FONT = re.compile(r"CMSY|CMBSY|LMMathSymbols|cal", re.I)
SCR_FONT = re.compile(r"RSFS|Script", re.I)
ITALIC_FONT = re.compile(r"Ital|Oblique|CMMI|MathItalic|-It\b|-It$|Italic", re.I)

GREEK = {
    "α": "\\alpha", "β": "\\beta", "γ": "\\gamma", "δ": "\\delta", "ε": "\\varepsilon", "ϵ": "\\epsilon", "ζ": "\\zeta",
    "η": "\\eta", "θ": "\\theta", "ϑ": "\\vartheta", "ι": "\\iota", "κ": "\\kappa", "λ": "\\lambda", "μ": "\\mu", "µ": "\\mu",
    "ν": "\\nu", "ξ": "\\xi", "π": "\\pi", "ϖ": "\\varpi", "ρ": "\\rho", "ϱ": "\\varrho", "σ": "\\sigma", "ς": "\\varsigma",
    "τ": "\\tau", "υ": "\\upsilon", "φ": "\\varphi", "ϕ": "\\phi", "χ": "\\chi", "ψ": "\\psi", "ω": "\\omega",
    "Γ": "\\Gamma", "Δ": "\\Delta", "Θ": "\\Theta", "Λ": "\\Lambda", "Ξ": "\\Xi", "Π": "\\Pi", "Σ": "\\Sigma",
    "Υ": "\\Upsilon", "Φ": "\\Phi", "Ψ": "\\Psi", "Ω": "\\Omega", "∆": "\\Delta", "Ω": "\\Omega", "ϑ": "\\vartheta",
}
SYMBOLS = {
    "∈": "\\in", "∉": "\\notin", "∋": "\\ni", "⊂": "\\subset", "⊃": "\\supset", "⊆": "\\subseteq", "⊇": "\\supseteq",
    "⊊": "\\subsetneq", "∪": "\\cup", "∩": "\\cap", "∞": "\\infty", "∂": "\\partial", "∇": "\\nabla", "≤": "\\leq",
    "≥": "\\geq", "≦": "\\leqq", "≧": "\\geqq", "≠": "\\neq", "≈": "\\approx", "≡": "\\equiv", "∼": "\\sim", "≃": "\\simeq",
    "≅": "\\cong", "→": "\\to", "←": "\\leftarrow", "↔": "\\leftrightarrow", "⇒": "\\Rightarrow", "⇐": "\\Leftarrow",
    "⇔": "\\Leftrightarrow", "↦": "\\mapsto", "⟶": "\\longrightarrow", "×": "\\times", "·": "\\cdot", "⋅": "\\cdot",
    "∙": "\\cdot", "±": "\\pm", "∓": "\\mp", "∑": "\\sum", "∏": "\\prod", "∐": "\\coprod", "∫": "\\int", "∬": "\\iint",
    "∮": "\\oint", "√": "\\sqrt", "∀": "\\forall", "∃": "\\exists", "∄": "\\nexists", "¬": "\\neg", "∧": "\\wedge",
    "∨": "\\vee", "⊗": "\\otimes", "⊕": "\\oplus", "⊙": "\\odot", "∘": "\\circ", "∗": "*", "−": "-", "–": "-", "′": "'",
    "″": "''", "ℓ": "\\ell", "ℏ": "\\hbar", "∅": "\\emptyset", "⟨": "\\langle", "⟩": "\\rangle", "〈": "\\langle",
    "〉": "\\rangle", "‖": "\\|", "∣": "\\mid", "…": "\\ldots", "⋯": "\\cdots", "⋮": "\\vdots", "⋱": "\\ddots",
    "⊥": "\\perp", "∥": "\\parallel", "∝": "\\propto", "≪": "\\ll", "≫": "\\gg", "⊢": "\\vdash", "⊤": "\\top",
    "⌊": "\\lfloor", "⌋": "\\rfloor", "⌈": "\\lceil", "⌉": "\\rceil", "ℝ": "\\mathbb{R}", "ℕ": "\\mathbb{N}",
    "ℤ": "\\mathbb{Z}", "ℚ": "\\mathbb{Q}", "ℂ": "\\mathbb{C}", "ℙ": "\\mathbb{P}", "ℍ": "\\mathbb{H}", "⋃": "\\bigcup",
    "⋂": "\\bigcap", "⨁": "\\bigoplus", "⨂": "\\bigotimes", "∖": "\\setminus", "⊲": "\\lhd", "◦": "\\circ",
    "□": "\\square", "∎": "\\blacksquare", "†": "\\dagger", "‡": "\\ddagger", "★": "\\star", "⋆": "\\star", "∠": "\\angle",
    "△": "\\triangle", "♯": "\\sharp", "♭": "\\flat", "ℜ": "\\Re", "ℑ": "\\Im", "℘": "\\wp", "ℵ": "\\aleph",
    "{": "\\{", "}": "\\}", "%": "\\%", "#": "\\#", "&": "\\&", "_": "\\_", "$": "\\$", "~": "\\sim", "^": "\\hat{}",
    "\\": "\\backslash", "ˆ": "", "˜": "", "¯": "", "´": "", "`": "", "¨": "",
}
BIG_OPS = set("∑∏∐∫∬∮⋃⋂⨁⨂")
OPERATORS = set("=+−-<>≤≥≠≈≡∼≃≅→←↔⇒⇐⇔↦×·⋅±∓∈∉∋⊂⊃⊆⊇∪∩∧∨⊗⊕∘/|‖∣:∖≪≫∝⊥")
FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "exp", "lim", "sup", "inf", "max", "min", "det", "dim",
         "ker", "deg", "gcd", "arg", "sinh", "cosh", "tanh", "arcsin", "arccos", "arctan", "Pr", "liminf", "limsup", "hom"}
MATH_ALNUM_STYLE = [("DOUBLE-STRUCK", "\\mathbb"), ("FRAKTUR", "\\mathfrak"), ("SCRIPT", "\\mathcal"),
                    ("BOLD", "\\mathbf"), ("SANS-SERIF", "\\mathsf"), ("MONOSPACE", "\\mathtt")]


# ----------------------------------------------------------------------------------- glyph mapping

def glyph_latex(ch, font=""):
    """LaTeX for one glyph. Returns None for unmapped glyphs (never guessed)."""
    if ch in ("�", "") or (0xE000 <= ord(ch[0]) <= 0xF8FF):
        return None
    if ch in GREEK:
        return GREEK[ch]
    o = ord(ch)
    if 0x1D400 <= o <= 0x1D7FF:  # Unicode mathematical alphanumerics
        name = unicodedata.name(ch, "")
        base = name.split()[-1]
        letter = None
        if "DIGIT" in name:
            letter = str(unicodedata.digit(ch, 0))
        elif "SMALL" in name and len(base) == 1:
            letter = base.lower()
        elif "CAPITAL" in name and len(base) == 1:
            letter = base.upper()
        else:
            g = {v[1:]: v for v in GREEK.values()}
            nm = base.lower()
            cmd = g.get(nm) or g.get(nm.capitalize() if "CAPITAL" in name else nm)
            return cmd
        for key, cmd in MATH_ALNUM_STYLE:
            if key in name:
                return f"{cmd}{{{letter}}}"
        return letter
    if ch in SYMBOLS:
        return SYMBOLS[ch]
    if ch.isalpha() and ch.isascii():
        if BB_FONT.search(font):
            return f"\\mathbb{{{ch}}}"
        if FRAK_FONT.search(font):
            return f"\\mathfrak{{{ch}}}"
        if SCR_FONT.search(font):
            return f"\\mathscr{{{ch}}}"
        if CAL_FONT.search(font) and ch.isupper():
            return f"\\mathcal{{{ch}}}"
        if re.search(r"Bold|CMBX|CMMIB|-Bd|Black", font):
            return f"\\mathbf{{{ch}}}"
        return ch
    if ch.isascii():
        return ch
    if unicodedata.category(ch).startswith("L"):
        return None  # non-ASCII letter we cannot express safely in math mode
    return None


def is_math_font(name):
    return bool(MATH_FONT.search(name or ""))


# ----------------------------------------------------------------------------------- page context

class PageContext:
    """Per-page data needed for 2-D math: horizontal rules (fraction bars, radical overlines)."""

    def __init__(self, page, body_size):
        self.body = body_size
        self.rules = []
        try:
            for d in page.get_drawings():
                for it in d["items"]:
                    if it[0] == "l":
                        p1, p2 = it[1], it[2]
                        if abs(p1.y - p2.y) < 0.6 and abs(p2.x - p1.x) > 2:
                            self.rules.append((min(p1.x, p2.x), (p1.y + p2.y) / 2, max(p1.x, p2.x)))
                    elif it[0] == "re":
                        r = it[1]
                        if r.height < 1.6 and r.width > 2 and d.get("fill") is not None or (r.height < 1.6 and r.width > 2 and d.get("type") in ("s", "fs")):
                            self.rules.append((r.x0, (r.y0 + r.y1) / 2, r.x1))
        except Exception:
            self.rules = []


# ----------------------------------------------------------------------------------- 2-D layout -> LaTeX

def _chars_of(lines):
    out = []
    for l in lines:
        sp = False
        for s in l["spans"]:
            for c in s.get("chars") or []:
                ch = c["c"]
                if not ch.strip() or unicodedata.category(ch[0]) in ("Cf", "Zs", "Cc") or s["size"] < 2:
                    if not ch.strip():
                        sp = True
                    continue
                x0, y0, x1, y1 = c["bbox"]
                out.append(dict(c=ch, x0=x0, y0=y0, x1=x1, y1=y1, ox=c["origin"][0], oy=c["origin"][1], size=s["size"],
                                font=s["font"], flags=s["flags"], sp=sp))
                sp = False
            if not (s.get("chars") or []) and s.get("text") == " ":
                sp = True
    return out


def _cluster_baselines(chars, tol):
    bl = sorted(c["oy"] for c in chars)
    groups = []
    for y in bl:
        if groups and y - groups[-1][-1] <= tol:
            groups[-1].append(y)
        else:
            groups.append([y])
    return [statistics.median(g) for g in groups]


class _Builder:
    def __init__(self, ctx, rules):
        self.ctx = ctx
        self.rules = rules
        self.total = 0
        self.mapped = 0

    def tok(self, c):
        self.total += 1
        t = glyph_latex(c["c"], c["font"])
        if t is None:
            return None
        self.mapped += 1
        return t

    def build(self, chars, depth=0):
        """chars: glyph dicts. Returns LaTeX string."""
        if not chars or depth > 12:
            return ""
        chars = sorted(chars, key=lambda c: (c["x0"], c["oy"]))
        # ---- fractions and radicals from rules
        used = set()
        atoms = []  # (x0, x1, baseline, size, latex, kind)
        for r in sorted(self.rules, key=lambda r: r[2] - r[0], reverse=True):
            rx0, ry, rx1 = r
            inside = [c for c in chars if id(c) not in used and rx0 - 1 <= (c["x0"] + c["x1"]) / 2 <= rx1 + 1]
            if not inside:
                continue
            sz = statistics.median([c["size"] for c in inside])
            rad = [c for c in chars if id(c) not in used and c["c"] == "√" and abs(c["x1"] - rx0) < 2.5 and abs(c["y0"] - ry) < 3]
            if rad:
                under = [c for c in inside if c["y0"] >= ry - 1 and c["y0"] - ry < 2.5 * sz]
                if under:
                    for c in under + rad[:1]:
                        used.add(id(c))
                    self.total += 1
                    self.mapped += 1
                    base = statistics.median([c["oy"] for c in under])
                    atoms.append((rad[0]["x0"], rx1, base, sz, "\\sqrt{" + self.build(under, depth + 1) + "}", "atom"))
                continue
            num = [c for c in inside if c["y1"] <= ry + 1.0 and ry - c["y1"] < 1.0 * c["size"]]
            den = [c for c in inside if c["y0"] >= ry - 1.0 and c["y0"] - ry < 1.0 * c["size"]]
            # extend numerator / denominator over glyphs stacked with them (scripts, tall glyphs)
            num = _grow_row(num, [c for c in inside if c["y1"] <= ry + 1.0 and ry - c["y1"] < 2.2 * c["size"]])
            den = _grow_row(den, [c for c in inside if c["y0"] >= ry - 1.0 and c["y0"] - ry < 2.2 * c["size"]])
            if num and den:
                for c in num + den:
                    used.add(id(c))
                self.total += 1
                self.mapped += 1
                atoms.append((rx0, rx1, ry + 0.3 * sz, sz, "\\frac{" + self.build(num, depth + 1) + "}{" + self.build(den, depth + 1) + "}", "atom"))
        rest = [c for c in chars if id(c) not in used]
        # ---- operator names with limits underneath (lim, max, min, sup, inf ...)
        letters = sorted([c for c in rest if c["c"].isalpha()], key=lambda c: c["x0"])
        k = 0
        while k < len(letters):
            run = [letters[k]]
            while k + len(run) < len(letters):
                nx = letters[k + len(run)]
                if nx["font"] == run[-1]["font"] and abs(nx["oy"] - run[-1]["oy"]) < 0.5 and nx["x0"] - run[-1]["x1"] < 0.15 * nx["size"]:
                    run.append(nx)
                else:
                    break
            word = "".join(c["c"] for c in run)
            if word in ("lim", "max", "min", "sup", "inf", "limsup", "liminf", "argmax", "argmin") and all(id(c) not in used for c in run):
                wx0, wx1 = run[0]["x0"], run[-1]["x1"]
                wy1 = max(c["y1"] for c in run)
                sz = run[0]["size"]
                pool = [c for c in rest if id(c) not in used and c not in run and c["size"] < 0.9 * sz and c["y0"] >= wy1 - 0.3 * sz and c["y0"] - wy1 < 1.2 * sz]
                below = _grow_row([c for c in pool if wx0 - 0.5 * sz <= (c["x0"] + c["x1"]) / 2 <= wx1 + 0.5 * sz], pool)
                if below:
                    for c in run + below:
                        used.add(id(c))
                    self.total += len(run)
                    self.mapped += len(run)
                    cmd = "\\operatorname*{" + word + "}" if word.startswith("arg") else "\\" + word
                    atoms.append((wx0, wx1, run[0]["oy"], sz, cmd + "_{" + self.build(below, depth + 1) + "}", "bigop"))
            k += len(run)
        rest = [c for c in chars if id(c) not in used]
        # ---- big operators with limits
        ops = [c for c in rest if c["c"] in BIG_OPS]
        for op in ops:
            if id(op) in used:
                continue
            w = op["x1"] - op["x0"]
            cx0, cx1 = op["x0"] - 0.6 * w, op["x1"] + 0.6 * w
            h = op["y1"] - op["y0"]
            pool_a = [c for c in rest if c is not op and id(c) not in used and c["y1"] <= op["y0"] + 0.25 * h and c["size"] < op["size"] and op["y0"] - c["y1"] < 1.2 * op["size"]]
            pool_b = [c for c in rest if c is not op and id(c) not in used and c["y0"] >= op["y1"] - 0.25 * h and c["size"] < op["size"] and c["y0"] - op["y1"] < 1.2 * op["size"]]
            above = _grow_row([c for c in pool_a if cx0 <= (c["x0"] + c["x1"]) / 2 <= cx1], pool_a)
            below = _grow_row([c for c in pool_b if cx0 <= (c["x0"] + c["x1"]) / 2 <= cx1], pool_b)
            t = self.tok(op)
            used.add(id(op))
            for c in above + below:
                used.add(id(c))
            s = t or ""
            if below:
                s += "_{" + self.build(below, depth + 1) + "}"
            if above:
                s += "^{" + self.build(above, depth + 1) + "}"
            atoms.append((op["x0"], op["x1"], op["oy"], op["size"], s, "bigop"))
        for c in rest:
            if id(c) in used:
                continue
            t = self.tok(c)
            if t is None:
                continue
            atoms.append((c["x0"], c["x1"], c["oy"], c["size"], t, "glyph" if c["c"].isalnum() else "sym"))
        if not atoms:
            return ""
        atoms.sort(key=lambda a: a[0])
        # ---- main baseline = baseline of the largest atoms
        big = max(a[3] for a in atoms if a[5] != "bigop") if any(a[5] != "bigop" for a in atoms) else atoms[0][3]
        mains = [a for a in atoms if a[3] >= 0.85 * big and a[5] != "bigop"] or atoms
        base = statistics.median([a[2] for a in mains])
        s = self._linear(atoms, base, big, depth)
        return _fix_funcs(s) if depth == 0 else s

    def _linear(self, atoms, base, big, depth):
        out = []
        i = 0
        while i < len(atoms):
            a = atoms[i]
            off = a[2] - base
            if a[5] != "bigop" and a[3] < 0.85 * big and abs(off) > 0.12 * big and out:
                # a run of script atoms after a base: split into subscript and superscript parts
                sup, sub = [], []
                last = None
                while i < len(atoms):
                    b = atoms[i]
                    o = b[2] - base
                    if b[5] != "bigop" and b[3] < 0.85 * big and abs(o) > 0.12 * big:
                        last = sup if o < 0 else sub
                        last.append(b)
                        i += 1
                    elif last is sub and sub and b[3] < 0.85 * big and b[4] in ("'", "''", "\\prime") and o > -0.12 * big:
                        sub.append(b)              # g' inside a subscript (lambda_{g'}): the prime sits between levels
                        i += 1
                    else:
                        break
                s_ = ""
                for grp, mark in ((sub, "_"), (sup, "^")):
                    if not grp:
                        continue
                    inner = (self._linear(grp, statistics.median([g[2] for g in grp]), max(g[3] for g in grp), depth + 1)
                             if depth < 8 else "".join(g[4] for g in grp))
                    if mark == "^" and inner and set(inner) <= {"'"}:
                        inner = "\\prime" * len(inner)          # a raised prime: g^{\prime}, not g^{'}
                    s_ += mark + "{" + inner + "}"
                out.append(s_)
                continue
            out.append(a[4])
            i += 1
        s = ""
        for t in out:
            if s and re.match(r"[A-Za-z]", t[:1]) and re.search(r"\\[A-Za-z]+$", s):
                s += " "
            s += t
        return s


def _grow_row(seed, pool):
    """Extend a limit row horizontally over contiguous glyphs (limits are often wider than the
    operator, e.g. u in (R/Ann_R(x_i))^x centred under a sum)."""
    row = list(seed)
    if not row:
        return row
    ids = {id(c) for c in row}
    changed = True
    while changed:
        changed = False
        x0 = min(c["x0"] for c in row)
        x1 = max(c["x1"] for c in row)
        y0 = min(c["y0"] for c in row)
        y1 = max(c["y1"] for c in row)
        for c in pool:
            if id(c) in ids:
                continue
            sz = c["size"]
            vov = min(y1, c["y1"]) - max(y0, c["y0"])
            if vov > -0.2 * sz and (x0 - 0.6 * sz <= c["x1"] and c["x0"] <= x1 + 0.6 * sz):
                row.append(c)
                ids.add(id(c))
                changed = True
    return row


def _fix_funcs(s):
    # upright function names written as plain letters
    def rep(m):
        w = m.group(0)
        return "\\" + w + " " if w in FUNCS else w
    s = re.sub(r"(?<![\\A-Za-z])(arcsin|arccos|arctan|sinh|cosh|tanh|liminf|limsup|sin|cos|tan|cot|sec|csc|log|ln|exp|lim|sup|inf|max|min|det|dim|ker|deg|gcd|arg)(?![A-Za-z])", rep, s)
    s = re.sub(r"(?<![\\A-Za-z])(Ann|Hom|End|Aut|Tr|tr|rank|Spec|Gal|Res|Ext|Tor|Ker|Im|id|sgn|diag|span|supp|Var|Cov)(?![A-Za-z])",
               lambda m: "\\operatorname{" + m.group(0) + "}", s)
    return s.strip()


def chars_to_latex(chars, ctx, bbox=None):
    """Convert a set of glyphs to LaTeX. Returns (latex, coverage)."""
    rules = []
    if ctx is not None and bbox is not None:
        x0, y0, x1, y1 = bbox
        rules = [r for r in ctx.rules if r[0] >= x0 - 3 and r[2] <= x1 + 3 and y0 - 3 <= r[1] <= y1 + 3]
    b = _Builder(ctx, rules)
    latex = b.build(chars)
    cov = b.mapped / b.total if b.total else 0.0
    return latex, cov


# ----------------------------------------------------------------------------------- classification

MATH_SIGNAL = set("−∈∉∋⊂⊃⊆⊇⊊∪∩∞∂∇≤≥≦≧≠≈≡∼≃≅→←↔⇒⇐⇔↦⟶×±∓∑∏∐∫∬∮√∀∃∄∧∨⊗⊕⊙∘′″ℓℏ∅⟨⟩〈〉‖∣⋯⋮⋱⊥∥∝≪≫⊢⌊⌋⌈⌉ℝℕℤℚℂℙℍ⋃⋂⨁⨂∖ℜℑ℘ℵ")


def _mathy(c, base_size=None):
    """Strong evidence that a glyph is mathematics (not ordinary punctuation)."""
    ch = c["c"]
    if is_math_font(c["font"]) and ch not in ".,;:":
        return True
    if ch in GREEK or ch in MATH_SIGNAL:
        return True
    if 0x1D400 <= ord(ch) <= 0x1D7FF:
        return True
    return False


def _word_spans(chars):
    """Split a visual row of glyphs into word groups by horizontal gaps."""
    words, cur = [], []
    for c in chars:
        if cur:
            gap = c["x0"] - cur[-1]["x1"]
            if c.get("sp") or gap > 0.25 * max(c["size"], cur[-1]["size"]):
                words.append(cur)
                cur = []
        cur.append(c)
    if cur:
        words.append(cur)
    return words


def _is_texty_word(w, base):
    letters = [c for c in w if c["c"].isalpha()]
    if len(letters) < 3:
        return False
    if any(is_math_font(c["font"]) for c in letters):
        return False
    word = "".join(c["c"] for c in w if c["c"].isalpha())
    if word in FUNCS or word in ("Ann", "Hom", "Spec", "rank"):
        return False
    return all(c["size"] >= 0.8 * base for c in letters)


def _script(c, base, base_oy):
    return c["size"] < 0.85 * base and abs(c["oy"] - base_oy) > 0.12 * base


def row_to_text(row_chars, base, ctx):
    """Plain text for a visual row, with inline math runs converted to $...$.
    Returns (text, n_math_runs, mapped, total)."""
    if not row_chars:
        return "", 0, 0, 0
    row_chars = sorted(row_chars, key=lambda c: c["x0"])
    mains = [c for c in row_chars if c["size"] >= 0.85 * base] or row_chars
    base_oy = statistics.median([c["oy"] for c in mains])
    words = _word_spans(row_chars)
    flags = []
    for w in words:
        strong = any(_mathy(c, base) or _script(c, base, base_oy) for c in w)
        texty = _is_texty_word(w, base)
        nlet = sum(1 for c in w if c["c"].isalpha())
        weak = (not texty) and all(c["c"].isdigit() or c["c"] in "()[]=+-−/,.'|<>!*:" or (c["c"].isalpha() and nlet <= 3 and ITALIC_FONT.search(c["font"])) for c in w)
        flags.append("strong" if strong and not texty else "weak" if weak else "text")
    # extend math runs over weak neighbours that connect to strong ones
    runs, i = [], 0
    while i < len(words):
        if flags[i] == "strong":
            j = i
            def depth(a, b):
                t = "".join(c["c"] for w in words[a:b + 1] for c in w)
                return t.count("(") + t.count("[") + t.count("{") - t.count(")") - t.count("]") - t.count("}")
            while (j + 1 < len(words) and flags[j + 1] in ("strong", "weak")
                   and (not "".join(c["c"] for c in words[j])[-1:] in ",.;" or depth(i, j) > 0)):
                j += 1
            k = i
            while k - 1 >= 0 and flags[k - 1] == "weak" and not any(r[1] >= k - 1 for r in runs):
                k -= 1
            runs.append((k, j))
            i = j + 1
        else:
            i += 1
    out, mapped, total, n = [], 0, 0, 0
    run_at = {r[0]: r for r in runs}
    i = 0
    while i < len(words):
        if i in run_at:
            k, j = run_at[i]
            glyphs = [c for w in words[k:j + 1] for c in w]
            # trailing sentence punctuation stays outside the math
            trail = ""
            while glyphs and glyphs[-1]["c"] in ".,;:" and not is_math_font(glyphs[-1]["font"]):
                trail = glyphs[-1]["c"] + trail
                glyphs = glyphs[:-1]
            if glyphs:
                b = _Builder(ctx, [])
                latex = b.build(glyphs)
                mapped += b.mapped
                total += b.total
                if latex:
                    out.append("$" + latex + "$" + trail)
                    n += 1
                else:
                    out.append("".join(c["c"] for c in glyphs) + trail)
            elif trail:
                out.append(trail)
            i = j + 1
            continue
        out.append("".join(c["c"] for c in words[i]))
        i += 1
    return " ".join(out), n, mapped, total


def visual_rows(chars, base):
    """Group glyphs into visual text rows: rows are anchored on full-size glyph baselines and
    small glyphs (scripts) attach to the nearest row."""
    mains = [c for c in chars if c["size"] >= 0.85 * base]
    if not mains:
        mains = chars
    anchors = _cluster_baselines(mains, 0.35 * base)
    rows = {a: [] for a in anchors}
    for c in chars:
        a = min(anchors, key=lambda a: abs(((c["y0"] + c["y1"]) / 2) - (a - 0.3 * base)))
        rows[a].append(c)
    return [rows[a] for a in sorted(rows) if rows[a]]


EQNUM = re.compile(r"^\(\s*[\dA-Za-z.]{1,8}\s*\)$")


def _block_base(chars):
    al = [c["size"] for c in chars if c["c"].isalnum() and c["c"] not in BIG_OPS] or [c["size"] for c in chars]
    top = max(al)
    big = [x for x in al if x >= 0.8 * top]
    return statistics.median(big)


def block_kind(block, ctx):
    """'display' (mostly mathematics), 'script' (a small-glyph fragment such as limits split from a
    display equation), 'prose' (text, possibly with inline math) or None (plain text)."""
    chars = _chars_of(block["lines"])
    block["_chars"] = chars
    if not chars:
        return None
    body = ctx.body
    txt = "".join(c["c"] for c in chars)
    if ctx.math_page and EQNUM.match(txt):
        return "eqnum"
    if ctx.math_page and txt in FUNCS:
        return "script"
    if all(c["size"] < 0.85 * body for c in chars) and len(chars) <= 24 and any(
            _mathy(c) or c["c"].isalnum() or c["c"] in "=+-−,()" for c in chars):
        return "script" if (ctx.math_page or any(_mathy(c) for c in chars)) else None
    strong = sum(1 for c in chars if _mathy(c))
    if not strong:
        # equations typeset with ordinary fonts (Word, Pages): only italic single-letter variables,
        # digits, operators and parentheses, with at least one relation sign
        ok = all((c["c"].isalpha() and ITALIC_FONT.search(c["font"])) or c["c"].isdigit() or c["c"] in "=+-−()[]/^.,'<>"
                 or c["size"] < 0.85 * body for c in chars)
        words_ok = all(len([c for c in w if c["c"].isalpha()]) <= 2 for w in _word_spans(sorted(chars, key=lambda c: c["x0"])))
        if ctx.math_page and ok and words_ok and any(c["c"] in "=<>" for c in chars) and len(chars) >= 3:
            return "display"
        if ctx.math_page and ok and words_ok and len(chars) <= 12:
            return "script"
        return None
    base = _block_base(chars)
    letters = 0
    for r in visual_rows(chars, base):
        for w in _word_spans(sorted(r, key=lambda c: c["x0"])):
            if _is_texty_word(w, base):
                letters += sum(1 for c in w if c["c"].isalpha())
    if letters <= 0.25 * len(chars) and len(block["lines"]) <= 14 and (len(chars) >= 2 or is_math_font(chars[0]["font"])):
        return "display"
    return "prose"


def absorb_inline_fragments(blocks):
    """PDF producers often store pieces of an inline formula (scripts, operators) as separate
    blocks overlapping a text line. Fold those back into the text block they sit in."""
    texts = [b for b in blocks if b.get("_mk") in ("prose", None)]
    out = []
    for b in blocks:
        if b.get("_mk") in ("display", "script") and texts:
            x0, y0, x1, y1 = b["bbox"]
            host = None
            for t in texts:
                tx0, ty0, tx1, ty1 = t["bbox"]
                inside_x = x0 >= tx0 - 2 and x1 <= tx1 + 2
                rows = [l for l in t["lines"] if min(l["bbox"][3], y1) - max(l["bbox"][1], y0) > 0.3 * (y1 - y0)]
                if inside_x and rows and ty0 - 2 <= y0 and y1 <= ty1 + 2:
                    host = t
                    break
            if host:
                host["lines"] = host["lines"] + b["lines"]
                host["_chars"] = (host.get("_chars") or []) + (b.get("_chars") or [])
                host["_mk"] = "prose"
                host["text"] = host["text"] + "\n" + b["text"]
                continue
        out.append(b)
    return out


def group_display(blocks, ctx):
    """Merge display blocks with adjacent display / script fragments (limits, scripts, numerators
    that the PDF stored as separate blocks). Returns list of block groups."""
    body = ctx.body
    cand = [b for b in blocks if b.get("_mk") in ("display", "script", "eqnum")]
    groups = []
    for b in sorted(cand, key=lambda b: (b["bbox"][1], b["bbox"][0])):
        x0, y0, x1, y1 = b["bbox"]
        hit = None
        for g in groups:
            gx0, gy0, gx1, gy1 = g["bbox"]
            vgap = max(y0 - gy1, gy0 - y1)
            hov = min(x1, gx1) - max(x0, gx0)
            same_row = min(y1, gy1) - max(y0, gy0) > 0.5 * min(y1 - y0, gy1 - gy0)
            # two complete displays need to touch to be one equation; small fragments may float
            both_display = b.get("_mk") == "display" and any(x.get("_mk") == "display" for x in g["blocks"])
            vlim = 0.25 * body if both_display else 0.9 * body
            if (vgap <= vlim and hov > -0.5 * body) or (same_row and (hov > -2.5 * body or b.get("_mk") == "eqnum")):
                hit = g
                break
        if hit:
            hit["blocks"].append(b)
            hit["bbox"] = (min(hit["bbox"][0], x0), min(hit["bbox"][1], y0), max(hit["bbox"][2], x1), max(hit["bbox"][3], y1))
        else:
            groups.append(dict(blocks=[b], bbox=b["bbox"]))
    # fragments of one equation that sit side by side on the same row (big delimiters, limits)
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i]["bbox"], groups[j]["bbox"]
                same_row = min(a[3], b[3]) - max(a[1], b[1]) > 0.5 * min(a[3] - a[1], b[3] - b[1])
                hov = min(a[2], b[2]) - max(a[0], b[0])
                if same_row and hov > -2.5 * body:
                    groups[i]["blocks"] += groups[j]["blocks"]
                    groups[i]["bbox"] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    del groups[j]
                    changed = True
                    break
            if changed:
                break
    # a group must contain at least one real display block
    return [g for g in groups if any(b.get("_mk") == "display" for b in g["blocks"])]


def _vpos(c, base):
    """Baseline-equivalent vertical position. Big operators and delimiters from extension fonts
    hang from their origin, so their centre (the math axis) is used instead."""
    if c["c"] in BIG_OPS or c["c"] in "()[]{}√⟨⟩|‖" and (c["y1"] - c["y0"]) > 1.4 * base:
        return (c["y0"] + c["y1"]) / 2 + 0.25 * base
    return c["oy"]


def display_rows(chars, rules, base):
    """Split a display region into separate equation rows. Fraction members (glyphs just above or
    below a rule) and big-operator limits travel with the row that owns the rule / operator.
    Returns [(chars, rules)] - a single entry when the region is one equation."""
    rules = sorted(rules, key=lambda r: r[0] - r[2])  # longest first: outer fractions claim first
    member, cand = {}, {}
    for k, r in enumerate(rules):
        cand[k] = [c for c in chars if r[0] - 1 <= (c["x0"] + c["x1"]) / 2 <= r[2] + 1 and (
            (0 <= r[1] - c["y1"] + 1 < 1.0 * c["size"] and r[1] - c["y0"] < 2.2 * c["size"]) or
            (0 <= c["y0"] - r[1] + 1 < 1.0 * c["size"] and c["y1"] - r[1] < 2.2 * c["size"]))]
        for c in cand[k]:
            member.setdefault(id(c), k)
    ops = [c for c in chars if c["c"] in BIG_OPS]
    for o in ops:
        w, h = o["x1"] - o["x0"], o["y1"] - o["y0"]
        for vert in ("above", "below"):
            if vert == "above":
                pool = [c for c in chars if c is not o and c["size"] < o["size"] and 0 <= o["y0"] - c["y1"] + 0.25 * h < 1.2 * o["size"]]
            else:
                pool = [c for c in chars if c is not o and c["size"] < o["size"] and 0 <= c["y0"] - o["y1"] + 0.25 * h < 1.2 * o["size"]]
            seed = [c for c in pool if o["x0"] - 0.6 * w <= (c["x0"] + c["x1"]) / 2 <= o["x1"] + 0.6 * w]
            for c in _grow_row(seed, pool):
                member.setdefault(id(c), ("op", id(o)))
    free = [c for c in chars if id(c) not in member and c["size"] >= 0.85 * base]
    if not free:
        return [(chars, rules)]
    anchors = sorted(_cluster_baselines([dict(c, oy=_vpos(c, base)) for c in free], 0.5 * base))
    merged = [anchors[0]]
    for a in anchors[1:]:
        if a - merged[-1] < 1.6 * base:
            continue
        merged.append(a)
    anchors = merged
    for r in rules:
        axis = r[1] + 0.3 * base
        if min(abs(a - axis) for a in anchors) > 1.2 * base:
            anchors.append(axis)
    anchors.sort()
    if len(anchors) <= 1:
        return [(chars, rules)]

    def nearest(y):
        return min(range(len(anchors)), key=lambda i: abs(anchors[i] - y))
    rule_row = {}
    for k, r in enumerate(rules):
        # a short rule whose glyphs already belong to a longer rule (radical overline inside a
        # numerator) lives in the same row as that longer rule
        owners = [member.get(id(c)) for c in cand[k] if isinstance(member.get(id(c)), int) and member.get(id(c)) != k]
        if owners and len(owners) >= 0.5 * len(cand[k]):
            rule_row[k] = rule_row.get(max(set(owners), key=owners.count), nearest(r[1] + 0.3 * base))
        else:
            rule_row[k] = nearest(r[1] + 0.3 * base)
    op_row = {id(o): nearest(_vpos(o, base)) for o in ops}
    rows = [([], []) for _ in anchors]
    for k, r in enumerate(rules):
        rows[rule_row[k]][1].append(r)
    for c in chars:
        m = member.get(id(c))
        if m is None:
            i = nearest(_vpos(c, base) if c["size"] >= 0.85 * base else (c["y0"] + c["y1"]) / 2 + 0.3 * base)
        elif isinstance(m, tuple):
            i = op_row[m[1]]
        else:
            i = rule_row[m]
        rows[i][0].append(c)
    return [r for r in rows if r[0]]


def display_from_chars(chars, ctx):
    """LaTeX for a display region. Returns dict(latex, coverage, number) or None."""
    if not chars:
        return None
    base = _block_base(chars)
    rows = visual_rows(chars, base)
    number = None
    body_chars = chars
    for r in rows:
        words = _word_spans(sorted(r, key=lambda c: c["x0"]))
        if len(words) > 1:
            t = "".join(c["c"] for c in words[-1])
            if EQNUM.match(t):
                number = t
                ids = {id(c) for c in words[-1]}
                body_chars = [c for c in body_chars if id(c) not in ids]
    if not body_chars:
        return None
    x0 = min(c["x0"] for c in body_chars)
    y0 = min(c["y0"] for c in body_chars)
    x1 = max(c["x1"] for c in body_chars)
    y1 = max(c["y1"] for c in body_chars)
    rules = [r for r in (ctx.rules if ctx is not None else []) if x0 - 3 <= r[0] and r[2] <= x1 + 3 and y0 - 2 <= r[1] <= y1 + 2]
    rows = display_rows(body_chars, rules, base)
    if len(rows) > 1:
        built = []
        for rc, rr in rows:
            b = _Builder(ctx, rr)
            s_ = b.build(rc)
            if s_:
                bb = (min(c["x0"] for c in rc), min(c["y0"] for c in rc), max(c["x1"] for c in rc), max(c["y1"] for c in rc))
                built.append(dict(latex=s_, mapped=b.mapped, total=b.total, bbox=bb))
        if not built:
            return None
        # continuation lines of one derivation start with a relation; otherwise rows are separate equations
        cont = any(re.match(r"^(=|<|>|\\leq|\\geq|\\approx|\\equiv|\\to|\\sim|\\le|\\ge)", r["latex"]) for r in built[1:])
        if cont:
            m, t = sum(r["mapped"] for r in built), sum(r["total"] for r in built)
            latex = "\\begin{aligned}" + " \\\\ ".join(r["latex"] for r in built) + "\\end{aligned}"
            cov = m / t if t else 0.0
            return dict(latex=latex, coverage=round(cov, 3), number=number, bbox=(x0, y0, x1, y1)) if cov >= 0.5 else None
        out = [dict(latex=r["latex"], coverage=round(r["mapped"] / r["total"], 3) if r["total"] else 0.0, number=None, bbox=r["bbox"])
               for r in built if r["total"] and r["mapped"] / r["total"] >= 0.5]
        if out:
            out[-1]["number"] = number
        return out or None
    main_rows = visual_rows(body_chars, base)
    rows_full = [r for r in main_rows if any(c["size"] >= 0.85 * base for c in r)]
    has_rules = bool(rules)
    has_big = any(c["c"] in BIG_OPS for c in body_chars)
    if len(rows_full) > 1 and not has_rules and not has_big:
        parts, m, t = [], 0, 0
        for r in main_rows:
            b = _Builder(ctx, [])
            s = b.build(r)
            m, t = m + b.mapped, t + b.total
            if s:
                parts.append(s)
        latex = "\\begin{aligned}" + " \\\\ ".join(parts) + "\\end{aligned}" if len(parts) > 1 else (parts[0] if parts else "")
        cov = m / t if t else 0.0
    else:
        latex, cov = chars_to_latex(body_chars, ctx, (x0, y0, x1, y1))
    if not latex or cov < 0.5:
        return None
    return dict(latex=latex, coverage=round(cov, 3), number=number, bbox=(x0, y0, x1, y1))


def mark_inline(block, ctx):
    """Prose block: rebuild rows with inline math runs wrapped in $...$ (sets block['line_texts'])."""
    chars = block.get("_chars") or _chars_of(block["lines"])
    if not chars:
        return
    base = _block_base(chars)
    texts, nm, mapped, total = [], 0, 0, 0
    for r in visual_rows(chars, base):
        t, n, m, tt = row_to_text(r, base, ctx)
        texts.append(t)
        nm, mapped, total = nm + n, mapped + m, total + tt
    if nm:
        block["line_texts"] = texts
        block["has_math"] = True
        block["math_coverage"] = round(mapped / total, 3) if total else None


def page_is_mathematical(lines_):
    n = 0
    for l in lines_:
        for s in l["spans"]:
            if is_math_font(s["font"]):
                n += len(s["text"].strip())
            else:
                n += sum(1 for ch in s["text"] if ch in MATH_SIGNAL or 0x1D400 <= ord(ch) <= 0x1D7FF)
    return n >= 3
