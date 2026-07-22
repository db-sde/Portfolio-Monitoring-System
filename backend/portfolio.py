"""
PortfolioIQ — portfolio.py

Ties the raw parsed CAS JSON, config.json (group/investor/ARN labels),
and enrichment data together into the shapes main.py's endpoints return.

`invested_value` (per scheme) is the gross total ever contributed —
sum of every PURCHASE/PURCHASE_SIP/SWITCH_IN(_MERGER) amount, not netted
against redemptions already taken out. That's deliberate: it's what lets
a fully-redeemed, zero-value scheme still show "you put in ₹X, this is
what happened to it" instead of just disappearing into a 0/0 line, which
is the whole point of keeping the 14 zero-value schemes visible at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import calculations as calc
import config_manager as cfgm

MONEY_OUT_TYPES = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER"}


def _num(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


_DEFAULT_CAS_DATA_PATH = Path(__file__).resolve().parent / "cas_data.json"


def _cas_data_path() -> Path:
    # See config_manager._config_path — same cwd-independence reasoning.
    return Path(os.environ.get("CAS_DATA_PATH", str(_DEFAULT_CAS_DATA_PATH)))


def load_cas_data() -> Optional[dict]:
    path = _cas_data_path()
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def save_cas_data(data: dict) -> None:
    path = _cas_data_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, indent=2, ensure_ascii=False)


def _mode_from_name(scheme_name: str) -> str:
    return "Direct" if "direct" in (scheme_name or "").lower() else "Regular"


def build_scheme_records(
    cas_data: dict,
    config: dict,
    enrichment_map: Optional[dict[str, dict]] = None,
) -> list[dict]:
    """One record per scheme, flattened out of folios, with every derived
    field the API responses need already computed."""
    enrichment_map = enrichment_map or {}
    records: list[dict] = []

    for folio in cas_data.get("folios", []):
        for scheme in folio.get("schemes", []):
            transactions = scheme.get("transactions") or []
            valuation = scheme.get("valuation") or {}
            current_value = _num(valuation.get("value"))
            current_nav = _num(valuation.get("nav"))
            cost_value = _num(valuation.get("cost"))
            current_units = _num(scheme.get("close"))
            valuation_date = valuation.get("date")

            invested_value = sum(
                abs(_num(t.get("amount")))
                for t in transactions
                if t.get("type") in MONEY_OUT_TYPES
            )
            # External money only (no switch_in): switches move the same
            # rupee between two schemes, so summing invested_value across
            # schemes double-counts it. Anything aggregated across schemes
            # (portfolio/advisor totals, dashboard stat cards) should use
            # this instead; a single scheme's own invested_value stays
            # gross on purpose (see module docstring).
            invested_value_external = sum(
                abs(_num(t.get("amount")))
                for t in transactions
                if t.get("type") in ("PURCHASE", "PURCHASE_SIP")
            )
            absolute_gain = current_value - invested_value
            absolute_gain_pct = (
                round(absolute_gain / invested_value * 100, 2) if invested_value else None
            )

            xirr_value = None
            if valuation_date:
                xirr_value = calc.calculate_xirr(transactions, current_value, valuation_date)

            advisor = scheme.get("advisor")
            group_name, investor_name = cfgm.find_owner_for_arn(config, advisor) if advisor else (None, None)

            amfi = scheme.get("amfi")
            enriched_raw = dict(enrichment_map.get(amfi, {}))
            nav_history = enriched_raw.pop("_nav_history", None)
            enriched = enriched_raw or None

            records.append({
                "folio": folio.get("folio"),
                "amc": folio.get("amc"),
                "scheme_name": scheme.get("scheme"),
                "advisor": advisor,
                "advisor_label": cfgm.find_arn_label(config, advisor) if advisor else None,
                "group_name": group_name,
                "investor_name": investor_name,
                "isin": scheme.get("isin"),
                "amfi": amfi,
                "type": scheme.get("type"),
                "mode": _mode_from_name(scheme.get("scheme", "")),
                "current_units": current_units,
                "current_nav": current_nav,
                "current_value": current_value,
                "cost_value": cost_value,
                "invested_value": round(invested_value, 2),
                "invested_value_external": round(invested_value_external, 2),
                "absolute_gain": round(absolute_gain, 2),
                "absolute_gain_pct": absolute_gain_pct,
                "xirr": xirr_value,
                "valuation_date": valuation_date,
                "transactions": transactions,
                "enriched": enriched,
                "_nav_history": nav_history or [],
            })
    return records


def filter_schemes(
    records: list[dict],
    include_zero_value: bool = False,
    level: Optional[str] = None,
    group_name: Optional[str] = None,
    investor_name: Optional[str] = None,
    arn: Optional[str] = None,
) -> list[dict]:
    out = records
    if not include_zero_value:
        out = [r for r in out if r["current_value"] > 0]

    if level == "group" and group_name:
        out = [r for r in out if r.get("group_name") == group_name]
    elif level == "investor" and investor_name:
        out = [r for r in out if r.get("investor_name") == investor_name]
    elif level == "arn" and arn:
        out = [r for r in out if r.get("advisor") == arn]
    return out


def enrichment_targets(cas_data: dict) -> list[dict]:
    """Unique {amfi, isin, scheme} across the whole statement, for
    enrich_schemes() — one lookup per fund, not per folio."""
    seen: dict[str, dict] = {}
    for folio in cas_data.get("folios", []):
        for scheme in folio.get("schemes", []):
            amfi = scheme.get("amfi")
            if amfi and amfi not in seen:
                seen[amfi] = {"amfi": amfi, "isin": scheme.get("isin"), "scheme": scheme.get("scheme")}
    return list(seen.values())


# ------------------------------------------------------------- summary ----

def build_portfolio_summary(records: list[dict], config: dict) -> dict:
    """Advisor-level comparison, grouped exactly as config.json defines
    groups/investors — an ARN present in the CAS but not in config.json
    simply won't have anywhere to be attributed and is left out of this
    view (it still shows up in /api/portfolio unfiltered)."""
    groups_out = []
    for group in config.get("groups", []):
        investors_out = []
        for investor in group.get("investors", []):
            investor_name = investor.get("investor_name")
            arn_labels = investor.get("arn_labels", {})
            advisors_out = []
            all_invested = all_current = 0.0
            all_xirr_cashflows: list = []

            for arn in investor.get("arns", []):
                # Include a fully-redeemed advisor too (current_value == 0
                # but invested_value > 0) so the historical relationship
                # still shows up, not just currently-live positions.
                arn_records = [
                    r for r in records
                    if r.get("advisor") == arn and (r["current_value"] > 0 or r["invested_value"] > 0)
                ]
                if not arn_records:
                    continue
                investment_value = sum(r["invested_value_external"] for r in arn_records)
                current_value = sum(r["current_value"] for r in arn_records)
                absolute_return_pct = (
                    round((current_value - investment_value) / investment_value * 100, 2)
                    if investment_value else None
                )
                cap_alloc = calc.calculate_weighted_cap_allocation(arn_records)

                advisors_out.append({
                    "arn": arn,
                    "advisor_label": arn_labels.get(arn, arn),
                    "investment_value": round(investment_value, 2),
                    "current_value": round(current_value, 2),
                    "absolute_return_pct": absolute_return_pct,
                    "xirr": _blended_xirr(arn_records),
                    "largecap_pct": cap_alloc["largecap_pct"],
                    "midcap_pct": cap_alloc["midcap_pct"],
                    "smallcap_pct": cap_alloc["smallcap_pct"],
                    # Benchmark comparison needs a benchmark NAV history
                    # source this build doesn't have verified access to
                    # (see enrichment.py docstring) — left null rather
                    # than fabricated.
                    "portfolio_xirr": _blended_xirr(arn_records),
                    "benchmark_xirr": None,
                    "beating_benchmark": None,
                })
                all_invested += investment_value
                all_current += current_value

            all_records = [r for r in records if r.get("investor_name") == investor_name]
            investors_out.append({
                "investor_name": investor_name,
                "all_advisor_xirr": _blended_xirr(all_records) if all_records else None,
                "all_advisor_benchmark_xirr": None,
                "advisors": advisors_out,
            })
        groups_out.append({"group_name": group.get("group_name"), "investors": investors_out})
    return {"groups": groups_out}


def _blended_xirr(records: list[dict]) -> Optional[float]:
    """XIRR across a set of schemes treated as one investment: every
    scheme's own transaction cash flows, pooled, plus each scheme's
    current value as a final inflow on its own valuation date."""
    cashflows = []
    for r in records:
        for t in r["transactions"]:
            cf = calc._txn_cash_flow(t)
            if cf is None:
                continue
            d = calc._parse_date(t.get("date"))
            if d is not None:
                cashflows.append((d, cf))
        if r["current_value"] and r.get("valuation_date"):
            d = calc._parse_date(r["valuation_date"])
            if d is not None:
                cashflows.append((d, r["current_value"]))
    return calc.xirr(cashflows) if cashflows else None


def build_fund_summary(records: list[dict]) -> dict:
    """Heatmap of trailing returns for every fund currently held. (A
    fuller version showing funds NOT held too — "is_held": false rows for
    comparison — would need a broader fund catalog beyond what's in this
    statement; out of scope for Phase 1 without that catalog source.)"""
    seen: dict[str, dict] = {}
    for r in records:
        amfi = r.get("amfi")
        if not amfi or amfi in seen or r["current_value"] <= 0:
            continue
        enriched = r.get("enriched") or {}
        seen[amfi] = {
            "scheme_name": r["scheme_name"],
            "amfi": amfi,
            "is_held": True,
            "corpus_cr": enriched.get("corpus_cr"),
            "largecap_pct": enriched.get("largecap_pct"),
            "midcap_pct": enriched.get("midcap_pct"),
            "smallcap_pct": enriched.get("smallcap_pct"),
            "returns": enriched.get("returns") or {"1m": None, "3m": None, "6m": None, "1y": None, "2y": None, "3y": None},
        }
    return {"funds": list(seen.values())}


def build_transactions(records: list[dict]) -> list[dict]:
    """Flat transaction ledger across every scheme in `records`, each row
    tagged with the scheme/folio/advisor context needed to filter or group
    it in the UI without a second lookup. Newest first."""
    out: list[dict] = []
    for r in records:
        for t in r["transactions"]:
            out.append({
                "date": t.get("date"),
                "type": t.get("type"),
                "description": t.get("description"),
                "amount": _num(t.get("amount")) if t.get("amount") is not None else None,
                "units": _num(t.get("units")) if t.get("units") is not None else None,
                "nav": _num(t.get("nav")) if t.get("nav") is not None else None,
                "balance": _num(t.get("balance")) if t.get("balance") is not None else None,
                "folio": r["folio"],
                "scheme_name": r["scheme_name"],
                "isin": r["isin"],
                "amfi": r["amfi"],
                "advisor": r["advisor"],
                "advisor_label": r["advisor_label"],
                "group_name": r["group_name"],
                "investor_name": r["investor_name"],
            })
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out


def build_exposure(records: list[dict]) -> dict:
    held = [r for r in records if r["current_value"] > 0]
    total_value = sum(r["current_value"] for r in held) or 1.0

    by_amc: dict[str, float] = {}
    for r in held:
        by_amc[r["amc"]] = by_amc.get(r["amc"], 0.0) + r["current_value"]
    top_amcs = sorted(
        ({"amc_name": k, "current_value": round(v, 2), "pct_of_portfolio": round(v / total_value * 100, 2)}
         for k, v in by_amc.items()),
        key=lambda x: -x["current_value"],
    )

    top_funds = sorted(
        ({"scheme_name": r["scheme_name"], "current_value": r["current_value"],
          "pct_of_portfolio": round(r["current_value"] / total_value * 100, 2)} for r in held),
        key=lambda x: -x["current_value"],
    )

    cap = calc.calculate_weighted_cap_allocation(held)
    lc, mc, sc = cap["largecap_pct"] or 0, cap["midcap_pct"] or 0, cap["smallcap_pct"] or 0
    other_pct = round(max(0.0, 100.0 - lc - mc - sc), 2) if cap["largecap_pct"] is not None else None

    return {
        "top_amcs": top_amcs,
        "top_funds": top_funds,
        "cap_allocation": {**cap, "other_pct": other_pct},
    }
