from pathlib import Path
import json
import tempfile
import logging
from casparser import read_cas_pdf
from casparser.types import CASData, NSDLCASData
from casparser.enums import TransactionType
from casparser.exceptions import CASParseError, ParserException

logger = logging.getLogger("portfolioiq")


def _fix_segregation_classification(parsed) -> None:
    """Runtime correction for a real upstream casparser bug: its
    transaction classifier only checks for "segregat" in the description
    on the *positive*-units leg of a segregation event. The matching
    negative-units leg falls through to REDEMPTION, generating a phantom
    taxable gain for units reclassified, not sold."""
    for folio in parsed.folios:
        for scheme in folio.schemes:
            for txn in scheme.transactions:
                if (
                    txn.type == TransactionType.REDEMPTION
                    and "segregat" in (txn.description or "").lower()
                ):
                    txn.type = TransactionType.SEGREGATION


class _UserFacingParseError(Exception):
    """A parse failure whose message is meant for the person who
    uploaded (wrong password, wrong kind of file). These used to be
    HTTPExceptions raised from the request; now that parsing happens in
    the background job, they reach the client through the job row's
    error_detail, which GET /api/upload-status returns as `message`."""


def _parse_upload(content: bytes, filename: str, password: str):
    """Bytes -> CASData. This is the slow part of an upload and is
    deliberately called from the background job, never from the request
    handler. Raises _UserFacingParseError with a message fit to show
    as-is."""
    if filename.endswith(".json"):
        # A previously-parsed CAS JSON — useful for testing, or if
        # someone already has one from elsewhere. Re-validated through
        # the same CASData pydantic model read_cas_pdf itself returns
        # (Decimal/date fields coerce correctly from JSON strings/numbers
        # via pydantic, same as any other CASData construction), so
        # everything downstream sees an identical shape either way.
        try:
            raw_dict = json.loads(content)
        except json.JSONDecodeError:
            raise _UserFacingParseError("That doesn't look like valid JSON.")
        try:
            parsed = CASData.model_validate(raw_dict)
        except Exception as exc:
            # Just the missing/invalid field names, not pydantic's full
            # multi-line dump. This message is now rendered in the UI's
            # error banner (parse failures reach the client through the
            # job row), where a raw ValidationError repr — five stanzas
            # with docs URLs — is unreadable. The full detail is still in
            # the server log via the logger.info in the caller.
            fields = ", ".join(
                sorted(
                    {
                        ".".join(str(p) for p in e.get("loc", ()))
                        for e in getattr(exc, "errors", lambda: [])()
                    }
                )
            )
            detail = f" (missing or invalid: {fields})" if fields else ""
            raise _UserFacingParseError(
                f"That JSON isn't a parsed CAS statement{detail}."
            )
    else:
        with tempfile.TemporaryDirectory(prefix="portfolioiq-") as tmp:
            pdf_path = Path(tmp) / "statement.pdf"
            pdf_path.write_bytes(content)
            try:
                # A plain synchronous call now, not run_in_threadpool:
                # this whole function already runs on a worker thread
                # (see _run_ingest_job_sync's docstring), so there is no
                # event loop here to keep unblocked.
                parsed = read_cas_pdf(str(pdf_path), password)
            except CASParseError as exc:
                if "password" in str(exc).lower():
                    raise _UserFacingParseError(
                        "That password didn't work. Double check it and try again."
                    )
                raise _UserFacingParseError(
                    "We couldn't read this as a CAS statement. Make sure it's the unmodified PDF from CAMS or KFintech."
                )
            except ParserException:
                raise _UserFacingParseError("This statement couldn't be parsed.")
            except Exception:
                # Anything else — a casparser internal error on a real-world
                # PDF shape the CASParseError/ParserException catches above
                # don't cover — must not surface as a bare, undiagnosable 500.
                # Logged with the full traceback (visible in Render's log
                # viewer) so a report of "upload failed" is actually
                # debuggable instead of a dead end.
                logger.exception("read_cas_pdf failed on an uncategorised exception")
                raise _UserFacingParseError(
                    "This statement couldn't be parsed. If this keeps happening, it's a bug — the server log has the details."
                )

    if isinstance(parsed, NSDLCASData):
        raise _UserFacingParseError(
            "This looks like an NSDL/CDSL demat statement. PortfolioIQ currently analyses "
            "CAMS/KFintech mutual-fund statements only."
        )
    _fix_segregation_classification(parsed)
    return parsed
