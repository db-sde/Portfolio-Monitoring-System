# casparser-web

A small web app around [casparser](https://github.com/codereverser/casparser):
upload a CAMS / KFintech / NSDL / CDSL Consolidated Account Statement (CAS)
PDF, get back a readable portfolio, transaction history and capital gains
report.

```
backend/    FastAPI app (main.py) — the only thing that touches your PDF
frontend/   Static HTML/CSS/JS dashboard, served by the same process
```

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

This app has no user accounts — it's built for one person/business, not
the general public. Set `APP_PASSWORD` and every request (except
`/api/health`) requires it via HTTP Basic Auth (any username, that
password). **If you leave it unset, the app runs completely open** — fine
for local development, not for anything reachable from the internet. The
app logs a warning on startup if it's unset.

## Run it locally

```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
APP_PASSWORD=changeme uvicorn main:app --reload --port 8000
```

Open http://127.0.0.1:8000 — the frontend is served from the same process.

## Run it with Docker

```bash
docker build -t casparser-web .
docker run --rm -p 8000:8000 -e APP_PASSWORD=changeme casparser-web
```

(The Dockerfile hasn't been build-tested in this environment — Docker
wasn't installed on the machine this was written on. The underlying
`uvicorn main:app --app-dir backend` invocation it uses *was* verified
directly. Run `docker build` once yourself before relying on it.)

## Deploying it as a public service

This is a single stateless container — no database, no filesystem writes
outside a request's own temp directory — so it fits any container host's
free/hobby tier: [Render](https://render.com), [Fly.io](https://fly.io),
[Railway](https://railway.app), or a small VPS running the Docker image
behind a reverse proxy (Caddy or nginx) for TLS.

A few things worth setting at the host/proxy level rather than in app code:
- **Request body size limit** — the app already rejects files over 20MB
  (`MAX_UPLOAD_BYTES` in `backend/main.py`), but a proxy-level cap (e.g.
  nginx `client_max_body_size`) is a cheap second line of defense.
- **Rate limiting** — there's none built in. If this gets real traffic,
  put a basic per-IP rate limit in front of `/api/parse` (it's the only
  CPU-heavy route) so one user can't peg the server.
- **CORS** — `backend/main.py` currently allows all origins (`allow_origins=["*"]`)
  since the frontend is served from the same origin by default. Tighten
  this if you ever split the frontend onto a different domain/CDN.

I'm not creating any hosting accounts or deploying this for you — pick a
host above and follow their "deploy a Dockerfile" flow, or ask me to walk
through a specific one once you've decided.

## Endpoints

- `POST /api/parse` — multipart form: `file` (the PDF), `password`,
  `include_gains` (`"true"`/`"false"`). Returns `{ok, mode, data, gains,
  gifts, gains_error}`.
- `GET /api/sample?mode=mf|demat` — static, fully-fictitious demo data
  (no upload, no parsing) so the UI has something to show before a real
  statement is uploaded.
- `GET /api/health` — liveness check.
- `GET /api/docs` — interactive FastAPI/Swagger docs.
