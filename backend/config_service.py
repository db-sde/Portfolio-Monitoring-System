"""
PortfolioIQ — config_service.py

DB-backed replacement for config_manager.py's config.json: groups ->
investors -> ARNs/labels, plus key/value preferences. Returns/accepts
the exact same dict shape the old JSON file had (groups: [{group_name,
investors: [{investor_name, arns: [...], arn_labels: {...}}]}],
preferences: {...}) so Settings.jsx and every consumer of this shape
needs zero changes — only the storage moved off Render's ephemeral disk.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import ConfigGroup, ConfigInvestor, ConfigInvestorArn, Preference

DEFAULT_PREFERENCES = {
    "show_zero_value_funds": False,
    "primary_benchmark": "Nifty 500",
    "show_benchmark_comparison": True,
}


def load_config(session: Session) -> dict:
    groups_out = []
    for group in session.execute(select(ConfigGroup).order_by(ConfigGroup.sort_order, ConfigGroup.id)).scalars():
        investors_out = []
        for investor in sorted(group.investors, key=lambda i: (i.sort_order, i.id)):
            arn_labels = {a.arn: (a.label or a.arn) for a in investor.arns}
            investors_out.append({
                "investor_name": investor.investor_name,
                "arns": [a.arn for a in investor.arns],
                "arn_labels": arn_labels,
            })
        groups_out.append({"group_name": group.group_name, "investors": investors_out})

    prefs = {p.key: p.value for p in session.execute(select(Preference)).scalars()}
    preferences = {**DEFAULT_PREFERENCES, **prefs}
    return {"groups": groups_out, "preferences": preferences}


def save_config(session: Session, config: dict) -> None:
    """Full replace, same semantics as the old save_config(): the
    Settings page always PUTs its complete edited tree, so the simplest
    correct implementation is delete-and-recreate rather than diffing."""
    for group in list(session.execute(select(ConfigGroup)).scalars()):
        session.delete(group)
    session.flush()

    for gi, group in enumerate(config.get("groups", [])):
        group_row = ConfigGroup(group_name=group.get("group_name", ""), sort_order=gi)
        session.add(group_row)
        session.flush()
        for ii, investor in enumerate(group.get("investors", [])):
            investor_row = ConfigInvestor(
                group_id=group_row.id, investor_name=investor.get("investor_name", ""), sort_order=ii,
            )
            session.add(investor_row)
            session.flush()
            arn_labels = investor.get("arn_labels", {})
            for arn in investor.get("arns", []):
                session.add(ConfigInvestorArn(investor_id=investor_row.id, arn=arn, label=arn_labels.get(arn)))

    for key, value in config.get("preferences", {}).items():
        pref = session.get(Preference, key)
        if pref:
            pref.value = value
        else:
            session.add(Preference(key=key, value=value))
    session.flush()


def find_arn_label(config: dict, arn: str) -> Optional[str]:
    for group in config.get("groups", []):
        for investor in group.get("investors", []):
            label = investor.get("arn_labels", {}).get(arn)
            if label:
                return label
    return None


def find_owner_for_arn(config: dict, arn: str) -> tuple[Optional[str], Optional[str]]:
    for group in config.get("groups", []):
        for investor in group.get("investors", []):
            if arn in investor.get("arns", []):
                return group.get("group_name"), investor.get("investor_name")
    return None, None
