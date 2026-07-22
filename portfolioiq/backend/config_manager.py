"""
PortfolioIQ — config_manager.py

Read/write config.json: groups -> investors -> ARNs/labels, and display
preferences. A thin wrapper, not a database — this is a single-investor
personal tool, so a JSON file is genuinely enough.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG = {
    "groups": [],
    "preferences": {
        "show_zero_value_funds": False,
        "primary_benchmark": "Nifty 500",
        "show_benchmark_comparison": True,
    },
}


_DEFAULT_PATH = Path(__file__).resolve().parent / "config.json"


def _config_path() -> Path:
    # Default resolves relative to this file, not the process's cwd —
    # a bare "./config.json" would silently look in the wrong place
    # depending on how/where uvicorn was launched from (e.g. a container
    # WORKDIR that doesn't match this file's own directory).
    return Path(os.environ.get("CONFIG_PATH", str(_DEFAULT_PATH)))


def load_config() -> dict:
    path = _config_path()
    if not path.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def save_config(config: dict) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(config, fp, indent=2, ensure_ascii=False)


def find_arn_label(config: dict, arn: str) -> Optional[str]:
    for group in config.get("groups", []):
        for investor in group.get("investors", []):
            label = investor.get("arn_labels", {}).get(arn)
            if label:
                return label
    return None


def find_owner_for_arn(config: dict, arn: str) -> tuple[Optional[str], Optional[str]]:
    """(group_name, investor_name) for whichever config entry claims this
    ARN, or (None, None) if it isn't in config.json yet."""
    for group in config.get("groups", []):
        for investor in group.get("investors", []):
            if arn in investor.get("arns", []):
                return group.get("group_name"), investor.get("investor_name")
    return None, None


def all_known_arns(config: dict) -> set[str]:
    return {
        arn
        for group in config.get("groups", [])
        for investor in group.get("investors", [])
        for arn in investor.get("arns", [])
    }
