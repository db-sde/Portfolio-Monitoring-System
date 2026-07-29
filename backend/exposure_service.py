"""
PortfolioIQ — exposure_service.py

Top AMC/fund concentration and cap allocation (spec section 15) — every
number here comes from portfolio_service.HoldingMetrics.current_value
(CAS units x live MFAPI NAV), never scheme.valuation.value (spec 15.1:
"Do not use CAS valuation values in any exposure denominator or
numerator").

Cap allocation stays explicitly unavailable rather than inferred from
scheme category (spec 15.1/22) — confirmed this session that no free
source publishes real portfolio-holdings/cap-allocation data, so this
is an honest gap, not a missing feature to silently approximate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from portfolio_service import HoldingMetrics

ZERO = Decimal("0")


@dataclass
class AmcExposure:
    amc_name: str
    current_value: Decimal
    pct_of_portfolio: Decimal


@dataclass
class FundExposure:
    scheme_name: str
    current_value: Decimal
    pct_of_portfolio: Decimal


@dataclass
class CapAllocation:
    largecap_pct: Optional[Decimal]
    midcap_pct: Optional[Decimal]
    smallcap_pct: Optional[Decimal]
    other_pct: Optional[Decimal]
    status: str  # "ok" | "unavailable"


@dataclass
class ExposureResult:
    top_amcs: list[AmcExposure]
    top_funds: list[FundExposure]
    cap_allocation: CapAllocation


def compute_exposure(holdings: list[HoldingMetrics]) -> ExposureResult:
    held = [h for h in holdings if h.balance_units > 0]
    total_value = sum((h.current_value for h in held), ZERO) or Decimal("1")

    by_amc: dict[str, Decimal] = {}
    for h in held:
        by_amc[h.amc] = by_amc.get(h.amc, ZERO) + h.current_value
    top_amcs = sorted(
        (AmcExposure(amc_name=amc, current_value=v, pct_of_portfolio=(v / total_value * 100).quantize(Decimal("0.01")))
         for amc, v in by_amc.items()),
        key=lambda a: -a.current_value,
    )

    top_funds = sorted(
        (FundExposure(
            scheme_name=h.scheme_name, current_value=h.current_value,
            pct_of_portfolio=(h.current_value / total_value * 100).quantize(Decimal("0.01")),
        ) for h in held),
        key=lambda f: -f.current_value,
    )

    # Cap allocation (large/mid/small %) requires actual portfolio
    # holdings/sector data no free source provides (confirmed this
    # session against captnemo/Kuvera's own documented schema) — always
    # unavailable today, never inferred from a scheme's category label.
    cap_allocation = CapAllocation(
        largecap_pct=None, midcap_pct=None, smallcap_pct=None, other_pct=None, status="unavailable",
    )

    return ExposureResult(top_amcs=top_amcs, top_funds=top_funds, cap_allocation=cap_allocation)
