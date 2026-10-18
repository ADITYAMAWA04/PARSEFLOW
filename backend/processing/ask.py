"""Ask ParseFlow: grounded question answering over the extracted Digital Twin.

retrieval  : structural chunks (section-aware groups of blocks; tables, equations and figures with
             their captions) ranked with BM25. Whole-document questions (summaries, overviews,
             practice questions) get a spread of chunks across sections instead of a keyword match.
generation : optional LLM (LLM_PROVIDER=anthropic|gemini, LLM_API_KEY). The model sees only the
             numbered evidence chunks and must cite them as [S1], [S2] ... Citations that do not refer
             to a provided chunk are removed and reported. Without a provider the endpoint says so
             explicitly and returns the best-matching passages (mode="search").
Nothing here ever fabricates a model answer: `mode` states what actually produced the response.
"""
import json
import math
import os
import re
import time
from collections import Counter

import httpx

from .headers import HEADER_ROLES

STOP = set("""a an the of to in on for and or but is are was were be been being this that these those it its as at by with from
into about over under what which who whom whose when where why how do does did can could should would may might will shall
not no yes than then there their them they we you your i me my our us he she his her him if so such also any all some each
document paper page pages explain describe tell show give please""".split())
GLOBAL_Q = re.compile(r"\b(summari[sz]e|summary|overview|tl;?dr|main (points|ideas|idea|argument|findings)|beginner|"
                      r"practice questions|quiz|contradict|inconsisten|whole document|entire document|key takeaways|outline|"
                      r"what is (this|the) (document|paper) about)\b", re.I)
MAX_CONTEXT_CHARS = int(os.getenv("ASK_MAX_CONTEXT_CHARS", "14000"))


def _toks(s):
    # keep "log(n)", "3.5", "o(n)" style tokens together, but drop sentence punctuation at the end
    out = []
    for t in re.findall(r"[a-z0-9][a-z0-9\-_.%$]*", (s or "").lower()):
        t = t.rstrip(".-_")
        if t and t not in STOP and len(t) > 1:
            out.append(t)
    return out


def block_text(b):
    t = b.get("type")
    if t == "table" and b.get("rows"):
        return "Table:\n" + "\n".join(" | ".join(r) for r in b["rows"])
    if t == "equation":
        return "Equation (LaTeX): " + (b.get("latex") or b.get("content") or "")
    if t in ("figure", "chart", "diagram"):
        parts = [f"[{t}]"]
        if b.get("description"):
            parts.append("Description: " + b["description"])
        if b.get("content") and b.get("content") != b.get("description"):
            parts.append(b["content"])
        if b.get("ocr_text"):
            parts.append("Text inside the figure: " + b["ocr_text"])
        return "\n".join(parts)
    return b.get("content") or ""


def _conf(blocks):
    """Extraction confidence of a passage: text-layer text ~1.0, OCR of handwriting much lower."""
    cs = [float(b.get("confidence") or 0) for b in blocks]
    return min(1.0, sum(cs) / len(cs)) if cs else 0.5


def build_chunks(doc, max_chars=1400):
    """Section-aware chunks. Every chunk keeps block ids and pages for citations."""
    blocks = []
    for b in sorted(doc.get("blocks", []), key=lambda b: (b["page"], b["reading_order"])):
        if b.get("superseded_by") or b.get("role") in HEADER_ROLES or not (b.get("content") or b.get("latex") or b.get("rows") or b.get("ocr_text")):
            continue
        if b.get("type") == "page_text":   # whole-page model reading: split into paragraphs so retrieval stays focused
            for k, part in enumerate(p for p in re.split(r"\n\s*\n", b["content"]) if p.strip()):
                blocks.append(dict(b, content=part.strip(), type="heading" if re.match(r"#{1,6}\s", part.strip()) else "paragraph",
                                   reading_order=b["reading_order"] + k / 1000))
            continue
        blocks.append(b)
    chunks, cur, section = [], [], None

    def flush():
        nonlocal cur
        if cur:
            chunks.append(dict(blocks=[b["block_id"] for b in cur], pages=sorted({b["page"] for b in cur}), section=section,
                               text="\n\n".join(block_text(b) for b in cur), conf=_conf(cur)))
        cur = []

    for i, b in enumerate(blocks):
        t = b["type"]
        if t == "heading":
            flush()
            section = b.get("content")
            cur = [b]
            continue
        if t in ("table", "equation", "figure", "chart", "diagram"):
            # standalone chunk with its caption and the paragraph that introduces it
            flush()
            ctx = [x for x in blocks[max(0, i - 1):i] if x["type"] in ("paragraph", "caption")]
            cap = [x for x in blocks[i + 1:i + 2] if x["type"] == "caption"]
            grp = ctx + [b] + cap
            chunks.append(dict(blocks=[x["block_id"] for x in grp], pages=sorted({x["page"] for x in grp}), section=section,
                               text="\n\n".join(block_text(x) for x in grp), conf=_conf([b])))
            continue
        cur.append(b)
        if sum(len(block_text(x)) for x in cur) > max_chars:
            flush()
    flush()
    for k, c in enumerate(chunks):
        c["id"] = k
    return chunks


