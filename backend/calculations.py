"""
PortfolioIQ — calculations.py

XIRR, benchmark-relative XIRR, period snapshots, and weighted cap
allocation. No network calls or file I/O here — everything is pure
functions over already-loaded data, so it can be unit tested without a
running server.

Cash-flow sign convention used throughout (spec section 8):
  money OUT of the investor's pocket (into the fund)  -> negative
  money IN to the investor's pocket (out of the fund)  -> positive

The spec's own transaction table only lists the 9 types actually present
in one real CAS file; casparser's full TransactionType enum has more
(SWITCH_IN_MERGER, SWITCH_OUT_MERGER, DIVIDEND_PAYOUT, TDS_TAX,
SEGREGATION, GIFT_IN, GIFT_OUT, MISC, UNKNOWN). Those are mapped here
too, following the same logic, so a re-uploaded statement with a type
Swanand's file happens not to have doesn't silently corrupt XIRR.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from scipy.optimize import brentq

MONEY_OUT_TYPES = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER"}
MONEY_IN_TYPES = {"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER", "DIVIDEND_PAYOUT"}
# Units change with no offsetting cash flow (reinvested dividends), pure
# fees the investor never sees as cash (stamp duty/STT/TDS), or ledger
# adjustments that aren't a real economic cash flow (segregation, gifts,
# unclassified rows). None of these belong in an XIRR cash-flow series.
SKIP_TYPES = {
    "DIVIDEND_REINVEST", "STAMP_DUTY_TAX", "STT_TAX", "TDS_TAX",
    "SEGREGATION", "GIFT_IN", "GIFT_OUT", "MISC", "UNKNOWN",
}

CAP_TYPES = ("EQUITY", "HYBRID", "DEBT")


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _txn_cash_flow(txn: dict) -> Optional[float]:
    """Signed cash flow for one transaction, or None if it shouldn't be
    part of an XIRR series at all."""
    ttype = txn.get("type")
    amount = _num(txn.get("amount"))
    if amount is None:
        return None
    if ttype in MONEY_OUT_TYPES:
        return -abs(amount)
    if ttype in MONEY_IN_TYPES:
        return abs(amount)
    if ttype == "REVERSAL":
        # "Reverse the original transaction sign" (spec): whatever sign the
        # ledger recorded the amount with, flip it.
        return -amount
    if ttype in SKIP_TYPES:
        return None
    # Unrecognised type: don't guess at a sign for real money.
    return None


def _npv(rate: float, cashflows: list[tuple[date, float]]) -> float:
    if rate <= -1.0:
        return float("inf")
    t0 = cashflows[0][0]
    return sum(cf / (1.0 + rate) ** ((d - t0).days / 365.0) for d, cf in cashflows)


def xirr(cashflows: list[tuple[date, float]]) -> Optional[float]:
    """Solve for the XIRR of a list of (date, signed_amount) cash flows.
    Returns a percentage (14.2 for 14.2%), or None if it can't be solved
    (fewer than 2 flows, all one sign, or no root in a sane range)."""
    if len(cashflows) < 2:
        return None
    if not (any(cf > 0 for _, cf in cashflows) and any(cf < 0 for _, cf in cashflows)):
        return None

    cashflows = sorted(cashflows, key=lambda x: x[0])
    low, high = -0.9999, 10.0
    try:
        f_low = _npv(low, cashflows)
        f_high = _npv(high, cashflows)
        attempts = 0
        while f_low * f_high > 0 and attempts < 50:
            high *= 2
            f_high = _npv(high, cashflows)
            attempts += 1
        if f_low * f_high > 0:
            return None
        rate = brentq(_npv, low, high, args=(cashflows,), maxiter=1000)
        return rate * 100
    except (ValueError, RuntimeError, OverflowError, ZeroDivisionError):
        return None


def calculate_xirr(
    transactions: list[dict], current_value: float, current_date: str
) -> Optional[float]:
    """Per-scheme XIRR: every real cash flow in the transaction ledger,
    plus the current value as a final inflow today."""
    cashflows: list[tuple[date, float]] = []
    for txn in transactions:
        cf = _txn_cash_flow(txn)
        if cf is None:
            continue
        d = _parse_date(txn.get("date"))
        if d is None:
            continue
        cashflows.append((d, cf))

    if not cashflows:
        return None

    end = _parse_date(current_date)
    if end is None:
        return None
    cashflows.append((end, float(current_value)))
    return xirr(cashflows)


def calculate_benchmark_xirr(
    transactions: list[dict],
    benchmark_nav_history: list[dict],
    current_benchmark_nav: float,
) -> Optional[float]:
    """Replay the same transaction dates/amounts into the benchmark's own
    NAV history: buy/sell hypothetical benchmark units on each real
    transaction date, then value the resulting unit balance at
    `current_benchmark_nav` as of the most recent date in the supplied
    history (that history's own "as of" date — the spec doesn't pass a
    separate current_date for this call)."""
    nav_by_date: dict[date, float] = {}
    for entry in benchmark_nav_history:
        d = _parse_date(entry.get("date"))
        nav = _num(entry.get("nav"))
        if d is not None and nav:
            nav_by_date[d] = nav
    if not nav_by_date:
        return None

    sorted_dates = sorted(nav_by_date)
    as_of_date = sorted_dates[-1]

    def nav_on_or_before(d: date) -> float:
        candidates = [dt for dt in sorted_dates if dt <= d]
        return nav_by_date[max(candidates)] if candidates else nav_by_date[sorted_dates[0]]

    units = 0.0
    cashflows: list[tuple[date, float]] = []
    for txn in transactions:
        cf = _txn_cash_flow(txn)
        if cf is None:
            continue
        d = _parse_date(txn.get("date"))
        if d is None:
            continue
        nav = nav_on_or_before(d)
        if not nav:
            continue
        units += (-cf) / nav  # cf<0 (invested) buys units, cf>0 (withdrawn) sells
        cashflows.append((d, cf))

    if not cashflows or units <= 0:
        return None

    final_value = units * float(current_benchmark_nav)
    cashflows.append((as_of_date, final_value))
    return xirr(cashflows)


def _scheme_cap_type(scheme_type: Optional[str]) -> Optional[str]:
    t = (scheme_type or "").upper()
    return t if t in CAP_TYPES else None


def _units_at(transactions: list[dict], as_of: Optional[date]) -> float:
    """Reconstruct the unit balance as of a date, from the transaction
    ledger's own unit deltas (purchases/redemptions/switches change
    units; tax rows have units=null and don't touch the balance)."""
    if as_of is None:
        return 0.0
    total = 0.0
    for txn in transactions:
        d = _parse_date(txn.get("date"))
        units = _num(txn.get("units"))
        if d is None or units is None:
            continue
        if d <= as_of:
            total += units
    return total


