"""Faithful Python port of the ORIGINAL frontend md() function (frontend/index.html at commit c3a5917).

Used only to reproduce the pre-change baseline: the original backend produced JSON and the browser
built Markdown from it, so benchmarking the original system requires this exact transformation.
Do not "improve" this file - it must stay byte-for-byte equivalent to the old JavaScript.
"""
import json


def _js_num(v):
    # JavaScript prints 0.99 as 0.99 and 1.0 as 1
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def md(doc):
    o = f"<!-- ParseFlow · {doc['filename']} -->\n"
    blocks = sorted(doc["blocks"], key=lambda b: (b["page"], b["reading_order"]))
    for b in blocks:
        prov = (f"<!-- {b['block_id']} · p{b['page']} · bbox {json.dumps(b['bbox'], separators=(',', ':'))} · "
                f"{b['extractor']} · {_js_num(b['confidence'])} ({b['confidence_type']}) · {b['verification']} -->")
        t = None
        if b.get("content") is None:
            kind = "Visual region not interpreted" if b["type"] in ("figure", "unknown") else "Unreadable region"
            t = f"> ⚠ {kind} — flagged for review (confidence {_js_num(b['confidence'])})."
        elif b["type"] == "heading" or (b["type"] == "handwritten_text" and b["reading_order"] == 1):
            t = "# " + b["content"]
        elif b["type"] == "table":
            rows = b["rows"]
            lines = []
            for i, r in enumerate(rows):
                line = "| " + " | ".join(r) + " |"
                if i == 0:
                    line += "\n|" + "|".join("---" for _ in r) + "|"
                lines.append(line)
            t = f"**{b['content']}**\n\n" + "\n".join(lines)
        elif b["type"] == "handwritten_code":
            t = "### Handwritten Code\n```text\n" + b["content"] + "\n```"
        elif b["type"] == "diagram":
            t = "### Diagram\n" + b["content"] + "\n\nRelationships: " + ", ".join(b.get("relationships") or [])
        elif b["type"] == "chart":
            t = "### Chart\n" + b["content"]
        elif b["type"] == "equation":
            t = "### Equation\n$$" + str(b.get("latex")) + "$$"
        elif b["type"] == "handwritten_text":
            t = "### Handwritten Notes\n" + b["content"]
        else:
            t = b["content"]
        o += f"\n{prov}\n{t}\n"
    return o
