"""The suites a run can ask: every runnable golden case, or a subset from suites.yaml."""

from functools import cache
from pathlib import Path
from typing import Literal, get_args

import yaml

Suite = Literal["full", "canary", "standby"]
SUITES: tuple[Suite, ...] = get_args(Suite)
SUITES_FILE = Path(__file__).with_name("suites.yaml")


@cache
def subsets() -> dict[str, frozenset[str]]:
    data = yaml.safe_load(SUITES_FILE.read_text())
    return {name: frozenset(data[name]) for name in SUITES if name != "full"}


def chosen(suite: Suite, case_id: str) -> bool:
    return suite == "full" or case_id in subsets()[suite]