class BM25:
    def __init__(self, docs, k1=1.4, b=0.75):
        self.docs = [Counter(_toks(d)) for d in docs]
        self.len = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 1.0
        df = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def score(self, q):
        qt = _toks(q)
        out = []
        for i, d in enumerate(self.docs):
            s = 0.0
            for t in qt:
                f = d.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / max(1, self.avg)))
            out.append(s)
        return out


def retrieve(doc, question, k=8):
    chunks = build_chunks(doc)
    if not chunks:
        return [], "empty"
    page_m = re.search(r"\bpage\s+(\d+)", question, re.I)
    if GLOBAL_Q.search(question):
        # spread over the document: first chunk of each section, in order, within the budget
        seen, picked = set(), []
        for c in chunks:
            if c["section"] not in seen or len(picked) < 3:
                picked.append(c)
                seen.add(c["section"])
        out, used = [], 0
        for c in picked:
            if used + len(c["text"]) > MAX_CONTEXT_CHARS:
                break
            out.append(c)
            used += len(c["text"])
        return out, "coverage"
    scores = BM25([(c["section"] or "") + "\n" + c["text"] for c in chunks]).score(question)
    if page_m:
        p = int(page_m.group(1))
        scores = [s + (3.0 if p in c["pages"] else 0) for s, c in zip(scores, chunks)]
    ranked = sorted(range(len(chunks)), key=lambda i: -scores[i])
    out, used = [], 0
    for i in ranked[:k]:
        if scores[i] <= 0:
            break
        c = chunks[i]
        if used + len(c["text"]) > MAX_CONTEXT_CHARS:
            continue
        out.append(dict(c, score=round(scores[i], 3)))
        used += len(c["text"])
    # keep document order for readability
    out.sort(key=lambda c: c["id"])
    if not out:
        return [], "no-match"
    return out, "bm25"


# ----------------------------------------------------------------------------------- LLM adapter

from . import llm  # noqa: E402


def llm_config():
    return llm.config("text")


LLMError = llm.AIError


def _call(cfg, system, user, timeout=90.0, retries=2, history=()):
    return llm.generate(cfg, user, system=system, history=history, max_tokens=2500, timeout=timeout, retries=retries, temperature=0.3)


SYSTEM = """You are ParseFlow's document assistant - a friendly, sharp expert tutor, like ChatGPT, helping the user with ONE document they uploaded.
You receive numbered evidence passages [S1], [S2] ... retrieved from that document, plus the conversation so far.

How to answer:
- Actually help. Explain, teach, summarise, compare, give step-by-step instructions, solve or walk through equations, quiz the user - whatever the question needs. Do not just quote lines.
- Ground document facts in the passages and cite them inline right after the claim, e.g. "Use the Boss-Extrude tool [S2]." Use only the labels you were given; never invent a citation.
- You MAY add your own general knowledge to make the answer useful (background, simpler explanations, examples, tips). Mark it so the user can tell it apart: put it in a short part that starts with "**Beyond the document:**", or tag a sentence with "(general knowledge)". Never present general knowledge as something the document says.
- If the passages do not cover the question, say so in one short sentence first ("The document doesn't cover this directly."), then still help from general knowledge, clearly labelled.
- Never invent numbers, steps, names or equations and attribute them to the document. Passages may contain OCR errors - mention it when it matters.
- Format with Markdown: short paragraphs, numbered steps for procedures, bullet lists, **bold** for key terms, tables when comparing, LaTeX between $...$ for maths. Be concise but complete; match the user's language and tone.
- For follow-up questions, use the conversation so far."""


def _retrieval_query(question, history):
    """Follow-ups ("and then?", "explain that simpler") need the previous topic to retrieve well."""
    prev = [h["content"] for h in history if h.get("role") == "user"][-1:]
    if prev and len(_toks(question)) < 6:
        return prev[0] + " " + question
    return question


