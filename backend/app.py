"""ParseFlow HTTP API (FastAPI). Entry point for `uvicorn app:app` (from backend/) and for Vercel
(`[tool.vercel] entrypoint = "backend.app:app"`).

Execution modes
  worker      (default, local / VM / container): uploads are processed by a bounded background
              thread pool; progress is persisted to <data>/<id>/status.json so it survives restarts and
              is visible to every worker process. Interrupted jobs resume from page checkpoints.
  serverless  (VERCEL / AWS Lambda detected, or PARSEFLOW_MODE=sync): the request processes the
              document inline and returns the result. Work after a response is not guaranteed on
              serverless platforms, so we never rely on it. Large documents are rejected with a clear
              message pointing to the CLI / a worker deployment.
"""
import json
import os
import secrets
import re
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, str(Path(__file__).parent))
from processing import config  # noqa: E402

config.load_dotenv()
from processing import ask, formats, history as doc_history, llm, markdown, ocr, pipeline, verifier, vision, vlm_ocr  # noqa: E402
import threading  # noqa: E402

ROOT = config.ROOT
SETTINGS = config.settings()
SERVERLESS = config.on_serverless() or os.getenv("PARSEFLOW_MODE", "").lower() == "sync"


def _data_dir():
    d = config.data_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:  # read-only filesystem (e.g. serverless bundle): fall back to /tmp
        d = Path("/tmp/parseflow")
        d.mkdir(parents=True, exist_ok=True)
        return d


DATA = _data_dir()
MAX_BYTES = SETTINGS.max_upload_mb * 1024 * 1024
JOBS = {}
POOL = ThreadPoolExecutor(max(1, int(os.getenv("JOB_WORKERS", "2"))))
ID_RE = re.compile(r"^[a-f0-9]{12}$")
app = FastAPI(title="ParseFlow")
STORE = doc_history.Store(DATA)
YEAR = 365 * 24 * 3600
# Accounts need storage that outlives the process: a database URL, or a normal server with a disk.
DURABLE = STORE.remote or not SERVERLESS
if not STORE.remote and (SERVERLESS or os.getenv("RENDER") or os.getenv("RAILWAY_ENVIRONMENT")):
    print("ParseFlow: no DATABASE_URL set. On this cloud host the local disk is temporary, so accounts and saved "
          "documents are lost when the server restarts. Set DATABASE_URL to a PostgreSQL database "
          "(see docs/DEPLOYMENT.md).", file=sys.stderr)
_DB_SOURCE_MB = float(os.getenv("PARSEFLOW_DB_SOURCE_MB", "50"))
_PERSISTED = ("meta.json", "status.json", "document.json", "document.md")


def _persist(doc_id, source=False):
    """With a database URL: copy this document's results (and, once, its source file) into the database so
    every server instance can open it, also after a restart. Never fails the request."""
    if not STORE.remote:
        return
    d = DATA / doc_id
    try:
        files = {n: (d / n).read_bytes() for n in _PERSISTED if (d / n).exists()}
        if source:
            for f in d.glob("source.*"):
                if f.stat().st_size <= _DB_SOURCE_MB * 1024 * 1024:
                    files[f.name] = f.read_bytes()
        STORE.put_files(doc_id, files)
    except Exception as e:
        print(f"ParseFlow: could not save {doc_id} to the database ({type(e).__name__}: {e})", file=sys.stderr)


def _hydrate(doc_id):
    """Refill a document's folder from the database when this instance does not have it (another instance
    processed it, or the disk was wiped by a restart)."""
    d = DATA / doc_id
    if d.is_dir() or not STORE.remote:
        return d.is_dir()
    try:
        files = STORE.get_files(doc_id)
    except Exception:
        return False
    if not files:
        return False
    d.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            (d / name).write_bytes(data)
    return True


def _secure(request):
    return request.url.scheme == "https" or os.getenv("PUBLIC_URL", "").startswith("https://")


@app.middleware("http")
async def _identity(request: Request, call_next):
    """Every browser gets an anonymous id so its document history works (no accounts)."""
    anon = request.cookies.get(doc_history.ANON_COOKIE) or ""
    fresh = not re.fullmatch(r"[a-f0-9]{24}", anon)
    if fresh:
        anon = uuid.uuid4().hex[:24]
    request.state.anon = anon
    token = request.cookies.get(doc_history.SESSION_COOKIE)
    # only API calls need the account (static files never wait for the database)
    request.state.user = (await run_in_threadpool(STORE.session_account, token)
                          if token and request.url.path.startswith("/api/") else None)
    response = await call_next(request)
    if fresh:
        response.set_cookie(doc_history.ANON_COOKIE, anon, max_age=YEAR, httponly=True, samesite="lax", secure=_secure(request))
    return response


def _owner(request):
    u = getattr(request.state, "user", None)
    return f"user:{u['id']}" if u else f"anon:{request.state.anon}"


SHARE_COOKIE = "pf_view"


