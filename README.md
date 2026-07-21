# casparser-web

A small web app around [casparser](https://github.com/codereverser/casparser):
upload a CAMS / KFintech / NSDL / CDSL Consolidated Account Statement (CAS)
PDF, get back a readable portfolio, transaction history and capital gains
report.

```
backend/    FastAPI app (main.py) — the only thing that touches your PDF
frontend/   Static HTML/CSS/JS dashboard
```

Deployed as two separate services — backend on Render, frontend on Vercel
(see "Deploying it" below) — but `backend/main.py` also serves `frontend/`
directly, so `uvicorn main:app` alone is enough for local dev.

## How it's architected (read this before hosting it for other people)

Parsing happens **server-side**, one request at a time:

1. The browser posts the PDF + password to `POST /api/parse`.
2. The backend writes it to a request-scoped temp directory, calls
   `casparser.read_cas_pdf`, and deletes the temp directory the moment
   parsing finishes (success or failure) — see the `with tempfile.TemporaryDirectory()`
   block in `backend/main.py`.
3. The parsed JSON goes straight back in the response. Nothing is written
   to a database, nothing is logged except "a request happened" and whether
   it succeeded — never the filename, password, or statement contents.

This is the same architecture the upstream author uses for their own
[casparser-web](https://github.com/codereverser/casparser-web) (FastAPI +
uvicorn). It is **not** the same as running fully client-side — the PDF
does cross the network to your server for the duration of one request.
If you deploy this publicly, put it behind HTTPS (any of the hosts below
do this for you) so that transit is encrypted, and don't add logging,
analytics, or a reverse-proxy config that captures request bodies.

## Access control

There isn't any — no login, no passcode. Anyone with the URL can use it.
That was a deliberate call for this deployment; if that changes, the
access-gate approach (env-var-driven HTTP Basic Auth in front of every
route except `/api/health`) is straightforward to bring back.

## Run it locally

```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open http://127.0.0.1:8000 — the frontend is served from the same process.

## Run it with Docker

```bash
docker build -t casparser-web .
docker run --rm -p 8000:8000 casparser-web
```

(The Dockerfile hasn't been build-tested in this environment — Docker
wasn't installed on the machine this was written on. The underlying
`uvicorn main:app --app-dir backend` invocation it uses *was* verified
directly. Run `docker build` once yourself before relying on it.)

## Deploying it: backend on Render, frontend on Vercel

This project is split across two hosts on purpose — Vercel doesn't run
persistent containers (this backend needs one: a native PDF-parsing
dependency, and a request-scoped temp directory), so the backend goes on
Render and the frontend, which is plain static HTML/CSS/JS with no build
step, goes on Vercel.

**1. Backend → Render**, from this same repo:
1. [dashboard.render.com](https://dashboard.render.com) → **New** → **Web Service**, connect this repo
2. Root directory: leave blank (the `Dockerfile` is at the repo root)
3. Instance type: Free is fine for personal/occasional use (cold starts
   after 15 min idle, ~30-50s to wake back up)
4. Health Check Path: `/api/health`
5. Deploy. Copy the `https://your-service.onrender.com` URL Render gives you.

**2. Point the frontend at that URL** — edit `frontend/vercel.json` and
replace `CHANGE-ME.onrender.com` with the real Render hostname from step 1,
then commit and push. This is the only thing that needs your Render URL in
it; the JS itself still just calls relative `/api/...` paths.

**3. Frontend → Vercel**:
1. [vercel.com/new](https://vercel.com/new), import this same repo
2. Set **Root Directory** to `frontend` (this is a monorepo — Vercel needs
   to know to serve just that folder)
3. Framework preset: **Other** / no build command — it's static files as-is
4. Deploy. Vercel's `vercel.json` (inside `frontend/`) rewrites `/api/*` to
   the Render backend, so from the browser's point of view everything is
   same-origin — no CORS wrangling, no backend URL hardcoded into `app.js`.

A few things worth knowing either way:
- **Request body size limit** — the app already rejects files over 20MB
  (`MAX_UPLOAD_BYTES` in `backend/main.py`), but a proxy-level cap (e.g.
  nginx `client_max_body_size`) is a cheap second line of defense if you
  ever move off Render.
- **Rate limiting** — there's none built in. If this gets real traffic,
  put a basic per-IP rate limit in front of `/api/parse` (it's the only
  CPU-heavy route) so one user can't peg the server.
- **CORS** — `backend/main.py` allows all origins (`allow_origins=["*"]`).
  With the Vercel rewrite proxy, the browser never actually makes a
  cross-origin request, so this mostly matters if something calls the
  Render URL directly instead of through Vercel.

I'm not creating any hosting accounts or deploying this for you — the
steps above are for you to run in each dashboard. Send me the Render URL
once you have it and I'll update and push `vercel.json` for you.

## Endpoints

- `POST /api/parse` — multipart form: `file` (the PDF), `password`,
  `include_gains` (`"true"`/`"false"`). Returns `{ok, mode, data, gains,
  gifts, gains_error}`.
- `GET /api/sample?mode=mf|demat` — static, fully-fictitious demo data
  (no upload, no parsing) so the UI has something to show before a real
  statement is uploaded.
- `GET /api/health` — liveness check.
- `GET /api/docs` — interactive FastAPI/Swagger docs.
