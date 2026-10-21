"""Checks that run on every outgoing message, plus helpers for handling text we didn't write."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b(?:[a-z0-9-]+\.)+(?:com|io|ai|co|dev|org|net|app|xyz|me|so|gg|edu|ly|sh)(?:/\S*)?",
    re.IGNORECASE,
)
# LinkedIn refuses tabs and most control characters in messages.
CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f\u0085  ]")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_%-]{1,99}$")

# Phrases that make a message read like a template. From the /human and voice rules.
TEMPLATE_TELLS = [
    "i came across your profile",
    "i hope this finds you",
    "hope you're doing well",
    "hope you are doing well",
    "passionate about",
    "i'd love to connect",
    "reaching out because",
    "i'd love to pick your brain",
    "delve",
    "leverage",
    "synergy",
    "game-changer",
    "game changer",
    "excited to",
    "impressive background",
    "love what you're building",
    "testament to",
    "in today's fast-paced",
    "i believe i would be a great fit",
    "great fit for your team",
]


@dataclass
class Check:
    errors: list[str] = field(default_factory=list)  # can't be sent as is
    warnings: list[str] = field(default_factory=list)  # can be sent, but only after you've looked

    @property
    def clean(self) -> bool:
        return not self.errors and not self.warnings

    def all(self) -> list[str]:
        return self.errors + self.warnings


def normalize_link(url: str) -> str:
    url = url.strip().rstrip(".,)").lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.rstrip("/")


def find_links(text: str) -> tuple[list[str], list[str]]:
    emails = EMAIL_RE.findall(text)
    without_emails = EMAIL_RE.sub(" ", text)
    return URL_RE.findall(without_emails), emails


def check_message(
    text: str,
    *,
    max_chars: int,
    allowed_links: list[str],
    allowed_emails: list[str],
    known_numbers: set[str],
) -> Check:
    check = Check()
    body = text.strip()
    if not body:
        check.errors.append("empty")
        return check
    if len(body) > max_chars:
        check.errors.append(f"{len(body)} characters, limit is {max_chars}")
    if CONTROL_RE.search(body):
        check.errors.append("has tabs or control characters LinkedIn won't accept")

    links, emails = find_links(body)
    for link in links:
        norm = normalize_link(link)
        if not any(norm == ok or norm.startswith(ok + "/") for ok in allowed_links):
            check.warnings.append(f"link that isn't in your profile: {link}")
    for email in emails:
        if email.lower() not in allowed_emails:
            check.warnings.append(f"email that isn't yours: {email}")

    lower = body.lower().replace("’", "'")
    if "—" in body or "–" in body:
        check.warnings.append("has an em dash")
    for phrase in TEMPLATE_TELLS:
        if phrase in lower:
            check.warnings.append(f'reads templated: "{phrase}"')
    if re.search(r"\bnot (?:just|only)\b[^.]*\bbut\b", lower) or re.search(r"\bit'?s not [^.,]+, it'?s\b", lower):
        check.warnings.append('"not X but Y" construction')
    if body.count("!") > 1:
        check.warnings.append("more than one exclamation mark")
    if EMOJI_RE.search(body):
        check.warnings.append("has an emoji")
    if re.search(r"(?:^|\s)#\w", body):
        check.warnings.append("has a hashtag")
    if "[" in body and "]" in body:
        check.warnings.append("has a [placeholder] to fill in")

    # Small numbers ("a 15 minute call", "2nd place") are rarely claims worth checking.
    for number in {n.rstrip(".,") for n in re.findall(r"\d[\d,.]*", body)}:
        if len(number) > 2 and number not in known_numbers:
            check.warnings.append(f"number {number} isn't in your profile or their post")
    return check


def untrusted(source: str, text: str, limit: int = 15000) -> str:
    """Fence off text written by other people so the model treats it as data.

    Founders' posts, profiles and messages can contain anything, including
    instructions aimed at AI tools. The fence can't be closed from inside.
    """
    text = (text or "").strip()[:limit]
    text = re.sub(r"</?\s*untrusted", "[tag removed]", text, flags=re.IGNORECASE)
    return f'<untrusted source="{source}">\n{text}\n</untrusted>'


def username_from(value: str) -> str | None:
    """Pull a LinkedIn username out of a profile URL or path, or None if it doesn't look like one."""
    value = (value or "").strip()
    match = re.search(r"/in/([^/?#\s]+)", value)
    slug = (match.group(1) if match else value).strip("/")
    return slug if SLUG_RE.match(slug) else None


def profile_url(username: str) -> str:
    return f"https://www.linkedin.com/in/{username}/"


def cell(value: object, limit: int = 45000) -> object:
    """Prepare a value for the sheet. Strings are always written RAW, never as formulas."""
    if value is None:
        return ""
    if isinstance(value, (bool, int, float)):
        return value
    return CONTROL_RE.sub(" ", str(value).replace("\t", " "))[:limit]