def _nav_at(nav_history: Optional[list[dict]], as_of: Optional[date]) -> Optional[float]:
    """Nearest available NAV on or before `as_of` from a scheme's own
    historical NAV series (sourced from mfapi.in by the enrichment layer).
    Returns None if no history was supplied — the caller decides the
    fallback (typically 0, with the gap surfaced rather than guessed at)."""
    if not nav_history or as_of is None:
        return None
    candidates = [
        (d, nav) for d, nav in (
            (_parse_date(e.get("date")), _num(e.get("nav"))) for e in nav_history
        )
        if d is not None and nav is not None and d <= as_of
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]


def _period_bucket(
    transactions: list[dict], start: Optional[date], end: date
) -> dict:
    bucket = {"purchase": 0.0, "switch_in": 0.0, "switch_out": 0.0, "div_payout": 0.0, "redemption": 0.0}
    type_key = {
        "PURCHASE": "purchase", "PURCHASE_SIP": "purchase",
        "SWITCH_IN": "switch_in", "SWITCH_IN_MERGER": "switch_in",
        "SWITCH_OUT": "switch_out", "SWITCH_OUT_MERGER": "switch_out",
        "DIVIDEND_PAYOUT": "div_payout",
        "REDEMPTION": "redemption",
    }
    for txn in transactions:
        d = _parse_date(txn.get("date"))
        amount = _num(txn.get("amount"))
        if d is None or amount is None:
            continue
        if start is not None and d <= start:
            continue
        if d > end:
            continue
        key = type_key.get(txn.get("type"))
        if key:
            bucket[key] += abs(amount)
    return bucket