def _share_tokens(request):
    return [t for t in (request.cookies.get(SHARE_COOKIE) or "").split(".") if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", t)][:30]


def _visible(request, doc_id):
    """An account's documents are private to it (or to people holding its share link). Guest documents open
    by their unguessable id."""
    owner = STORE.owner_of(doc_id)
    if not owner or not owner.startswith("user:") or owner == _owner(request):
        return True
    toks = _share_tokens(request)
    if toks:
        r = STORE.doc(doc_id)
        return bool(r and r.get("share") and r["share"] in toks)
    return False


def _mine(request, doc_id):
    """Rename, star, share and delete: the owner only."""
    return bool(ID_RE.match(doc_id or "")) and STORE.owner_of(doc_id) == _owner(request)


def _summarize(doc):
    """What the library shows for a document, computed once when it is read."""
    st = doc.get("stats") or {}
    blocks = doc.get("blocks") or []
    n = max(1, st.get("blocks") or len(blocks))
    kinds = [str(p.get("kind") or "") for p in doc.get("pages") or []]
    return dict(pages=st.get("pages") or len(kinds), blocks=st.get("blocks") or len(blocks), tables=st.get("tables", 0),
                equations=st.get("equations", 0), visuals=st.get("visuals", 0),
                handwritten=sum(1 for b in blocks if b.get("handwritten") or b.get("type") == "handwritten_text"),
                verified_pct=round(100 * (st.get("verified") or 0) / n), review=st.get("flagged", 0),
                headers=st.get("excluded_headers_footers", 0), scanned=sum(1 for k in kinds if k.startswith("scanned") or k == "image"),
                converted=bool(doc.get("converted")))


def _record_summary(doc_id):
    d = DATA / doc_id
    try:
        doc = json.loads((d / "document.json").read_text())
        size = sum(f.stat().st_size for f in d.glob("original.*")) or sum(f.stat().st_size for f in d.glob("source.*"))
        STORE.update_doc(doc_id, summary=json.dumps(_summarize(doc)), size=size)
    except Exception:
        pass


GUEST_HOURS = float(os.getenv("GUEST_RETENTION_HOURS", "24"))
_LAST_SWEEP = [0.0]


def _sweep_guest_docs(force=False):
    """Guest documents are temporary: removed GUEST_RETENTION_HOURS after upload (0 = keep)."""
    if GUEST_HOURS <= 0 or not DURABLE or (not force and time.time() - _LAST_SWEEP[0] < 600):
        return
    _LAST_SWEEP[0] = time.time()
    for doc_id in STORE.expired_guest_docs(GUEST_HOURS):
        if doc_id in JOBS and JOBS[doc_id].status == "running":
            continue
        STORE.delete_doc(doc_id)
        shutil.rmtree(DATA / doc_id, True)


def err(code, title, msg, status=400):
    return JSONResponse(dict(error=dict(code=code, title=title, message=msg)), status_code=status)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # never leak stack traces, paths or secrets
    return err("PF-500", "Processing Error", f"Unexpected server error ({type(exc).__name__}). The server is still running.", 500)


def _job_dir(doc_id, request=None):
    if not ID_RE.match(doc_id or ""):
        return None
    if request is not None and not _visible(request, doc_id):
        return None
    return DATA / doc_id if _hydrate(doc_id) else None


@app.get("/api/health")
def health():
    lc = ask.llm_config()
    return dict(ok=True, version=config.VERSION, ocr=ocr.available(), vision=vision.info(),
                llm=dict(configured=lc["configured"], provider=lc["provider"], model=llm.public(lc)["model"]),
                ai_settings_editable=not SERVERLESS,
                mode="serverless" if SERVERLESS else "worker",
                history=True, accounts=DURABLE, storage=STORE.kind, formats=formats.describe(),
                high_accuracy_ocr=dict(vlm_ocr.public(), in_app=os.getenv("HIGH_ACCURACY_OCR", "1") != "0"),
                limits=dict(max_upload_mb=SETTINGS.max_upload_mb, max_pages=SETTINGS.max_pages or None,
                            legacy_MAX_PAGES_ignored=SETTINGS.legacy_max_pages_ignored,
                            sync_max_pages=SETTINGS.sync_max_pages if SERVERLESS else None))


def _run_job(doc_id, src, name, d, prog):
    try:
        # phase 1: everything except reading text inside images / handwriting -> the document opens now
        doc = pipeline.process(str(src), name, doc_id, str(d), prog, defer_visuals=not SERVERLESS)
        STORE.update_doc(doc_id, status=prog.status or "complete")
        _record_summary(doc_id)
        _persist(doc_id)
        if doc.get("pending_visuals"):
            # phase 2: read those regions in the background; the open document refreshes when done
            try:
                pipeline.enrich_visuals(str(src), name, str(d), prog, lock=_doc_lock(doc_id))
            except Exception as e:
                prog.enrich = dict(state="failed", error=type(e).__name__)
                prog.save(force=True)
            _record_summary(doc_id)
            _persist(doc_id)
        return
    except pipeline.Cancelled:
        prog.status = "cancelled"
        prog.save(force=True)
    except pipeline.PFError as e:
        prog.status, prog.error = "failed", e.dict()
        prog.save(force=True)
    except Exception as e:
        prog.status, prog.error = "failed", dict(code="PF-500", title="Processing Error",
                                                 message=f"Unexpected failure ({type(e).__name__}); the server is still running.")
        prog.save(force=True)
    STORE.update_doc(doc_id, status=prog.status)
    _persist(doc_id)


@app.post("/api/process")
async def process(request: Request, file: UploadFile = File(...)):
    name = os.path.basename(file.filename or "upload").replace("\x00", "")
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if not re.fullmatch(r"[a-z0-9]{1,8}", ext) or "." + ext not in formats.SUPPORTED:
        return err("PF-204", "Unsupported Document", "ParseFlow cannot open this file type. It opens: " +
                   ", ".join(sorted(e[1:].upper() for e in formats.SUPPORTED)) + ".")
    doc_id = uuid.uuid4().hex[:12]
    d = DATA / doc_id
    d.mkdir()
    native = "." + ext in formats.NATIVE
    # never use the client's filename on disk; other formats are kept as original.<ext> and converted
    src = d / (f"source.{ext}" if native else f"original.{ext}")
    size = 0
    with open(src, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                f.close()
                shutil.rmtree(d, True)
                return err("PF-206", "File Too Large", f"Maximum upload size here is {SETTINGS.max_upload_mb} MB." +
                           (" Serverless deployments cannot accept larger request bodies; use the CLI or a worker deployment." if SERVERLESS else ""), 413)
            f.write(chunk)
    if not native:
        try:
            conv, _how = await run_in_threadpool(formats.to_pipeline_input, str(src), name, str(d))
        except formats.FormatError as e:
            shutil.rmtree(d, True)
            return err("PF-204", "Could Not Open This File", str(e))
        except Exception as e:
            shutil.rmtree(d, True)
            return err("PF-201", "Corrupt Document", f"The file could not be converted ({type(e).__name__}).")
        src = Path(conv)
    try:
        pages = pipeline.page_count(str(src), name)
    except pipeline.PFError as e:
        shutil.rmtree(d, True)
        return JSONResponse(dict(error=e.dict()), status_code=400)
    except Exception:
        shutil.rmtree(d, True)
        return err("PF-201", "Corrupt Document", "The file could not be read.")
    (d / "meta.json").write_text(json.dumps(dict(filename=name, size=size, pages=pages)))
    info = dict(document_id=doc_id, filename=name, size=size, type=ext.upper(), pages=pages, mode="serverless" if SERVERLESS else "worker")
    prog = pipeline.Progress(str(d / "status.json"))
    prog.total_pages = pages
    prog.save(force=True)
    STORE.add_doc(doc_id, _owner(request), name, pages)
    await run_in_threadpool(_persist, doc_id, True)
    _sweep_guest_docs()
    if SERVERLESS:
        if pages > SETTINGS.sync_max_pages:
            shutil.rmtree(d, True)
            return err("PF-207", "Document Too Long For This Deployment",
                       f"This serverless deployment processes up to {SETTINGS.sync_max_pages} pages per request "
                       f"(the file has {pages}). Use the CLI (python -m backend.cli) or a worker deployment for long documents.", 413)
        _run_job(doc_id, src, name, d, prog)
        if prog.status in ("failed", "cancelled"):
            return JSONResponse(dict(error=prog.error or dict(code="PF-500", title="Processing Error", message="Processing did not finish.")), status_code=500)
        # serverless instances do not share /tmp: return the result in this response
        doc = json.loads((d / "document.json").read_text())
        md = (d / "document.md").read_text(encoding="utf-8")
        return dict(info, status=prog.status, document=doc, markdown=md)
    JOBS[doc_id] = prog
    POOL.submit(_run_job, doc_id, src, name, d, prog)
    return info


@app.get("/api/status/{doc_id}")
def status(doc_id: str, request: Request):
    if not _visible(request, doc_id):
        return err("PF-404", "Unknown Document", "No such processing job.", 404)
    p = JOBS.get(doc_id)
    if p:
        return p.to_dict()
    d = _job_dir(doc_id)
    if not d or not (d / "status.json").exists():
        return err("PF-404", "Unknown Document", "No such processing job.", 404)
    st = json.loads((d / "status.json").read_text())
    stale = time.time() - (d / "status.json").stat().st_mtime > float(os.getenv("RESUME_STALE_SECONDS", "120"))
    if st.get("status") == "running" and stale and not SERVERLESS and not (d / "CANCEL").exists():
        # the process that owned this job is gone (e.g. a restart): resume from page checkpoints
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        src = next(iter(d.glob("source.*")), None)
        if src:
            prog = pipeline.Progress(str(d / "status.json"))
            JOBS[doc_id] = prog
            POOL.submit(_run_job, doc_id, src, meta.get("filename", src.name), d, prog)
            st["resumed"] = True
    elif (st.get("enrich") or {}).get("state") == "running" and stale and not SERVERLESS and doc_id not in JOBS:
        # the server restarted while images were being read in the background: finish that part
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        src = next(iter(d.glob("source.*")), None)
        if src:
            prog = pipeline.Progress(str(d / "status.json"))
            prog.__dict__.update({k: st[k] for k in ("status", "stages", "total_pages") if k in st})
            prog.done_pages = st.get("completed_pages", 0)
            JOBS[doc_id] = prog
            POOL.submit(pipeline.enrich_visuals, str(src), meta.get("filename", src.name), str(d), prog, None, _doc_lock(doc_id))
    return st


@app.post("/api/cancel/{doc_id}")
def cancel(doc_id: str, request: Request):
    d = _job_dir(doc_id, request)
    if not d:
        return err("PF-404", "Unknown Document", "No such processing job.", 404)
    (d / "CANCEL").write_text("1")
    if doc_id in JOBS:
        JOBS[doc_id].cancel()
    return dict(ok=True)


@app.post("/api/skip-ocr/{doc_id}")
def skip_ocr(doc_id: str, request: Request):
    """Stop waiting for the high-accuracy OCR server and finish with ParseFlow's own reading."""
    d = _job_dir(doc_id, request)
    if not d:
        return err("PF-404", "Unknown Document", "No such processing job.", 404)
    (d / "SKIP_VLM").write_text("1")
    return dict(ok=True)


@app.get("/api/document/{doc_id}")
def document(doc_id: str, request: Request, download: int = 0):
    d = _job_dir(doc_id, request)
    p = d / "document.json" if d else None
    if not p or not p.exists():
        return err("PF-404", "Not Ready", "Document not available.", 404)
    if download:
        return FileResponse(p, media_type="application/json", filename=_download_name(doc_id, ".json"))
    return FileResponse(p, media_type="application/json")


def _download_name(doc_id, ext):
    r = STORE.doc(doc_id) or {}
    base = re.sub(r"[^\w.\- ]+", "_", (r.get("filename") or doc_id)).strip() or doc_id
    return re.sub(r"\.(pdf|png|jpe?g)$", "", base, flags=re.I) + ext


@app.get("/api/document/{doc_id}/markdown")
def document_markdown(doc_id: str, request: Request, annotate: int = 0, include_headers: int = 0, download: int = 0):
    d = _job_dir(doc_id, request)
    p = d / "document.json" if d else None
    if not p or not p.exists():
        return err("PF-404", "Not Ready", "Document not available.", 404)
    if not annotate and not include_headers and (d / "document.md").exists():
        text = (d / "document.md").read_text(encoding="utf-8")
    else:
        text = markdown.render(json.loads(p.read_text()), annotate=bool(annotate), include_headers=bool(include_headers))
    headers = {"Content-Disposition": f'attachment; filename="{_download_name(doc_id, ".md")}"'} if download else None
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8", headers=headers)


@app.get("/api/page/{doc_id}/{n}.png")
def page_png(doc_id: str, n: int, request: Request):
    d = _job_dir(doc_id, request)
    if not d:
        return err("PF-404", "Not Found", "Page image not found.", 404)
    cache = d / f"page_{n}.png"
    if not cache.exists():
        src = next(iter(d.glob("source.*")), None)
        if not src:
            return err("PF-404", "Not Found", "Page image not found.", 404)
        try:
            if src.suffix == ".pdf":
                import pymupdf
                with pymupdf.open(str(src)) as doc:
                    if not 1 <= n <= doc.page_count:
                        return err("PF-404", "Not Found", "Page out of range.", 404)
                    page = doc[n - 1]
                    s = min(SETTINGS.render_scale, 2400.0 / max(1.0, page.rect.width, page.rect.height))
                    page.get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False).save(str(cache))
            else:
                if n != 1:
                    return err("PF-404", "Not Found", "Page out of range.", 404)
                from PIL import Image
                Image.open(src).convert("RGB").save(cache)
        except Exception:
            return err("PF-500", "Render Error", "The page image could not be rendered.", 500)
    return FileResponse(cache, media_type="image/png")


@app.get("/api/page/{doc_id}/{n}.jpg")
def page_jpg(doc_id: str, n: int, request: Request, w: int = 0):
    """Page image as JPEG (much faster to make and to send than PNG). w>0: a small preview of that width."""
    d = _job_dir(doc_id, request)
    if not d or not 0 < n < 100000:
        return err("PF-404", "Not Found", "Page image not found.", 404)
    w = max(0, min(int(w or 0), 2400))
    w = 0 if w == 0 else max(80, (w // 40) * 40)          # a few cache sizes only
    cache = d / f"page_{n}_{w}.jpg"
    if not cache.exists():
        src = next(iter(d.glob("source.*")), None)
        if not src:
            return err("PF-404", "Not Found", "Page image not found.", 404)
        try:
            from PIL import Image
            if src.suffix == ".pdf":
                import pymupdf
                with pymupdf.open(str(src)) as doc:
                    if not 1 <= n <= doc.page_count:
                        return err("PF-404", "Not Found", "Page out of range.", 404)
                    page = doc[n - 1]
                    s_ = (w / max(1.0, page.rect.width)) if w else min(SETTINGS.render_scale, 2400.0 / max(1.0, page.rect.width, page.rect.height))
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(s_, s_), alpha=False)
                    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            else:
                if n != 1:
                    return err("PF-404", "Not Found", "Page out of range.", 404)
                im = Image.open(src).convert("RGB")
                if w:
                    im = im.resize((w, max(1, round(im.size[1] * w / im.size[0]))), Image.BILINEAR)
            tmp = cache.with_suffix(".tmp")
            im.save(tmp, "JPEG", quality=85 if not w else 75, optimize=False)
            os.replace(tmp, cache)
        except Exception:
            return err("PF-500", "Render Error", "The page image could not be rendered.", 500)
    return FileResponse(cache, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/ask")
async def ask_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:
        return err("PF-400", "Bad Request", "Expected JSON: {document_id, question}.")
    doc_id, q = str(body.get("document_id", "")), str(body.get("question", ""))[:2000]
    if not q.strip():
        return err("PF-400", "Bad Request", "The question is empty.")
    d = _job_dir(doc_id, request)
    p = d / "document.json" if d else None
    if p and p.exists():
        doc = json.loads(p.read_text())
    elif isinstance(body.get("document"), dict) and isinstance(body["document"].get("blocks"), list):
        # serverless: the instance that processed the file may be gone; the client sends the twin back
        doc = body["document"]
    else:
        return err("PF-404", "Not Ready", "Document not available.", 404)
    hist = body.get("history") if isinstance(body.get("history"), list) else []
    hist = [dict(role=h.get("role"), content=str(h.get("content", ""))[:4000]) for h in hist[-8:]
            if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")]
    return ask.answer(doc, q, history=hist)


# ------------------------------------------------------------------ AI reading of one visual, on demand

_DOC_LOCKS = {}


def _doc_lock(doc_id):
    return _DOC_LOCKS.setdefault(doc_id, threading.Lock())


def _crop_block(d, doc, b):
    """Render the block's region from the stored source at 1.5x the viewer scale."""
    src = next(iter(d.glob("source.*")), None)
    if not src:
        raise FileNotFoundError("source")
    n = b["page"]
    meta = doc["pages"][n - 1] if 0 < n <= len(doc.get("pages", [])) else {}
    from PIL import Image
    if src.suffix == ".pdf":
        import pymupdf
        with pymupdf.open(str(src)) as pdf:
            page = pdf[n - 1]
            S = float(meta.get("scale") or (meta.get("width") or page.rect.width * 2) / page.rect.width)
            x0, y0, x1, y1 = (v / S for v in b["bbox"])
            pix = page.get_pixmap(matrix=pymupdf.Matrix(S * 1.5, S * 1.5), clip=pymupdf.Rect(x0, y0, x1, y1), alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    im = Image.open(src).convert("RGB")
    k = im.size[0] / float(meta.get("width") or im.size[0])
    x0, y0, x1, y1 = b["bbox"]
    return im.crop((int(x0 * k), int(y0 * k), int(x1 * k), int(y1 * k)))


@app.post("/api/visual/{doc_id}/{block_id}")
def read_visual_endpoint(doc_id: str, block_id: str, request: Request):
    d = _job_dir(doc_id, request)
    p = d / "document.json" if d else None
    if not p or not p.exists():
        return err("PF-404", "Not Ready", "Document not available.", 404)
    if not vision.available():
        return err("PF-412", "AI Not Connected", "Connect an AI key first (Connect AI in the top bar) to read images with AI.", 412)
    with _doc_lock(doc_id):
        doc = json.loads(p.read_text())
        b = next((x for x in doc.get("blocks", []) if x.get("block_id") == block_id), None)
    if not b:
        return err("PF-404", "Not Found", "No such block.", 404)
    if b.get("pending"):
        return err("PF-409", "Still Reading", "This image is still being read; try again in a few seconds.", 409)
    try:
        crop = _crop_block(d, doc, b)
    except Exception:
        return err("PF-500", "Render Error", "The image region could not be rendered.", 500)
    res, e = vision.read_visual(crop)
    if not res:
        return err("PF-502", "AI Reading Failed", f"The AI could not read this image: {e}.", 502)
    with _doc_lock(doc_id):
        doc = json.loads(p.read_text())          # re-read: another request may have updated other blocks
        b = next(x for x in doc["blocks"] if x.get("block_id") == block_id)
        b["warnings"] = [w for w in b.get("warnings", []) if not w.startswith(("AI reading failed", "Model self-reported", "Read by an AI"))]
        pipeline.apply_ai_reading(b, res)
        meta = doc["pages"][b["page"] - 1] if 0 < b["page"] <= len(doc.get("pages", [])) else {}
        verifier.verify(b, dict(width=meta.get("width") or 10 ** 6, height=meta.get("height") or 10 ** 6, norm_text=""), None)
        st = doc.setdefault("stats", {})
        st["verified"] = sum(1 for x in doc["blocks"] if x.get("verification") == "verified")
        st["flagged"] = len(doc["blocks"]) - st["verified"]
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, p)
        (d / "document.md").write_text(markdown.render(doc), encoding="utf-8")
    _record_summary(doc_id)
    _persist(doc_id)
    return dict(block=b, ai=vision.info())


# ------------------------------------------------------------------ AI key setup (local server only)

def _local_request(request: Request):
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient") and request.headers.get("x-parseflow") == "1"


def _write_env(updates):
    p = Path(os.getenv("PARSEFLOW_ENV_FILE") or (ROOT / ".env"))
    lines = p.read_text().splitlines() if p.exists() else []
    seen = set()
    out = []
    for ln in lines:
        k = ln.split("=", 1)[0].strip() if "=" in ln and not ln.lstrip().startswith("#") else None
        if k in updates:
            seen.add(k)
            if updates[k] is not None:
                out.append(f"{k}={updates[k]}")
            continue
        out.append(ln)
    out += [f"{k}={v}" for k, v in updates.items() if k not in seen and v is not None]
    p.write_text("\n".join(out) + "\n")


@app.post("/api/settings/ai")
async def set_ai(request: Request):
    if SERVERLESS:
        return err("PF-403", "Not Allowed", "On a hosted deployment, set AI_API_KEY in the platform's environment variables.", 403)
    if not _local_request(request):
        return err("PF-403", "Not Allowed", "AI settings can only be changed from the computer running ParseFlow.", 403)
    try:
        body = await request.json()
    except Exception:
        return err("PF-400", "Bad Request", "Expected JSON: {api_key, provider?, model?}.")
    key = str(body.get("api_key") or "").strip()
    prov = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    vmodel = str(body.get("vision_model") or "").strip()
    name_ok = lambda m: not m or re.fullmatch(r"[A-Za-z0-9._:/\-]{2,120}", m)
    if prov not in ("", "gemini", "anthropic", "ollama", "groq", "openai", "openrouter") or not name_ok(model) or not name_ok(vmodel):
        return err("PF-400", "Bad Request", "Unknown provider or model name.")
    if prov != "ollama" and not re.fullmatch(r"[A-Za-z0-9_\-.]{20,300}", key):
        return err("PF-400", "Invalid Key", "That does not look like an API key.")
    keys = ("AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_VISION_MODEL", "OLLAMA_AUTODETECT")
    old = {k: os.environ.get(k) for k in keys}
    new = {"AI_API_KEY": key if prov != "ollama" else "", "AI_PROVIDER": prov, "AI_MODEL": model, "AI_VISION_MODEL": vmodel,
           "OLLAMA_AUTODETECT": "1"}
    os.environ.update(new)
    if prov == "ollama":
        llm.ollama_models(force=True)
    cfg = llm.config("text")
    note = None
    try:
        if prov == "ollama":
            llm.generate(cfg, "Reply with the single word OK.", max_tokens=8, timeout=30, retries=1)
        else:
            llm.check_key(cfg)
            try:   # one real request, so a wrong model or region shows up now; temporary errors don't block saving
                llm.generate(cfg, "Reply with the single word OK.", max_tokens=8, timeout=30, retries=1)
            except llm.AIError as e:
                if e.permanent:
                    raise
                note = f"Key saved. {str(e)[0].upper() + str(e)[1:]}."
    except llm.AIError as e:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return err("PF-401", "Key Not Working", f"The key was not saved: {e}.", 400)
    except Exception as e:   # network trouble while checking: do not save an unchecked key
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return err("PF-502", "Could Not Check Key", f"The key could not be checked ({type(e).__name__}); check your internet connection.", 502)
    _write_env(new)
    return dict(ok=True, ai=llm.public(cfg), vision=vision.info(), note=note)


@app.post("/api/settings/ai/check")
def ai_check(request: Request):
    """Tiny request to every model the configured key can use; per-model status. Local requests only."""
    if SERVERLESS or not _local_request(request):
        return err("PF-403", "Not Allowed", "Only available on the computer running ParseFlow.", 403)
    cfg = llm.config("text")
    if not cfg["configured"]:
        return err("PF-412", "AI Not Connected", "Connect an AI key first.", 412)
    try:
        rows = llm.diagnose(cfg)
    except llm.AIError as e:
        return err("PF-502", "Check Failed", str(e), 502)
    return dict(provider=cfg["provider"], results=rows, working=[r["model"] for r in rows if r["ok"]], ai=llm.public(cfg))


@app.get("/api/settings/ai/local")
def local_models(request: Request):
    """Models pulled in a local Ollama (names and whether they can read images). Local requests only."""
    if SERVERLESS or not _local_request(request):
        return err("PF-403", "Not Allowed", "Only available on the computer running ParseFlow.", 403)
    ms = llm.ollama_models(force=True)
    return dict(reachable=ms is not None, url=llm.ollama_url(), models=ms or [])


@app.delete("/api/settings/ai")
def clear_ai(request: Request):
    if SERVERLESS or not _local_request(request):
        return err("PF-403", "Not Allowed", "AI settings can only be changed from the computer running ParseFlow.", 403)
    for k in ("AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_VISION_MODEL", "VISION_API_KEY", "LLM_API_KEY"):
        os.environ.pop(k, None)
    os.environ["OLLAMA_AUTODETECT"] = "0"     # "disconnect" also stops auto-using a local Ollama
    _write_env({"AI_API_KEY": None, "AI_PROVIDER": None, "AI_MODEL": None, "AI_VISION_MODEL": None,
                "VISION_API_KEY": "", "LLM_API_KEY": "", "OLLAMA_AUTODETECT": "0"})
    return dict(ok=True)


# ------------------------------------------------------------------ history and optional sign-in

def _history_row(r):
    r = dict(r)
    r["summary"] = json.loads(r["summary"]) if isinstance(r.get("summary"), str) and r["summary"] else None
    r["starred"], r["shared"] = bool(r.get("starred")), bool(r.get("share"))
    r.pop("share", None)
    r.pop("owner", None)
    d = DATA / r["doc_id"]
    if r["summary"] is None and (d / "document.json").exists() and r["status"] in ("complete", "partial"):
        _record_summary(r["doc_id"])                       # documents converted before v0.9.1
        row = STORE.doc(r["doc_id"]) or {}
        r["summary"] = json.loads(row["summary"]) if row.get("summary") else None
        r["size"] = row.get("size")
    if not d.is_dir() and r["status"] not in ("processing", "running"):
        # listed from the database; the folder is refilled when the document is opened
        return dict(r, available=r["status"] == "complete" and STORE.remote, exists=STORE.remote)
    st = r["status"]
    if st in ("processing", "running") and (d / "status.json").exists():
        try:
            st = json.loads((d / "status.json").read_text()).get("status") or st
        except Exception:
            pass
        if st not in ("processing", "running"):
            STORE.update_doc(r["doc_id"], status=st)
    return dict(r, status=st, available=(d / "document.json").exists(), exists=d.is_dir())


@app.get("/api/history")
def history_list(request: Request):
    rows = [_history_row(r) for r in STORE.docs(_owner(request))]
    docs = [r for r in rows if r["exists"]]
    return dict(enabled=True, signed_in=bool(request.state.user), guest_hours=GUEST_HOURS, storage=STORE.kind,
                documents=docs, totals=dict(documents=len(docs), pages=sum(r.get("pages") or 0 for r in docs),
                                            bytes=sum(r.get("size") or 0 for r in docs)))


@app.patch("/api/history/{doc_id}")
async def edit_history(doc_id: str, request: Request):
    """Rename or star one of your documents."""
    if not _mine(request, doc_id):
        return err("PF-404", "Not Found", "No such document in your history.", 404)
    try:
        b = await request.json()
    except Exception:
        return err("PF-400", "Bad Request", "Expected JSON: {filename?, starred?}.")
    kw = {}
    if "filename" in b:
        name = re.sub(r"[\x00-\x1f/\\]+", " ", str(b.get("filename") or "")).strip()[:180]
        if not name:
            return err("PF-400", "Name Needed", "Give the document a name.")
        kw["filename"] = name
    if "starred" in b:
        kw["starred"] = 1 if b.get("starred") else 0
    STORE.update_doc(doc_id, **kw)
    return dict(ok=True, **{k: (bool(v) if k == "starred" else v) for k, v in kw.items()})


@app.post("/api/history/{doc_id}/share")
def share_doc(doc_id: str, request: Request):
    """A read-only link to one of your documents; anyone with the link can open it until you stop sharing."""
    if not _mine(request, doc_id):
        return err("PF-404", "Not Found", "No such document in your history.", 404)
    token = (STORE.doc(doc_id) or {}).get("share") or secrets.token_urlsafe(18)
    STORE.update_doc(doc_id, share=token)
    return dict(ok=True, url=str(request.base_url).rstrip("/") + "/s/" + token)


@app.delete("/api/history/{doc_id}/share")
def unshare_doc(doc_id: str, request: Request):
    if not _mine(request, doc_id):
        return err("PF-404", "Not Found", "No such document in your history.", 404)
    STORE.update_doc(doc_id, share=None)
    return dict(ok=True)


@app.get("/s/{token}")
def open_shared(token: str, request: Request):
    r = STORE.doc_by_share(token)
    if not r:
        return RedirectResponse("/?shared=gone", status_code=303)
    toks = [t for t in _share_tokens(request) if t != token][-19:] + [token]
    resp = RedirectResponse(f"/?open={r['doc_id']}", status_code=303)
    resp.set_cookie(SHARE_COOKIE, ".".join(toks), max_age=YEAR, httponly=True, samesite="lax", secure=_secure(request))
    return resp


@app.get("/api/history/export.zip")
def export_all(request: Request):
    """Every document you can see in your library: Markdown + JSON, in one ZIP."""
    import io
    import zipfile
    buf, used = io.BytesIO(), set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in STORE.docs(_owner(request), limit=1000):
            d = DATA / r["doc_id"] if _hydrate(r["doc_id"]) else None
            if not d or not (d / "document.json").exists():
                continue
            base = _download_name(r["doc_id"], "")
            name, k = base, 2
            while name in used:
                name, k = f"{base} ({k})", k + 1
            used.add(name)
            if (d / "document.md").exists():
                z.write(d / "document.md", f"{name}.md")
            z.write(d / "document.json", f"{name}.json")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="parseflow-documents.zip"'})


@app.delete("/api/history")
def delete_all_history(request: Request):
    """Delete every document in your library (guest: this browser's; member: your account's)."""
    n = 0
    for r in STORE.docs(_owner(request), limit=10000):
        if r["doc_id"] in JOBS:
            JOBS[r["doc_id"]].cancel()
        STORE.delete_doc(r["doc_id"])
        shutil.rmtree(DATA / r["doc_id"], True)
        n += 1
    return dict(ok=True, deleted=n)


@app.post("/api/auth/delete")
async def delete_account(request: Request):
    """Delete your account and every document in it. Needs your password."""
    u = request.state.user
    if not u:
        return err("PF-401", "Not Logged In", "Log in first.", 401)
    try:
        b = await request.json()
    except Exception:
        b = {}
    if not await run_in_threadpool(STORE.check_password, u["id"], str(b.get("password") or "")):
        return err("PF-400", "Account Not Deleted", "The password is not correct.")
    delete_all_history(request)
    STORE.delete_account(u["id"])
    r = JSONResponse(dict(ok=True))
    r.delete_cookie(doc_history.SESSION_COOKIE)
    return r


# ------------------------------------------------------------------ accounts (username + password)

_FAILS = {}


def _too_many(key):
    now = time.time()
    hits = [t for t in _FAILS.get(key, []) if now - t < 300]
    _FAILS[key] = hits
    return len(hits) >= 8


def _session_response(request, uid, body):
    STORE.claim(f"anon:{request.state.anon}", f"user:{uid}")
    token = STORE.new_session(uid)
    r = JSONResponse(body)
    r.set_cookie(doc_history.SESSION_COOKIE, token, max_age=doc_history.SESSION_DAYS * 86400, httponly=True,
                 samesite="lax", secure=_secure(request))
    return r


async def _creds(request):
    try:
        b = await request.json()
    except Exception:
        return None, None
    return str(b.get("username") or "").strip(), str(b.get("password") or "")


@app.get("/api/me")
def me(request: Request):
    u = request.state.user
    return dict(signed_in=bool(u), user=dict(username=u["username"]) if u else None,
                accounts=DURABLE, guest_hours=GUEST_HOURS)


@app.post("/api/auth/register")
async def register(request: Request):
    username, password = await _creds(request)
    ip = request.client.host if request.client else "?"
    if not DURABLE:
        return err("PF-403", "Accounts Need A Database", "This server has no database for accounts yet. "
                   "Set DATABASE_URL (see docs/DEPLOYMENT.md); documents still open without an account.", 403)
    if _too_many("reg:" + ip):
        return err("PF-429", "Too Many Attempts", "Too many attempts. Wait a few minutes and try again.", 429)
    try:
        uid = STORE.create_account(username, password)
    except ValueError as e:
        _FAILS.setdefault("reg:" + ip, []).append(time.time())
        return err("PF-400", "Could Not Create Account", str(e))
    return _session_response(request, uid, dict(ok=True, user=dict(username=username)))


@app.post("/api/auth/login")
async def login(request: Request):
    username, password = await _creds(request)
    ip = request.client.host if request.client else "?"
    key = f"login:{ip}:{username.lower()}"
    if not DURABLE:
        return err("PF-403", "Accounts Need A Database", "This server has no database for accounts yet. "
                   "Set DATABASE_URL (see docs/DEPLOYMENT.md).", 403)
    if _too_many(key):
        return err("PF-429", "Too Many Attempts", "Too many wrong passwords. Wait five minutes and try again.", 429)
    uid = await run_in_threadpool(STORE.check_login, username, password)
    if not uid:
        _FAILS.setdefault(key, []).append(time.time())
        return err("PF-401", "Wrong Username Or Password", "The username or password is not correct.", 401)
    _FAILS.pop(key, None)
    return _session_response(request, uid, dict(ok=True, user=dict(username=STORE.account(uid)["username"])))


@app.post("/api/auth/logout")
def logout(request: Request):
    STORE.end_session(request.cookies.get(doc_history.SESSION_COOKIE))
    r = JSONResponse(dict(ok=True))
    r.delete_cookie(doc_history.SESSION_COOKIE)
    return r


@app.post("/api/auth/password")
async def change_password(request: Request):
    u = request.state.user
    if not u:
        return err("PF-401", "Not Logged In", "Log in first.", 401)
    try:
        b = await request.json()
        await run_in_threadpool(STORE.change_password, u["id"], str(b.get("current") or ""), str(b.get("new") or ""))
    except ValueError as e:
        return err("PF-400", "Password Not Changed", str(e))
    except Exception:
        return err("PF-400", "Bad Request", "Expected JSON: {current, new}.")
    r = JSONResponse(dict(ok=True))
    r.delete_cookie(doc_history.SESSION_COOKIE)        # all sessions end; log in again with the new password
    return r


@app.get("/login")
def login_page():
    return FileResponse(ROOT / "frontend" / "login.html", media_type="text/html")


@app.delete("/api/history/{doc_id}")
def delete_history(doc_id: str, request: Request):
    if not ID_RE.match(doc_id) or STORE.owner_of(doc_id) != _owner(request):
        return err("PF-404", "Not Found", "No such document in your history.", 404)
    if doc_id in JOBS:
        JOBS[doc_id].cancel()
    STORE.delete_doc(doc_id)
    shutil.rmtree(DATA / doc_id, True)
    return dict(ok=True)


_sweep_guest_docs(force=True)

app.mount("/", StaticFiles(directory=str(ROOT / "frontend"), html=True), name="ui")
