import pymupdf

from conftest import para_lines, text_pdf
from processing import headers, layout, markdown, pipeline
from processing.textnorm import Vocabulary, join_lines

LEFT = ("Alpha column text begins here and keeps going with ordinary sentences about soil and water. " * 3).strip()
RIGHT = ("Beta column text starts on the right side and talks about rivers and bridges in detail. " * 3).strip()


def two_column_pdf(path):
    items = [(72, 60, "A Study of Two Columns", 18)]
    for i, t in enumerate(para_lines(LEFT, 45)):
        items.append((72, 110 + 14 * i, t, 10))
    for i, t in enumerate(para_lines(RIGHT, 45)):
        items.append((330, 110 + 14 * i, t, 10))
    return text_pdf(path, [items])


def test_multi_column_reading_order(tmp_path):
    out = pipeline.process_file(two_column_pdf(tmp_path / "cols.pdf"))
    md = out["_markdown"]
    assert md.index("A Study of Two Columns") < md.index("Alpha column") < md.index("Beta column")
    # the left column is read completely before the right column starts
    assert md.index("soil and water. Alpha") < md.index("Beta column") or md.count("Alpha column") == 3
    assert out["pages"][0]["flags"].get("multi_column")


def test_xy_cut_grid_reads_rows_first():
    # 2x2 grid of cards with a wide gap between the rows
    items = [dict(bbox=(0, 0, 100, 40), t="A"), dict(bbox=(200, 0, 300, 40), t="B"),
             dict(bbox=(0, 140, 100, 180), t="C"), dict(bbox=(200, 140, 300, 180), t="D")]
    order, _ = layout.reading_order(items, line_height=10)
    assert [i["t"] for i in order] == ["A", "B", "C", "D"]


def test_xy_cut_columns():
    items = [dict(bbox=(0, 0, 100, 10), t="L1"), dict(bbox=(0, 12, 100, 22), t="L2"),
             dict(bbox=(150, 0, 250, 10), t="R1"), dict(bbox=(150, 12, 250, 22), t="R2")]
    order, cols = layout.reading_order(items, line_height=10)
    assert [i["t"] for i in order] == ["L1", "L2", "R1", "R2"] and cols


def repeated_header_pdf(path, n=6):
    pages = []
    for i in range(n):
        items = [(72, 30, "Journal of Synthetic Testing - Vol. 12", 8), (300, 770, str(i + 1), 9)]
        for k, t in enumerate(para_lines(f"Body paragraph on page {i + 1}. " + "Real content continues here. " * 6)):
            items.append((72, 120 + 14 * k, t, 11))
        pages.append(items)
    return text_pdf(path, pages)


def test_repeated_headers_and_page_numbers_removed(tmp_path):
    out = pipeline.process_file(repeated_header_pdf(tmp_path / "h.pdf"))
    md = out["_markdown"]
    assert "Journal of Synthetic Testing" not in md
    assert "Body paragraph on page 3" in md
    roles = {b.get("role") for b in out["blocks"]}
    assert {"page_header", "page_number"} <= roles
    # excluded blocks are kept in the JSON with evidence
    hdr = [b for b in out["blocks"] if b.get("role") == "page_header"]
    assert hdr and all(b["in_markdown"] is False and b.get("role_evidence") for b in hdr)
    # the last characters of the page Markdown must not be a page number
    page_md = markdown.render(dict(out, blocks=[b for b in out["blocks"] if b["page"] == 2], pages=[out["pages"][1]]))
    assert not page_md.strip().endswith("2")


def test_header_suppression_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("STRIP_HEADERS_FOOTERS", "0")
    out = pipeline.process_file(repeated_header_pdf(tmp_path / "h.pdf"))
    assert "Journal of Synthetic Testing" in out["_markdown"]


def test_legitimate_top_of_page_content_kept(tmp_path):
    items = [(72, 50, "Introduction to Soil Science", 20)]
    for k, t in enumerate(para_lines("Soil is a mixture of minerals, organic matter, gases and water. " * 5)):
        items.append((72, 90 + 14 * k, t, 11))
    items.append((72, 740, "1 This footnote explains the sampling method used for every site in the survey.", 8))
    out = pipeline.process_file(text_pdf(tmp_path / "t.pdf", [items]))
    md = out["_markdown"]
    assert "Introduction to Soil Science" in md
    assert "This footnote explains the sampling method" in md


def test_single_page_header_classifier_units():
    blocks = [dict(bbox=(250, 20, 360, 30), text="Proceedings of the Workshop, pp. 1-9", size=8, n_lines=1),
              dict(bbox=(72, 80, 540, 300), text="Body " * 80, size=11, n_lines=15),
              dict(bbox=(300, 760, 310, 770), text="7", size=9, n_lines=1)]
    headers.classify_page(blocks, 612, 792, body_size=11, line_h=13)
    assert [b["role"] for b in blocks] == ["page_header", "body", "page_number"]


def test_dehyphenation_rules():
    v = Vocabulary()
    decisions = []
    assert join_lines(["the docu-", "ment was signed"], v, decisions) == "the document was signed"
    assert join_lines(["a self-", "consistent model"], v) == "a self-consistent model"
    v.add_text("we used state-of-the-art tools and a well-known method")
    assert join_lines(["a well-", "known result"], v) == "a well-known result"
    assert join_lines(["Smith-", "Jones agreed"], v) == "Smith-Jones agreed"
    assert decisions and decisions[0][2] == "join"


def test_duplicate_blocks_not_repeated_in_markdown():
    para = "This identical paragraph is long enough to be considered for duplicate detection."
    doc = dict(pages=[dict(n=1)], blocks=[
        dict(block_id="a", type="paragraph", content=para, page=1, reading_order=1, bbox=[0, 0, 1, 1]),
        dict(block_id="b", type="paragraph", content=para, page=1, reading_order=2, bbox=[0, 2, 1, 3])])
    md = markdown.render(doc)
    assert md.count("identical paragraph") == 1
    assert doc["blocks"][1]["in_markdown"] is False


def test_typographic_double_single_quotes():
    from processing.textnorm import normalize_ligatures
    assert normalize_ligatures("The term \u2018\u2018quoted\u2019\u2019 \ufb01ne") == "The term \u201cquoted\u201d fine"
