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
from sqlalchemy.orm import Session, selectinload
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text

from models import ConfigGroup, ConfigInvestor, ConfigInvestorArn, Preference

DEFAULT_PREFERENCES = {
    "show_zero_value_funds": False,
    "primary_benchmark": "Nifty 50",
    "show_benchmark_comparison": True,
}


def load_config(session: Session) -> dict:
    groups_out = []
    for group in session.execute(
        select(ConfigGroup)
        .options(selectinload(ConfigGroup.investors).selectinload(ConfigInvestor.arns))
        .order_by(ConfigGroup.sort_order, ConfigGroup.id)
    ).scalars():
        investors_out = []
        for investor in sorted(group.investors, key=lambda i: (i.sort_order, i.id)):
            arn_labels = {a.arn: (a.label or a.arn) for a in investor.arns}
            investors_out.append(
                {
                    "investor_name": investor.investor_name,
                    "arns": [a.arn for a in investor.arns],
                    "arn_labels": arn_labels,
                }
            )
        groups_out.append({"group_name": group.group_name, "investors": investors_out})

    prefs = {p.key: p.value for p in session.execute(select(Preference)).scalars()}
    preferences = {
        **DEFAULT_PREFERENCES,
        **{k: v for k, v in prefs.items() if not k.startswith("_")},
    }
    return {
        "groups": groups_out,
        "preferences": preferences,
        "version": prefs.get("_config_version", 0),
    }


def save_config(session: Session, config: dict) -> None:
    """Full replace, same semantics as the old save_config(): the
    Settings page always PUTs its complete edited tree, so the simplest
    correct implementation is delete-and-recreate rather than diffing."""
    session.execute(text("SELECT pg_advisory_xact_lock(74102004)"))
    current = session.get(Preference, "_config_version")
    version = current.value if current else 0
    if config.get("version", 0) != version:
        raise HTTPException(
            409, "Settings changed in another tab. Reload before saving."
        )
    config.setdefault("preferences", {})["_config_version"] = version + 1
    for group in list(session.execute(select(ConfigGroup)).scalars()):
        session.delete(group)
    session.flush()

    for gi, group in enumerate(config.get("groups", [])):
        group_row = ConfigGroup(group_name=group.get("group_name", ""), sort_order=gi)
        session.add(group_row)
        session.flush()
        for ii, investor in enumerate(group.get("investors", [])):
            investor_row = ConfigInvestor(
                group_id=group_row.id,
                investor_name=investor.get("investor_name", ""),
                sort_order=ii,
            )
            session.add(investor_row)
            session.flush()
            arn_labels = investor.get("arn_labels", {})
            for arn in investor.get("arns", []):
                session.add(
                    ConfigInvestorArn(
                        investor_id=investor_row.id, arn=arn, label=arn_labels.get(arn)
                    )
                )

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


class InvestorInput(BaseModel):
    investor_name: str = Field(min_length=1, max_length=120)
    arns: list[str] = Field(default_factory=list, max_length=500)
    arn_labels: dict[str, str] = Field(default_factory=dict)


class GroupInput(BaseModel):
    group_name: str = Field(min_length=1, max_length=120)
    investors: list[InvestorInput] = Field(default_factory=list, max_length=100)


class ConfigInput(BaseModel):
    groups: list[GroupInput] = Field(default_factory=list, max_length=100)
    preferences: dict = Field(default_factory=dict)
    version: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def unique_names_and_arns(self):
        groups, investors, arns = set(), set(), set()
        for group in self.groups:
            group.group_name = group.group_name.strip()
            if not group.group_name or group.group_name in groups:
                raise ValueError("Group names must be unique and nonempty.")
            groups.add(group.group_name)
            for investor in group.investors:
                investor.investor_name = investor.investor_name.strip()
                if not investor.investor_name or investor.investor_name in investors:
                    raise ValueError("Investor names must be unique and nonempty.")
                investors.add(investor.investor_name)
                for arn in investor.arns:
                    if not arn.strip() or arn != arn.strip() or arn in arns:
                        raise ValueError(
                            "Each ARN must have one owner and contain no surrounding spaces."
                        )
                    arns.add(arn)
        allowed = {
            "show_zero_value_funds",
            "primary_benchmark",
            "show_benchmark_comparison",
            "_config_version",
        }
        if set(self.preferences) - allowed:
            raise ValueError("Unknown preference.")
        for key in ("show_zero_value_funds", "show_benchmark_comparison"):
            if key in self.preferences and not isinstance(self.preferences[key], bool):
                raise ValueError("Display preferences must be boolean.")
        if self.preferences.get("primary_benchmark", "Nifty 50") not in (
            "Nifty 50",
            "Nifty 500",
        ):
            raise ValueError("Choose a supported benchmark.")
        return self
