# Deployment

## Execution modes

| mode | where | how documents are processed |
|---|---|---|
| **worker** (default) | your machine, a VM, a container (Docker, Render, Fly.io, Railway …) | uploads return immediately; a bounded thread pool processes them page by page; progress, checkpoints and outputs are written to `PARSEFLOW_DATA_DIR`; an interrupted job resumes when its status is next polled |
| **serverless** | Vercel (auto-detected via `VERCEL`), or `PARSEFLOW_MODE=sync` | the upload request processes the document **inline** and returns when done; no background work is relied on |
| **CLI** | anywhere | `python -m backend.cli in/ --output out/` – no server, no keys, any length |

## Cloud server with accounts and saved documents (recommended)

Accounts (username and password) and saved documents need a server that keeps files: the documents
and the accounts database (`parseflow.db`, SQLite) live in `PARSEFLOW_DATA_DIR`, which must be on a
persistent disk. Vercel cannot do this, but any host that runs a Docker container with a disk can.

### Render.com (step by step)

1. Push the repository to GitHub (it contains `Dockerfile` and `render.yaml`).
2. On https://render.com: **New → Blueprint**, pick the repository, and confirm. Render builds the
   image (Tesseract and LibreOffice included) and creates the service with a 10 GB disk at `/data`.
   Disks need a paid instance (the "Starter" type); on the free type, files are lost on every restart.
3. Optional: in the service's **Environment**, set `AI_API_KEY` (Gemini, Groq, Claude or OpenAI key)
   so Ask and image reading use AI for everyone. The key stays on the server.
4. Open the `https://<name>.onrender.com` address. People can use ParseFlow without an account
   (their documents are deleted after `GUEST_RETENTION_HOURS`, default 24), or create an account
   to keep their documents.

### Any other host (Railway, Fly.io, a VPS)

```bash
docker build -t parseflow .                                   # add --build-arg WITH_LIBREOFFICE=0 for a smaller image
docker run -d -p 8000:8000 -v parseflow-data:/data --restart unless-stopped parseflow
```

Mount a volume at `/data`, and put HTTPS in front of it (the host's proxy, or Caddy/nginx). Run
**one** container: jobs run in its thread pool, and SQLite is a single-server database. Back up the
volume to keep accounts and documents.

Security notes: passwords are stored as salted scrypt hashes; sessions are random tokens of which
only a hash is stored; repeated wrong passwords are slowed down; account documents are visible only
to their owner. There is no email, so a forgotten password cannot be reset. Keep the server's data
folder private.

Not tested here: the Dockerfile and `render.yaml` were written for this release, but the build
environment had no Docker daemon, so the image has not been built.

## Vercel

What the repository now provides (verified against Vercel's current documentation, **not verified by
an actual deployment** – no Vercel account or network access was available while making these
changes):

* `pyproject.toml` declares the runtime dependencies (previously `dependencies = []`) and
  `[tool.vercel] entrypoint = "backend.app:app"`.
* `vercel.json` sets `maxDuration: 300` for `backend/app.py` and excludes `bench/`, `tests/`,
  `docs/`, `data/` from the bundle.
* The app writes only to `/tmp/parseflow` on serverless (the project directory is read-only there;
  the old code crashed at import with `mkdir`).
* The frontend is served by `app.mount("/", StaticFiles(...))`, which Vercel promotes to its CDN.

Hard platform limits (Vercel docs, 2026): request body **4.5 MB**, function duration **300 s**
(Hobby) / 800 s (Pro), memory **2 GB** (Hobby) / 4 GB (Pro), filesystem writable only under `/tmp`
and not persistent between invocations, no Tesseract binary in the Python runtime.

Consequences, handled explicitly:

* `MAX_UPLOAD_MB` defaults to 4 on serverless; larger uploads get `PF-206` with an explanation.
* Documents over `SYNC_MAX_PAGES` (default 25) get `PF-207` pointing to the CLI / a worker.
* Scanned pages are preserved and flagged (no OCR engine) instead of failing.
* Results live in `/tmp` of the instance that processed them, so `POST /api/process` returns the
  Digital Twin JSON **and** the Markdown in its response, and the browser sends the twin back with
  each Ask request. Page images (`/api/page/...`) and later `GET /api/document/{id}` calls may hit a
  different instance and return 404 (the viewer then shows no page image). Durable multi-instance
  storage needs object storage (e.g. Vercel Blob / S3) – not implemented here.

Deploy steps:

```bash
npm i -g vercel
vercel link
vercel env add VISION_API_KEY      # optional
vercel env add LLM_API_KEY         # optional, for Ask
vercel deploy
```

Then check: `/` loads, `/api/health` returns `mode: "serverless"`, upload a small PDF, open the
Markdown tab, ask a question.

## Long documents: worker deployment

For 400+ page documents or scanned PDFs, run the same app as a long-lived process with Tesseract:

```bash
# Debian/Ubuntu
sudo apt install tesseract-ocr
pip install -r requirements.txt
cd backend && uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
```

Use one uvicorn worker per container (jobs run in its thread pool; `JOB_WORKERS` controls
concurrent documents, `OCR_WORKERS` parallel OCR per document). Mount a persistent volume at
`PARSEFLOW_DATA_DIR` so checkpoints survive restarts.

## Environment variables

See `.env.example`. None are required for the offline pipeline. Secrets are read only on the
server and never returned by any endpoint (`/api/health` reports `configured: true/false` only).
