"""
casparser-web backend.

A single endpoint that wraps casparser's `read_cas_pdf`: accept an uploaded
CAS PDF + password, parse it, optionally compute the capital gains report,
and return plain JSON. Nothing about the request is ever written to disk
outside a single request-scoped temp directory, and nothing is logged.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
import tempfile
from dataclasses import asdict
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from casparser import read_cas_pdf
from casparser.analysis import CapitalGainsReport
from casparser.enums import FileType
from casparser.exceptions import (
    CASParseError,
    GainsError,
    IncompleteCASError,
    ParserException,
)
from casparser.types import NSDLCASData

from samples import SAMPLE_DEMAT, SAMPLE_MF, SAMPLE_MF_GAINS

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB — real CAS PDFs are a few MB at most
GAINS_ELIGIBLE = {FileType.CAMS.value, FileType.KFINTECH.value}

# We never want the uploaded filename, password, or parsed contents to reach
# a log line — only that a request happened and how it resolved.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("casparser-web")

app = FastAPI(title="casparser-web", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# Opt-in access gate: this app has no concept of user accounts, and is meant
# for one person/business, not the open public — so if APP_PASSWORD is set,
# every request (except the liveness check) must present it via HTTP Basic
# Auth. Username is ignored; only the password is checked. Leave the env var
# unset for local development.
_APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
if not _APP_PASSWORD:
    log.warning(
        "APP_PASSWORD is not set — this instance has NO access control. "
        "Set it before exposing this to the internet."
    )

_UNPROTECTED_PATHS = {"/api/health"}


@app.middleware("http")
async def _require_passcode(request: Request, call_next):
    if not _APP_PASSWORD or request.url.path in _UNPROTECTED_PATHS:
        return await call_next(request)

    header = request.headers.get("authorization", "")
    ok = False
    if header.startswith("Basic "):
        try:
            _, _, supplied = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            ok = secrets.compare_digest(supplied, _APP_PASSWORD)
        except (binascii.Error, UnicodeDecodeError):
            ok = False
    if not ok:
        return JSONResponse(
            {"detail": {"error_code": "UNAUTHORIZED", "message": "Password required."}},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="casparser-web"'},
        )
    return await call_next(request)


def _jsonable(value: Any) -> Any:
    """Recursively make dataclass/Decimal/date values JSON-safe."""
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# Finance Act 2023 (Sec 50AA): debt-oriented mutual funds acquired on or
# after this date lose indexation and LTCG treatment entirely - every gain
# on them is short-term, taxed at slab rate, regardless of holding period.
# casparser's own GainEntry.gain_type still applies the pre-2023 flat
# 3-year LTCG rule to every debt fund uniformly, so it's corrected here
# rather than upstream, since that's a real tax-methodology call, not a
# parsing bug.
DEBT_STCG_ONLY_FROM = _date(2023, 4, 1)


def _to_date(value) -> _date:
    return value if isinstance(value, _date) else _date.fromisoformat(str(value))


def _serialize_gain(entry) -> dict:
    d = asdict(entry)
    fund = d.pop("fund")
    d["scheme"] = fund["scheme"]
    d["folio"] = fund["folio"]
    d["isin"] = fund["isin"]
    d["fund_type"] = fund["type"]
    d["acquisition_value"] = entry.acquisition_value

    gain = entry.gain
    d["gain"] = gain
    if fund["type"] == "DEBT" and _to_date(entry.purchase_date) >= DEBT_STCG_ONLY_FROM:
        d["gain_type"] = "STCG"
        d["stcg"] = gain
        d["ltcg"] = Decimal(0)
        d["ltcg_taxable"] = Decimal(0)
    else:
        d["gain_type"] = entry.gain_type.name
        d["ltcg"] = entry.ltcg
        d["stcg"] = entry.stcg
        d["ltcg_taxable"] = entry.ltcg_taxable
    return _jsonable(d)


def _serialize_gift(entry) -> dict:
    d = asdict(entry)
    fund = d.pop("fund")
    d["scheme"] = fund["scheme"]
    d["folio"] = fund["folio"]
    d["isin"] = fund["isin"]
    return _jsonable(d)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error_code": code, "message": message})


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/sample")
def sample(mode: str = "mf"):
    """Fully-fictitious demo data — no upload, no parsing, no real PAN/PII."""
    if mode == "demat":
        return {"ok": True, "mode": "demat", "data": SAMPLE_DEMAT, "gains": None, "gifts": None, "gains_error": None}
    return {"ok": True, "mode": "mf", "data": SAMPLE_MF, "gains": SAMPLE_MF_GAINS, "gifts": [], "gains_error": None}


@app.post("/api/parse")
async def parse(
    file: UploadFile = File(...),
    password: str = Form(default=""),
    include_gains: bool = Form(default=False),
    force_pdfminer: bool = Form(default=False),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise _error(400, "NOT_A_PDF", "Please upload the original CAS PDF file.")

    content = await file.read()
    if not content:
        raise _error(400, "EMPTY_FILE", "That file came through empty. Try uploading it again.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise _error(413, "FILE_TOO_LARGE", "That file is larger than we accept (20MB max).")

    with tempfile.TemporaryDirectory(prefix="casparser-") as tmp:
        pdf_path = Path(tmp) / "statement.pdf"
        pdf_path.write_bytes(content)
        del content  # don't keep a second copy of the statement around in memory

        try:
            data = await run_in_threadpool(
                read_cas_pdf, str(pdf_path), password, force_pdfminer=force_pdfminer
            )
        except CASParseError as exc:
            message = str(exc)
            if "password" in message.lower():
                raise _error(401, "WRONG_PASSWORD", "That password didn't work. Double check it and try again.")
            raise _error(
                422,
                "UNREADABLE_FILE",
                "We couldn't read this as a CAS statement. Make sure it's the unmodified "
                "PDF from CAMS, KFintech, NSDL or CDSL.",
            )
        except ParserException as exc:
            log.warning("parse failed: %s", type(exc).__name__)
            raise _error(422, "PARSE_FAILED", "This statement couldn't be parsed.")

    is_demat = isinstance(data, NSDLCASData)
    payload: dict[str, Any] = {
        "ok": True,
        "mode": "demat" if is_demat else "mf",
        "data": data.model_dump(mode="json", by_alias=True),
        "gains": None,
        "gifts": None,
        "gains_error": None,
    }

    if include_gains and not is_demat and data.file_type in GAINS_ELIGIBLE:
        try:
            report = await run_in_threadpool(CapitalGainsReport, data)
            payload["gains"] = [_serialize_gain(g) for g in report.gains]
            payload["gifts"] = [_serialize_gift(g) for g in report.gifts]
            if report.has_error():
                payload["gains_error"] = (
                    "Some schemes couldn't be included: "
                    + "; ".join(f"{name} — {msg}" for name, msg in report.errors)
                )
        except IncompleteCASError:
            payload["gains_error"] = (
                "This looks like a Summary statement rather than a Detailed one — gains "
                "need the full transaction history. Re-download the Detailed CAS and try again."
            )
        except GainsError as exc:
            payload["gains_error"] = f"Couldn't compute gains: {exc}"

    log.info("parsed statement ok (mode=%s, gains=%s)", payload["mode"], include_gains)
    return payload


# Serves the frontend too when run standalone (local dev, or a single-service
# deploy). In the Vercel+Render split, Vercel is the canonical frontend and
# proxies /api/* here (see frontend/vercel.json) — this mount just means the
# bare Render URL still renders something sane instead of a 404, and `uvicorn
# main:app` alone is still enough for local testing. Registered last so it
# doesn't shadow the /api routes.
frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
