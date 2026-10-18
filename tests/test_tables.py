import pymupdf

from conftest import para_lines, text_pdf
from processing import pipeline
from processing import tables as T


def ruled_table_pdf(path, rows, merged_header=False):
    doc = pymupdf.open()
    p = doc.new_page()
    x0, y0, cw, rh = 72, 100, 120, 22
    ncol = len(rows[0])
    for r, row in enumerate(rows):
        for c, txt in enumerate(row):
            if txt:
                p.insert_text((x0 + c * cw + 4, y0 + r * rh + 15), txt, fontsize=10)
    for r in range(len(rows) + 1):
        if merged_header and r == 1:
            p.draw_line((x0, y0 + rh), (x0 + ncol * cw, y0 + rh))
        p.draw_line((x0, y0 + r * rh), (x0 + ncol * cw, y0 + r * rh))
    for c in range(ncol + 1):
        if merged_header and 0 < c < ncol:
            p.draw_line((x0 + c * cw, y0 + rh), (x0 + c * cw, y0 + len(rows) * rh))  # header spans all columns
        else:
            p.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + len(rows) * rh))
    doc.save(str(path))
    return str(path)


def test_ruled_table_rows_and_markdown(tmp_path):
    rows = [["Region", "FY23", "FY24"], ["Americas", "18.0", "19.1"], ["Europe", "11.2", "11.2"], ["APAC", "14.4", "17.9"]]
    out = pipeline.process_file(ruled_table_pdf(tmp_path / "t.pdf", rows))
    t = [b for b in out["blocks"] if b["type"] == "table"]
    assert t and t[0]["rows"][0] == ["Region", "FY23", "FY24"] and ["APAC", "14.4", "17.9"] in t[0]["rows"]
    assert "| Americas | 18.0 | 19.1 |" in out["_markdown"]
    assert t[0]["verification"] == "verified" and "text layer" in t[0]["evidence"]


def test_merged_header_uses_html_colspan(tmp_path):
    rows = [["Quarterly results", "", ""], ["Region", "Q1", "Q2"], ["North", "5", "7"]]
    out = pipeline.process_file(ruled_table_pdf(tmp_path / "m.pdf", rows, merged_header=True))
    md = out["_markdown"]
    assert "<table>" in md and 'colspan="3"' in md and "Quarterly results" in md


def test_borderless_table_detected(tmp_path):
    items = [(72, 60, "Results", 14)]
    data = [("Model", "Accuracy", "Latency", "Params"), ("Base", "71.2", "12 ms", "110M"), ("Large", "78.9", "35 ms", "340M"),
            ("XL", "81.4", "80 ms", "1.5B"), ("Tiny", "64.0", "4 ms", "14M")]
    for r, row in enumerate(data):
        for c, t in enumerate(row):
            items.append((72 + c * 110, 100 + r * 16, t, 10))
    for k, t in enumerate(para_lines("The table above compares four model sizes on the same evaluation set. " * 3)):
        items.append((72, 220 + 14 * k, t, 11))
    out = pipeline.process_file(text_pdf(tmp_path / "b.pdf", [items]))
    t = [b for b in out["blocks"] if b["type"] == "table"]
    assert t and t[0]["extractor"] == "borderless_table"
    assert ["Large", "78.9", "35 ms", "340M"] in t[0]["rows"]
    assert "compares four model sizes" in out["_markdown"]


def test_prose_is_not_turned_into_a_table(tmp_path):
    items = []
    left = para_lines("Plain prose in the left column with many ordinary words in every line. " * 4, 45)
    right = para_lines("More plain prose in the right column, also written in full sentences. " * 4, 45)
    for i, t in enumerate(left):
        items.append((72, 100 + 14 * i, t, 10))
    for i, t in enumerate(right):
        items.append((330, 100 + 14 * i, t, 10))
    out = pipeline.process_file(text_pdf(tmp_path / "p.pdf", [items]))
    assert not [b for b in out["blocks"] if b["type"] == "table"]


def test_pipe_escaping_empty_and_multiline_cells():
    md = T.to_markdown([["Name", "Value"], ["a|b", ""], ["two\nlines", "x"]])
    assert "a\\|b" in md
    assert "| two<br>lines | x |" in md
    assert md.splitlines()[2] == "| a\\|b |  |"


