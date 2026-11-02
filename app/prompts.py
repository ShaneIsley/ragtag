"""Prompts live in prompts/*.yaml, versioned, so the app and the evaluation suite share them."""

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


@dataclass(frozen=True)
class Prompt:
    version: str
    system: str
    user: str


@cache
def load_prompt(name: str) -> Prompt:
    data = yaml.safe_load((PROMPTS_DIR / f"{name}.yaml").read_text())
    return Prompt(version=data["version"], system=data["system"], user=data["user"])
