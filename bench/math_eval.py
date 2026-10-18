"""Formula-recognition evaluation, separate from general OCR.

Builds a fixed set of equations with pdflatex (Computer Modern, the arXiv default), runs the
ParseFlow pipeline on the PDF, and checks every equation with the OFFICIAL olmOCR-Bench MathTest
comparator (KaTeX rendering + symbol-layout matching). The equations were written for this
evaluation and are not taken from the benchmark. Synthetic, cleanly typeset input: results are an
upper bound for real arXiv pages, not a substitute for the benchmark.

    python bench/math_eval.py [--out bench_out/math_eval]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

CASES = [
    ("fraction", r"\frac{a+b}{c-d}"),
    ("fraction", r"\frac{1}{2}+\frac{3}{4}=\frac{5}{4}"),
    ("fraction", r"x=\frac{-b\pm\sqrt{b^{2}-4ac}}{2a}"),
    ("scripts", r"x_{i}^{2}+y_{j}^{3}"),
    ("scripts", r"a_{n+1}=a_{n}+d"),
    ("scripts", r"e^{-x^{2}}"),
    ("greek", r"\alpha+\beta=\gamma"),
    ("greek", r"\Gamma(\alpha)\Delta\Theta"),
    ("greek", r"\sigma^{2}=\mathbb{E}[(X-\mu)^{2}]"),
    ("integral", r"\int_{0}^{1}x^{2}\,dx=\frac{1}{3}"),
    ("integral", r"\int_{a}^{b}f(x)\,dx=F(b)-F(a)"),
    ("integral", r"\oint_{C}\mathbf{F}\cdot d\mathbf{r}"),
    ("summation", r"\sum_{k=1}^{n}k=\frac{n(n+1)}{2}"),
    ("summation", r"\sum_{i=0}^{\infty}x^{i}=\frac{1}{1-x}"),
    ("summation", r"\prod_{p}\frac{1}{1-p^{-s}}"),
    ("limit", r"\lim_{n\to\infty}\left(1+\frac{1}{n}\right)^{n}=e"),
    ("relation", r"A\subseteq B\cup C"),
    ("relation", r"\forall\epsilon>0\,\exists\delta>0"),
    ("matrix", r"\begin{pmatrix}1&2\\3&4\end{pmatrix}"),
    ("matrix", r"\det\begin{bmatrix}a&b\\c&d\end{bmatrix}=ad-bc"),
    ("multiline", r"f(x)=(x+1)^{2}"),
    ("multiline", r"=x^{2}+2x+1"),
    ("sqrt", r"\sqrt{x^{2}+y^{2}}"),
    ("sqrt", r"\sqrt[3]{8}=2"),
]


def build_pdf(out):
    lines = [r"\documentclass{article}", r"\usepackage{amsmath,amssymb}", r"\pagestyle{empty}", r"\begin{document}"]
    lines.append(r"Inline formulas such as $x_{i}^{2}+y_{j}^{3}$ and $\alpha+\beta=\gamma$ appear inside running text, "
                 r"together with $a_{n+1}=a_{n}+d$ and $e^{-x^{2}}$.")
    for cat, tex in CASES:
        if cat == "multiline":
            continue
        lines.append(r"\[ " + tex + r" \]")
    lines.append(r"\begin{align} f(x) &= (x+1)^{2} \\ &= x^{2}+2x+1 \end{align}")
    lines.append(r"\end{document}")
    tex = out / "equations.tex"
    tex.write_text("\n".join(lines))
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "-output-directory", str(out), str(tex)],
                   capture_output=True, timeout=120)
    return out / "equations.pdf"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "bench_out" / "math_eval"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if not shutil.which("pdflatex"):
        print("pdflatex not available - cannot build the fixture", file=sys.stderr)
        return 2
    pdf = build_pdf(out)
    from processing import pipeline, markdown
    from olmocr.bench.tests import MathTest
    doc = pipeline.process_file(str(pdf))
    md = markdown.render(doc)
    (out / "equations.md").write_text(md)
    res, by = [], {}
    for i, (cat, tex) in enumerate(CASES):
        t = MathTest(pdf="equations.pdf", page=1, id=f"m{i}", type="math", math=tex)
        ok, why = t.run(md)
        res.append(dict(category=cat, latex=tex, passed=ok))
        by.setdefault(cat, []).append(ok)
    summary = dict(total=len(res), passed=sum(r["passed"] for r in res),
                   by_category={k: f"{sum(v)}/{len(v)}" for k, v in by.items()}, cases=res)
    (out / "math_eval.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in ("total", "passed", "by_category")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
