"""
PortfolioIQ — gains_service.py

Realised capital-gains (Schedule 112A style) and gift-transfer reporting,
built on top of casparser's own `CapitalGainsReport` (FIFO lot matching).
This isn't part of the PortfolioIQ spec's own calculations.py/portfolio.py
— it's the same feature casparser-web (the archived MVP) had, restored
here so it isn't lost in the unification onto PortfolioIQ.

CapitalGainsReport needs the *original* parsed pydantic CASData object
(Decimal amounts, real date objects, TransactionType enums) — not the
JSON-serialised dict PortfolioIQ persists as cas_data.json. So gains are
computed once, at upload time, right after `read_cas_pdf` returns the
live object, and the *result* (already-serialised) is what gets persisted
here. A CAS re-uploaded as pre-parsed JSON (no live pydantic object
available) simply has no gains data — see `clear_gains` in main.py.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from casparser.analysis import CapitalGainsReport
from casparser.exceptions import GainsError, IncompleteCASError

# Finance (No. 2) Act 2023 (Sec 50AA): debt-oriented mutual funds acquired on
# or after this date lose indexation and LTCG treatment entirely — every
# gain on them is short-term, taxed at slab rate, regardless of holding
# period. casparser's own GainEntry.gain_type still applies the pre-2023
# flat 3-year LTCG rule to every debt fund uniformly, so it's corrected
# here rather than upstream, since that's a tax-methodology call, not a
# parsing bug (same fix as the archived casparser-web/backend/main.py).
DEBT_STCG_ONLY_FROM = _date(2023, 4, 1)

_DEFAULT_PATH = Path(__file__).resolve().parent / "gains_data.json"


def _gains_data_path() -> Path:
    # Same cwd-independence reasoning as config_manager._config_path.
    return Path(os.environ.get("GAINS_DATA_PATH", str(_DEFAULT_PATH)))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _to_date(value) -> _date:
    return value if isinstance(value, _date) else _date.fromisoformat(str(value))


def _serialize_gain(entry, advisor: Optional[str]) -> dict:
    d = asdict(entry)
    fund = d.pop("fund")
    d["scheme"] = fund["scheme"]
    d["folio"] = fund["folio"]
    d["isin"] = fund["isin"]
    d["fund_type"] = fund["type"]
    d["acquisition_value"] = entry.acquisition_value
    d["advisor"] = advisor

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


def _serialize_gift(entry, advisor: Optional[str]) -> dict:
    d = asdict(entry)
    fund = d.pop("fund")
    d["scheme"] = fund["scheme"]
    d["folio"] = fund["folio"]
    d["isin"] = fund["isin"]
    d["advisor"] = advisor
    return _jsonable(d)


def _advisor_map(cas_data: dict) -> dict[tuple[str, str], Optional[str]]:
    """(folio, isin) -> advisor, from the already-serialised cas_data dict
    (same one saved as cas_data.json) rather than the live pydantic object,
    so this doesn't need to know that object's shape too."""
    out = {}
    for folio in cas_data.get("folios", []):
        for scheme in folio.get("schemes", []):
            out[(folio.get("folio"), scheme.get("isin"))] = scheme.get("advisor")
    return out


def compute_and_save_gains(parsed_cas_data, cas_data: dict) -> Optional[str]:
    """Run CapitalGainsReport against the live pydantic object, serialise
    and persist the result. Returns a gains_error message (or None) the
    same way the archived casparser-web backend surfaced it — a non-fatal
    "some schemes couldn't be included" or "wrong statement type" note,
    not an exception, since a gains failure shouldn't block the rest of
    the app from working."""
    advisor_by_fund = _advisor_map(cas_data)
    gains_error = None
    gains: list[dict] = []
    gifts: list[dict] = []
    try:
        report = CapitalGainsReport(parsed_cas_data)
        for g in report.gains:
            advisor = advisor_by_fund.get((g.fund.folio, g.fund.isin))
            gains.append(_serialize_gain(g, advisor))
        for gift in report.gifts:
            advisor = advisor_by_fund.get((gift.fund.folio, gift.fund.isin))
            gifts.append(_serialize_gift(gift, advisor))
        if report.has_error():
            gains_error = "Some schemes couldn't be included: " + "; ".join(
                f"{name} — {msg}" for name, msg in report.errors
            )
    except IncompleteCASError:
        gains_error = (
            "This looks like a Summary statement rather than a Detailed one — gains "
            "need the full transaction history. Re-download the Detailed CAS and try again."
        )
    except GainsError as exc:
        gains_error = f"Couldn't compute gains: {exc}"

    path = _gains_data_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump({"gains": gains, "gifts": gifts, "gains_error": gains_error}, fp, indent=2, ensure_ascii=False)
    return gains_error


def clear_gains() -> None:
    """Called when a pre-parsed JSON is uploaded instead of a PDF — no live
    pydantic object exists to compute gains from, and any gains persisted
    from a *previous* PDF upload would now describe the wrong statement."""
    path = _gains_data_path()
    if path.exists():
        path.unlink()


def load_gains() -> Optional[dict]:
    path = _gains_data_path()
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)
