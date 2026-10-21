"""Loads profile/me.md, the only source of facts the agent may use about you."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .safety import EMAIL_RE, normalize_link


@dataclass
class Profile:
    text: str
    links: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)


def load_profile(path: Path) -> Profile:
    if not path.exists():
        raise FileNotFoundError(f"No profile at {path}. Run `igothired setup`, then fill in profile/me.md.")
    raw = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.DOTALL)
    # Anything still marked [verify] is unconfirmed, so the model never sees it.
    lines = [line for line in raw.splitlines() if "[verify]" not in line.lower()]
    text = "\n".join(lines).strip()

    links, emails = [], []
    in_links = False
    for line in lines:
        if line.startswith("#"):
            in_links = line.lstrip("#").strip().lower() == "links"
            continue
        if not in_links or ":" not in line:
            continue
        value = line.split(":", 1)[1].strip()
        if EMAIL_RE.fullmatch(value):
            emails.append(value.lower())
        elif value:
            links.append(normalize_link(value))
    return Profile(text=text, links=links, emails=emails)


def numbers_in(text: str) -> set[str]:
    return {n.rstrip(".,") for n in re.findall(r"\d[\d,.]*", text)}