def calculate_snapshot(
    schemes: list[dict],
    start_date: Optional[str],
    end_date: str,
    nav_history_by_amfi: Optional[dict[str, list[dict]]] = None,
) -> dict:
    """
    schemes: enriched scheme dicts (folio/scheme/type/transactions/
             current_value/amfi at minimum)
    start_date: None = since inception (opening balance is always 0)
    end_date: "YYYY-MM-DD"
    nav_history_by_amfi: optional {amfi_code: [{date, nav}, ...]} from
             enrichment.py, used to value opening/closing balances on
             dates other than the statement's own valuation date.

    Buckets are EQUITY / HYBRID / DEBT + total. A scheme whose type isn't
    one of those three (casparser's real data only ever emits EQUITY,
    DEBT, or an unresolved "N/A" — never HYBRID) is still counted in
    "total" but can't be attributed to a cap bucket, so it's left out of
    all three rather than guessed into one.
    """
    nav_history_by_amfi = nav_history_by_amfi or {}
    start = _parse_date(start_date) if start_date else None
    end = _parse_date(end_date)

    buckets: dict[str, dict] = {k: {
        "opening_balance": 0.0, "purchase": 0.0, "switch_in": 0.0, "switch_out": 0.0,
        "div_payout": 0.0, "redemption": 0.0, "closing_balance": 0.0,
        "_cashflows": [],
    } for k in (*CAP_TYPES, "total")}

    for scheme in schemes:
        cap = _scheme_cap_type(scheme.get("type"))
        transactions = scheme.get("transactions") or []
        amfi = scheme.get("amfi")
        nav_history = nav_history_by_amfi.get(amfi)

        opening_units = _units_at(transactions, start) if start else 0.0
        opening_nav = _nav_at(nav_history, start) if start else None
        opening_balance = (opening_units * opening_nav) if (start and opening_nav) else 0.0

        is_current = end >= (_parse_date(scheme.get("valuation_date")) or end)
        if is_current:
            closing_balance = float(scheme.get("current_value") or 0.0)
        else:
            closing_units = _units_at(transactions, end)
            closing_nav = _nav_at(nav_history, end)
            closing_balance = closing_units * closing_nav if closing_nav else 0.0

        period = _period_bucket(transactions, start, end)

        targets = ["total"] + ([cap] if cap else [])
        for key in targets:
            b = buckets[key]
            b["opening_balance"] += opening_balance
            b["closing_balance"] += closing_balance
            for field in ("purchase", "switch_in", "switch_out", "div_payout", "redemption"):
                b[field] += period[field]
            # Period cash flows for this scheme's contribution to the bucket XIRR:
            # the opening balance counts as if invested right at the start of
            # the window, then every real flow in-window, then the closing
            # balance as the final inflow.
            if start and opening_balance:
                b["_cashflows"].append((start, -opening_balance))
            for txn in transactions:
                d = _parse_date(txn.get("date"))
                if d is None or (start and d <= start) or d > end:
                    continue
                cf = _txn_cash_flow(txn)
                if cf is not None:
                    b["_cashflows"].append((d, cf))
            if closing_balance:
                b["_cashflows"].append((end, closing_balance))

    result = {}
    for key, b in buckets.items():
        net_addition = b["purchase"] + b["switch_in"] - b["switch_out"] - b["redemption"]
        net_gain = b["closing_balance"] - b["opening_balance"] - net_addition
        result[key] = {
            "opening_balance": round(b["opening_balance"], 2),
            "purchase": round(b["purchase"], 2),
            "switch_in": round(b["switch_in"], 2),
            "switch_out": round(b["switch_out"], 2),
            "div_payout": round(b["div_payout"], 2),
            "redemption": round(b["redemption"], 2),
            "net_addition": round(net_addition, 2),
            "closing_balance": round(b["closing_balance"], 2),
            "net_gain": round(net_gain, 2),
            "xirr": xirr(b["_cashflows"]),
        }
    return {"start_date": start_date, "end_date": end_date, **result}


def calculate_weighted_cap_allocation(schemes: list[dict]) -> dict:
    """Value-weighted large/mid/small-cap split across held EQUITY
    schemes only. Schemes with no enrichment data (so no cap-split known)
    are excluded from the weighting entirely rather than treated as 0%,
    since 0% would silently understate the other categories."""
    total_value = 0.0
    largecap = midcap = smallcap = 0.0

    for scheme in schemes:
        if (scheme.get("type") or "").upper() != "EQUITY":
            continue
        value = _num(scheme.get("current_value")) or 0.0
        if value <= 0:
            continue
        enriched = scheme.get("enriched") or {}
        lc, mc, sc = enriched.get("largecap_pct"), enriched.get("midcap_pct"), enriched.get("smallcap_pct")
        if lc is None and mc is None and sc is None:
            continue
        total_value += value
        largecap += value * (lc or 0.0)
        midcap += value * (mc or 0.0)
        smallcap += value * (sc or 0.0)

    if total_value <= 0:
        return {"largecap_pct": None, "midcap_pct": None, "smallcap_pct": None}

    return {
        "largecap_pct": round(largecap / total_value, 2),
        "midcap_pct": round(midcap / total_value, 2),
        "smallcap_pct": round(smallcap / total_value, 2),
    }
