# Deployment

## Execution modes

| mode | where | how documents are processed |
|---|---|---|
| **worker** (default) | your machine, a VM, a container (Docker, Render, Fly.io, Railway …) | uploads return immediately; a bounded thread pool processes them page by page; progress, checkpoints and outputs are written to `PARSEFLOW_DATA_DIR`; an interrupted job resumes when its status is next polled |
| **serverless** | Vercel (auto-detected via `VERCEL`), or `PARSEFLOW_MODE=sync` | the upload request processes the document **inline** and returns when done; no background work is relied on |
| **CLI** | anywhere | `python -m backend.cli in/ --output out/` – no server, no keys, any length |

## Cloud server with accounts and saved documents (recommended)

**Why logins and saved documents disappeared on a cloud host:** Vercel instances and Render's free
plan have no permanent disk. Every restart (Render free sleeps after 15 minutes idle) or every new
serverless instance starts with an empty folder, so the accounts database and the documents in it
were gone, and a login made on one instance was unknown to the next.

**The fix: give ParseFlow a PostgreSQL database.** Set `DATABASE_URL` (Vercel's Postgres integration
sets `POSTGRES_URL`, which also works). ParseFlow then keeps accounts, sessions, history **and each
document's results** (JSON, Markdown and the source file for page images) in that database. Any
instance can open any document, and nothing is lost on a restart; the local folder is only a cache.
Free databases that work: [Neon](https://neon.tech) (also offered inside Vercel as Storage → Neon)
and [Supabase](https://supabase.com). Without a database URL on Vercel, accounts are switched off
and the Log in button is hidden; documents still open.

Steps (about 5 minutes):

1. Create a free database on https://neon.tech and copy its connection string
   (`postgresql://user:password@host/db?sslmode=require`).
2. In your host's settings, add the environment variable `DATABASE_URL` with that string:
   * **Render**: service → Environment → Add `DATABASE_URL`, then Manual Deploy.
   * **Vercel**: Project → Settings → Environment Variables → `DATABASE_URL`, then redeploy
     (or Project → Storage → Connect Database → Neon, which adds `POSTGRES_URL` for you).
   * **Railway**: add a PostgreSQL service; Railway sets `DATABASE_URL` on the app.
3. Open `/api/health`: it shows `"storage": "postgres"` and `"accounts": true`.
4. Create an account, convert a document, log out, log in again (or restart the service): the
   document is still in your list.

Tested locally against PostgreSQL 16 with two server processes on separate empty disks
(`tests/test_cloud_storage.py`; run it with `PARSEFLOW_TEST_DATABASE_URL=postgresql://...`).
Size: each saved document stores its source file (up to `PARSEFLOW_DB_SOURCE_MB`, default 50 MB) plus
its JSON and Markdown; Neon's free tier holds 0.5 GB. Guest documents are removed after
`GUEST_RETENTION_HOURS` (default 24) from the database too.

### Render.com (step by step)

1. Push the repository to GitHub (it contains `Dockerfile` and `render.yaml`).
2. On https://render.com: **New → Blueprint**, pick the repository, and confirm. Render builds the
   image (Tesseract included) and creates the web service.
3. Set `DATABASE_URL` as above (required on the free plan, where the disk is wiped on every restart).
4. Optional: set `AI_API_KEY` (Gemini, Groq, Claude or OpenAI key) so Ask and image reading use AI
   for everyone. The key stays on the server.
5. Open the `https://<name>.onrender.com` address. People can use ParseFlow without an account
   (their documents are deleted after `GUEST_RETENTION_HOURS`, default 24), or create an account
   to keep their documents.

### Railway (step by step)

1. Connect the GitHub repository `https://github.com/ADITYAMAWA04/PARSEFLOW` on [railway.com](https://railway.com).
2. Railway detects `Dockerfile` and `railway.json` automatically, building the full container with Tesseract, Poppler, and headless LibreOffice.
3. In **Settings → Networking**, generate a domain or set the port to `8000`.
4. (Optional) In **Variables**, add `DATABASE_URL` (e.g. from Neon PostgreSQL) or add a persistent Railway Volume.
5. Live link: [https://parseflow-production-b5e7.up.railway.app](https://parseflow-production-b5e7.up.railway.app)

### Any other host (Fly.io, VPS, Docker)

```bash
docker build -t parseflow .                                   # add --build-arg WITH_LIBREOFFICE=0 for a smaller image
docker run -d -p 8000:8000 -v parseflow-data:/data --restart unless-stopped parseflow
```

Mount a volume at `/data` (or set `DATABASE_URL`), and put HTTPS in front of it (the host's proxy, or
Caddy/nginx). With SQLite run **one** container; with `DATABASE_URL` several can share the database.

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
* `POST /api/process` returns the Digital Twin JSON **and** the Markdown in its response. With
  `DATABASE_URL` set, results are also stored in the database, so page images, history and accounts
  work from every instance. Without it, results live only in `/tmp` of the instance that processed
  them and accounts are switched off.

Deploy steps:

```bash
npm i -g vercel
vercel link
vercel env add DATABASE_URL        # accounts and saved documents (Neon / Supabase PostgreSQL)
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
