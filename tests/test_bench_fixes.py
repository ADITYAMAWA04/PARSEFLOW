"""Fixes from the olmOCR-Bench failure analysis (v0.7.0). Synthetic inputs; the sample-set score is
checked separately by bench/regression_check.py."""
from processing import layout, pipeline
from processing import tables as T
from processing.textnorm import normalize_ligatures


def test_tex_spacing_accents_are_recombined():
    assert normalize_ligatures("J´an Min´aˇc") == "Ján Mináč"
    assert normalize_ligatures("Gr¨ obner, Erd˝os, fa¸cade, ´ıtem") == "Gröbner, Erdős, façade, ítem"
    assert normalize_ligatures("x^2 and a~b `code`") == "x^2 and a~b `code`"      # ASCII is never touched


def test_table_spans_only_cover_empty_positions():
    grid = [[(0, 0, 10, 20), (10, 0, 20, 20), (20, 0, 30, 20)],
            [(0, 20, 10, 40), (10, 20, 20, 40), (20, 20, 30, 40)],     # tall boxes, but the row below has its own cells
            [None, (10, 40, 20, 60), (20, 40, 30, 60)]]
    texts = [["a", "b", "c"], ["d", "e", "f"], [None, "h", "i"]]
    cells = T.spans_from_grid(grid, [0, 10, 20, 30], [0, 20, 40, 60], texts)
    assert all(c["rs"] == 1 and c["cs"] == 1 for c in cells)
    assert sorted((c["r"], c["c"]) for c in cells) == [(r, c) for r in range(3) for c in range(3)]


def test_spanning_header_above_value_columns_joins_the_table():
    t = dict(bbox=(80, 400, 520, 480), col_x=[80, 350, 410, 470, 520], header_rows=1,
             rows=[["", "Jan 26, 2025", "Jan 28, 2024", "Jan 29, 2023"], ["Cost of revenue", "$178", "$141", "$138"]])
    words = [(423, 393, 437, 400, "Year"), (439, 393, 458, 400, "Ended")]
    assert pipeline._absorb_spanning_headers([t], words, body=7.0)
    html = T.to_html(t)
    assert '<th colspan="3">Year Ended</th>' in html and t["header_rows"] == 2 and t["bbox"][1] == 393


def _blk(text, bbox, typ="paragraph"):
    return dict(type=typ, role="body", content=text, bbox=bbox)


def test_sentence_continued_in_the_next_column_is_moved_and_joined():
    a = _blk("Abstract text that ends properly.", (0, 0, 300, 100))
    right_top = _blk("tobacco company espousing CSR should be judged simply as a corporate entity.", (320, 0, 600, 80))
    right_next = _blk("CORPORATE SOCIAL RESPONSIBILITY: THE CONTEXT and more text here.", (320, 100, 600, 300))
    left_body = _blk("Over the past three decades increasing pressure has required companies. This paper examines whether a",
                     (0, 120, 300, 400))
    out = pipeline._link_continuations([a, right_top, right_next, left_body], line_h=20)
    assert [b["content"][:4] for b in out] == ["Abst", "Over", "toba", "CORP"]
    assert right_top.get("continues_previous")
    from processing import markdown
    doc = dict(pages=[{}], blocks=[dict(b, page=1, reading_order=k, block_id=str(k)) for k, b in enumerate(out)])
    assert "examines whether a tobacco company" in markdown.render(doc)


def test_list_items_are_not_taken_for_continuations():
    p = _blk("157. A potato is launched upward. The distance travelled after t seconds is given by", (320, 0, 600, 90))
    item = _blk("a. Find the velocity and acceleration functions.", (20, 60, 300, 80))
    out = pipeline._link_continuations([item, p], line_h=20)
    assert out == [item, p] and not item.get("continues_previous")


def test_numbered_sections_run_down_the_columns():
    def it(n, x, y):
        return dict(kind="text", bbox=(x, y, x + 250, y + 30), ref=dict(text=f"{n}. Section {n}"))
    items = [it(1, 50, 100), it(3, 400, 100), it(2, 50, 400), it(4, 400, 400)]
    order, _ = layout.reading_order(items, line_height=12)
    assert [i["ref"]["text"][0] for i in order] == ["1", "2", "3", "4"]