def test_html_escaping_and_spans():
    tbl = dict(rows=[["<b>&", "x"]], cells=[dict(r=0, c=0, rs=2, cs=1, text="<b>&"), dict(r=0, c=1, rs=1, cs=1, text="x")], n_rows=2)
    h = T.render(tbl)
    assert "&lt;b&gt;&amp;" in h and 'rowspan="2"' in h


def test_spans_from_grid():
    grid = [[(0, 0, 200, 10), None], [(0, 10, 100, 20), (100, 10, 200, 20)]]
    cells = T.spans_from_grid(grid, [0, 100, 200], [0, 10, 20], [["H", ""], ["a", "b"]])
    assert dict(r=0, c=0, rs=1, cs=2, text="H") in cells


def _grid(rows, spans=()):
    cells = [dict(r=i, c=j, rs=1, cs=1, text=x) for i, r in enumerate(rows) for j, x in enumerate(r)]
    for (r, c) in spans:
        cells = [x for x in cells if not (x["r"] == r and x["c"] in (c, c + 1))] + [dict(r=r, c=c, rs=1, cs=2, text=rows[r][c])]
    return dict(rows=[list(r) for r in rows], cells=cells, n_cols=len(rows[0]), col_x=list(range(0, 10 * (len(rows[0]) + 1), 10)), evidence="test")


def test_currency_sign_columns_are_folded_into_values():
    t = _grid([["Cost", "$", "178", "$", "141"],
               ["R&D", "3,423", "", "2,532", ""],
               ["Total", "$", "4,737", "$", "3,549"]], spans=[(1, 1), (1, 3)])
    assert T.merge_currency_columns(t) == 2
    assert t["rows"] == [["Cost", "$178", "$141"], ["R&D", "3,423", "2,532"], ["Total", "$4,737", "$3,549"]]
    assert t["n_cols"] == 3 and len(t["col_x"]) == 4 and "currency" in t["evidence"]


def test_currency_fold_refuses_real_columns_and_other_spans():
    # the left column has text alongside a value on the right: two real columns, not a split
    t = _grid([["Item", "$", "10"], ["Fee", "waived", "0"]])
    assert T.merge_currency_columns(t) == 0 and t["rows"][1] == ["Fee", "waived", "0"]
    # a multi-row span is not the sign/value split pattern
    t = _grid([["A", "$", "1"], ["B", "$", "2"]])
    t["cells"].append(dict(r=0, c=0, rs=2, cs=1, text="A"))
    assert T.merge_currency_columns(t) == 0
    # no currency sign at all
    t = _grid([["A", "1", ""], ["B", "", "2"]])
    assert T.merge_currency_columns(t) == 0


def test_header_found_above_a_units_line(tmp_path):
    doc = pymupdf.open()
    p = doc.new_page()
    x0, cw, rh = 72, 120, 20
    p.insert_text((x0 + cw + 4, 95), "FY2024", fontsize=10)
    p.insert_text((x0 + 2 * cw + 4, 95), "FY2023", fontsize=10)
    p.insert_text((x0 + 2 * cw + 4, 109), "(in millions)", fontsize=9)
    body = [["Revenue", "120", "100"], ["Costs", "80", "70"], ["Profit", "40", "30"]]
    y0 = 114
    for r, row in enumerate(body):
        for c, txt in enumerate(row):
            p.insert_text((x0 + c * cw + 4, y0 + r * rh + 14), txt, fontsize=10)
    for r in range(len(body) + 1):
        p.draw_line((x0, y0 + r * rh), (x0 + 3 * cw, y0 + r * rh))
    for c in range(4):
        p.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + len(body) * rh))
    path = tmp_path / "units.pdf"
    doc.save(str(path))
    out = pipeline.process_file(str(path))
    tb = [b for b in out["blocks"] if b["type"] == "table"]
    assert tb and tb[0]["rows"][0][1:] == ["FY2024", "FY2023"]
    assert all("in millions" not in c for c in tb[0]["rows"][0])
