"""v0.9.0 fixes found by comparing failing olmOCR-Bench sample tests (synthetic checks of each rule)."""
from processing import headers, pipeline


def test_photo_texture_is_not_text():
    junk = [("e", 0.85), ("qu", 0.61), ("Eee", 0.66), ("FA,", 0.5), ("(a", 0.6)]
    real = [("Lake Shore", 0.8), ("Model 475", 0.86), ("10", 0.4), ("Yes", 0.92), ("数据", 0.5)]
    assert all(pipeline._ocr_junk(t, c) for t, c in junk)
    assert not any(pipeline._ocr_junk(t, c) for t, c in real)


def test_document_control_footer_is_page_furniture():
    blocks = [dict(bbox=(72, 100, 540, 680), text="Body text " * 80, size=10, n_lines=40),
              dict(bbox=(72, 703, 551, 717), text="Revision: 2.4 P/N 119-036 10 June 2019", size=10, n_lines=1)]
    headers.classify_page(blocks, 612, 792, body_size=10)
    assert blocks[1]["role"] == "page_footer" and blocks[0]["role"] == "body"


def _table(rows, col_x, bbox):
    return dict(rows=rows, col_x=col_x, bbox=bbox, n_rows=len(rows), n_cols=len(col_x) - 1, header_rows=1)


def test_several_headings_over_one_table_each_span_their_columns():
    cx = [100, 140, 180, 220, 240, 280, 320, 340, 380, 420]
    t = _table([["#", "a", "b", "", "a", "b", "", "a", "b"], ["1", "0.1", "0.2", "", "0.3", "0.4", "", "0.5", "0.6"]], cx, (100, 200, 420, 240))
    w = lambda x0, x1, txt: (x0, 186, x1, 194, txt)
    pipeline._absorb_spanning_headers([t], [w(165, 195, "ReACT"), w(265, 295, "Plan+Execute"), w(365, 395, "Hypothesizer")], 8)
    head = [c for c in t["cells"] if c["r"] == 0 and c["text"]]
    assert [(c["text"], c["c"], c["cs"]) for c in head] == [("ReACT", 1, 2), ("Plan+Execute", 4, 2), ("Hypothesizer", 7, 2)]


def test_summary_rows_and_trailing_row_join_the_table():
    cx = [100, 200, 240, 280]
    upper = _table([["Task", "x", "y"], ["One", "0.1", "0.2"]], cx, (100, 100, 280, 140))
    lower = _table([["Average", "0.5", "0.6"]], cx, (100, 145, 280, 155))
    merged = pipeline._merge_stacked_tables([upper, lower])
    assert len(merged) == 1 and merged[0]["rows"][-1] == ["Average", "0.5", "0.6"]
    words = [(105, 158, 140, 165, "Average", 0, 0, 0), (145, 158, 170, 165, "(all)", 0, 0, 1),
             (205, 158, 230, 165, "0.7", 0, 0, 2), (245, 158, 270, 165, "0.8", 0, 0, 3),
             (100, 200, 400, 210, "Table", 1, 0, 0)]
    pipeline._absorb_trailing_rows(merged, words, 8)
    assert merged[0]["rows"][-1] == ["Average (all)", "0.7", "0.8"] and merged[0]["n_rows"] == 4