def best_sentences(chunks, question, k=4):
    """Extractive fallback without a model: the sentences / bullet points that share the most
    (idf-weighted) words with the question, each with the label of the passage it came from."""
    qt = set(_toks(question))
    if not qt:
        return []
    sents = []
    for i, c in enumerate(chunks):
        w = c.get("conf", 0.8) ** 3
        for part in re.split(r"(?<=[.!?])\s+|\s*[\u2022\u25cf\u25aa]\s*|\n+", c["text"]):
            t = part.strip(" -\t")
            words = re.findall(r"[A-Za-z]{2,}", t)
            if 12 <= len(t) <= 400 and len(words) >= 5 and not t.startswith(("Table:", "[figure", "Equation (LaTeX)")):
                sents.append((t, f"S{i + 1}", w))
    if not sents:
        return []
    df = Counter(x for t, _, _ in sents for x in set(_toks(t)))
    n = len(sents)
    scored = []
    for t, lab, wq in sents:
        tt = set(_toks(t))
        sc = sum(math.log(1 + n / df[x]) for x in qt & tt)
        if sc > 0:
            scored.append((wq * sc / (1 + 0.002 * len(t)), t, lab))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, t, lab in scored:
        key = t.lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append((t, lab))
        if len(out) >= k:
            break
    return out


def _sources(chunks):
    return [dict(label=f"S{i + 1}", chunk_id=c["id"], block_ids=c["blocks"], pages=c["pages"], section=c.get("section"),
                 excerpt=c["text"][:400]) for i, c in enumerate(chunks)]


def answer(doc, question, use_llm=True, history=None):
    question = (question or "").strip()
    history = [h for h in (history or []) if h.get("role") in ("user", "assistant") and h.get("content")][-8:]
    if not question:
        return dict(error="empty question")
    chunks, strategy = retrieve(doc, _retrieval_query(question, history))
    sources = _sources(chunks)
    cfg = llm_config()
    base = dict(question=question, retrieval=strategy, sources=sources)
    if not chunks and strategy == "empty" and not (use_llm and cfg["configured"]):
        return dict(base, mode="none", answer="The extracted document has no text to search.", warnings=[])
    if not (use_llm and cfg["configured"]):
        if not chunks:
            return dict(base, mode="search", model=None, answer="No passage in the extracted document matches this question.",
                        warnings=["No matching passages.", "No AI connected - search results only."])
        best = best_sentences(chunks, _retrieval_query(question, history))
        lines = "\n".join(f"- {t} [{lab}]" for t, lab in best)
        return dict(base, mode="search", model=None,
                    answer=(("Closest statements in the document:\n\n" + lines + "\n\n") if best else "") +
                           ("*AI answers are off, so these are quoted, not explained. Click **Connect AI** (top bar) to use a "
                            "free Gemini key or a local model (Ollama) for real, explained answers.*"),
                    warnings=["No AI connected - search results only."])
    if not chunks and strategy != "empty":
        # let the model see an overview so it can say what the document does (not) cover
        chunks, _ = retrieve(doc, "summary overview of the whole document")
        sources = _sources(chunks)
        base["sources"] = sources
    evidence = "\n\n".join(f"[S{i + 1}] (pages {', '.join(map(str, c['pages']))}{'; section: ' + c['section'] if c.get('section') else ''})\n{c['text']}"
                             for i, c in enumerate(chunks)) or "(no passages - the document has no extractable text)"
    user = f"Document: {doc.get('filename')}\n\nEvidence passages from the document:\n{evidence}\n\nUser's question: {question}"
    hist = [(h["role"], h["content"]) for h in history]
    if hist and hist[0][0] != "user":
        hist = hist[1:]
    try:
        text = _call(cfg, SYSTEM, user, history=hist)
    except LLMError as e:
        return dict(base, mode="search", model=None, answer=f"The AI could not answer right now ({e}). Showing search results instead.",
                    warnings=[str(e)])
    valid = {s["label"] for s in sources}
    bad = sorted({m for m in re.findall(r"\[(S\d+)\]", text) if m not in valid})
    for m in bad:
        text = text.replace(f"[{m}]", "")
    cited = sorted({m for m in re.findall(r"\[(S\d+)\]", text)}, key=lambda s: int(s[1:]))
    warns = [f"Removed citation(s) to non-existent passages: {', '.join(bad)}"] if bad else []
    if not cited:
        warns.append("This answer cites no passage from the document; treat it as general knowledge.")
    return dict(base, mode="llm", model=llm.public(cfg)["model"], provider=cfg["provider"], answer=text.strip(), cited=cited, warnings=warns)
