"""Formula reconstruction from the PDF text layer (pdflatex fixtures; skipped without pdflatex)."""
import re

import pytest

from conftest import image_with_text, latex_pdf, needs_tesseract, scanned_pdf
from processing import mathtext, pipeline


def eqs(doc):
    return [b["latex"] for b in doc["blocks"] if b["type"] == "equation"]


def norm(s):
    return re.sub(r"\s+|\\[,;!]", "", s)


@pytest.fixture(scope="module")
def mathdoc(tmp_path_factory):
    d = tmp_path_factory.mktemp("math")
    body = r"""
Inline math such as $x_i^2 + \alpha \leq \beta$ appears inside this paragraph of ordinary text.

\[ \frac{a+b}{c-d} = \sqrt{x^2+y^2} \]

\vspace{1em}
\[ \int_0^1 x^2 \, dx = \frac{1}{3} \]

\vspace{1em}
\[ \sum_{k=1}^{n} k = \frac{n(n+1)}{2} \]

\vspace{1em}
\[ \Gamma(z) = \int_{0}^{\infty} t^{z-1} e^{-t}\, dt \]

\vspace{1em}
\begin{align} f(x) &= (x+1)^2 \\ &= x^2 + 2x + 1 \end{align}

\vspace{1em}
\[ A = \begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix} \]
"""
    return pipeline.process_file(latex_pdf(d, body))


def test_inline_math_in_paragraph(mathdoc):
    para = [b for b in mathdoc["blocks"] if b["type"] == "paragraph" and "ordinary text" in (b["content"] or "")]
    assert para and "$x_{i}^{2}+\\alpha\\leq\\beta$" in para[0]["content"]
    assert para[0].get("contains_math")


def test_fraction_and_radical(mathdoc):
    assert any(norm(e) == norm(r"\frac{a+b}{c-d}=\sqrt{x^{2}+y^{2}}") for e in eqs(mathdoc))


def test_integral_with_limits(mathdoc):
    assert any(r"\int_{0}^{1}x^{2}" in norm(e) and r"\frac{1}{3}" in e for e in eqs(mathdoc))


def test_summation_with_limits(mathdoc):
    assert any(norm(e) == norm(r"\sum_{k=1}^{n}k=\frac{n(n+1)}{2}") for e in eqs(mathdoc))


def test_greek_scripts(mathdoc):
    assert any(norm(e) == norm(r"\Gamma(z)=\int_{0}^{\infty}t^{z-1}e^{-t}dt") for e in eqs(mathdoc))


def test_multiline_derivation_is_aligned(mathdoc):
    al = [e for e in eqs(mathdoc) if e.startswith("\\begin{aligned}")]
    assert any("(x+1)^{2}" in e and "=x^{2}+2x+1" in e for e in al)


@pytest.mark.xfail(reason="matrices/arrays are not reconstructed from the text layer yet (documented limitation)", strict=True)
def test_matrix(mathdoc):
    assert any("pmatrix" in e or "array" in e for e in eqs(mathdoc))


def test_equation_blocks_carry_provenance_and_are_not_auto_verified(mathdoc):
    for b in mathdoc["blocks"]:
        if b["type"] == "equation":
            assert b["extractor"] == "text_layer_math" and b["page"] == 1 and len(b["bbox"]) == 4
            assert b["verification"] == "review"  # valid LaTeX is not proof of a correct reading
            assert 0 < b["glyph_coverage"] <= 1


def test_markdown_uses_display_delimiters(mathdoc):
    assert "$$\n\\frac{a+b}{c-d}" in mathdoc["_markdown"]


def test_unmapped_glyphs_are_not_guessed():
    assert mathtext.glyph_latex("�") is None
    assert mathtext.glyph_latex("") is None
    assert mathtext.glyph_latex("α") == "\\alpha" and mathtext.glyph_latex("∆") == "\\Delta"
    assert mathtext.glyph_latex("R", "MSBM10") == "\\mathbb{R}"


@needs_tesseract()
def test_scanned_equation_is_not_fabricated(tmp_path):
    # an equation in a raster image: no offline formula model -> no LaTeX may be invented
    img = image_with_text(["E = mc^2   and   a/b + c"], size=(1400, 500), origin=(60, 200))
    out = pipeline.process_file(scanned_pdf(tmp_path / "eq.pdf", img))
    assert not [b for b in out["blocks"] if b["type"] == "equation"]
