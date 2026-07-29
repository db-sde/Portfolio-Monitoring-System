"""
PortfolioIQ — xirr_engine.py

Money-weighted XIRR (spec section 9.5). Cash flows in/out of this module
are Decimal (spec 5.3) — the iterative brentq solve itself works in
float internally (XIRR has no closed form; a numerical root-finder is
float-based regardless of how precisely the inputs are held), and the
final rate is rounded only at the very end, never re-truncated mid-solve.

Fixes two real bugs the old float-based calculations.py had (spec
section 13/20's XIRR_NO_SOLUTION requirement):
  - A near-zero solved rate could print as "-0.00%" (a solved rate of
    e.g. -0.00004 rounds to -0.00 at 2dp, which reads as a negative
    zero — worse than just "0.00%"). Now explicitly zeroed when the
    rounded magnitude is 0.
  - A same-date-only cash-flow set (e.g. a same-day purchase + valuation)
    has zero time value, so f_low/f_high in the bisection are numerically
    identical regardless of rate and never bracket a root — this returned
    None before too, but silently; now it's explicit (XIRR_NO_SOLUTION).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from scipy.optimize import brentq

ZERO_DISPLAY_THRESHOLD = Decimal("0.005")  # anything rounding to 0.00% at 2dp


@dataclass
class XirrOutcome:
    value: Optional[Decimal]  # percentage, e.g. Decimal("14.20") for 14.2%; None if XIRR_NO_SOLUTION
    reason: Optional[str] = None  # set when value is None


def _npv(rate: float, cashflows: list[tuple[date, float]]) -> float:
    if rate <= -1.0:
        return float("inf")
    t0 = cashflows[0][0]
    return sum(cf / (1.0 + rate) ** ((d - t0).days / 365.0) for d, cf in cashflows)


def xirr(cashflows: list[tuple[date, Decimal]]) -> XirrOutcome:
    """cashflows: list of (date, signed_amount) — negative for money out
    of the investor's pocket, positive for money in (spec 9.5's sign
    table), Decimal amounts. Combine same-date flows before calling this
    (spec 9.5: "Combine same-date cash flows before solving")."""
    if len(cashflows) < 2:
        return XirrOutcome(None, "XIRR_NO_SOLUTION: fewer than 2 cash flows")

    float_flows = [(d, float(cf)) for d, cf in cashflows]
    if not (any(cf > 0 for _, cf in float_flows) and any(cf < 0 for _, cf in float_flows)):
        return XirrOutcome(None, "XIRR_NO_SOLUTION: requires at least one negative and one positive flow")

    float_flows = sorted(float_flows, key=lambda x: x[0])
    if float_flows[0][0] == float_flows[-1][0]:
        # Every flow lands on the same date -> zero time separation ->
        # the NPV equation is sum(cashflows)/(1+rate)^0 = sum(cashflows),
        # constant at every rate. brentq still "converges" on a flat-zero
        # function, but to an arbitrary boundary value with no actual
        # meaning (observed: -99.99% for a same-day purchase+valuation
        # that nets to exactly 0) — there's no time-value information
        # here to solve XIRR from at all, so this must be explicit.
        return XirrOutcome(None, "XIRR_NO_SOLUTION: all cash flows share one date, no time separation to solve from")
    low, high = -0.9999, 10.0
    try:
        f_low = _npv(low, float_flows)
        f_high = _npv(high, float_flows)
        attempts = 0
        while f_low * f_high > 0 and attempts < 50:
            high *= 2
            f_high = _npv(high, float_flows)
            attempts += 1
        if f_low * f_high > 0:
            return XirrOutcome(None, "XIRR_NO_SOLUTION: no bracketed root found (often same-date-only flows)")
        rate = brentq(_npv, low, high, args=(float_flows,), maxiter=1000)
    except (ValueError, RuntimeError, OverflowError, ZeroDivisionError) as exc:
        return XirrOutcome(None, f"XIRR_NO_SOLUTION: solver error ({exc})")

    pct = Decimal(str(rate * 100)).quantize(Decimal("0.01"))
    if abs(pct) < ZERO_DISPLAY_THRESHOLD:
        pct = Decimal("0.00")  # kill the "-0.00%" display bug
    return XirrOutcome(pct)
