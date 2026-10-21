from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from .paths import REPO_ROOT


class SendWindow(BaseModel):
    start_hour: int = 9
    end_hour: int = 19
    weekdays: list[int] = [0, 1, 2, 3, 4]  # Monday is 0


class Limits(BaseModel):
    invites_per_day: int = 12
    invites_per_week: int = 70
    messages_per_day: int = 25
    new_founders_per_run: int = 12
    # Random pause between LinkedIn actions, in seconds. Bursts are what get accounts flagged.
    read_delay: tuple[float, float] = (4, 12)
    write_delay: tuple[float, float] = (45, 150)
    send_window: SendWindow = SendWindow()


class Discovery(BaseModel):
    post_queries: list[str] = [
        "hiring founding engineer",
        "looking for a technical cofounder",
        "we're hiring AI engineer intern",
        "startup hiring interns San Francisco",
    ]
    date_posted: Literal["past-24h", "past-week", "past-month"] = "past-week"
    max_pages: int = 2


class Targeting(BaseModel):
    min_fit_score: int = 65
    looking_for: str = ""
    avoid: list[str] = []


class Messaging(BaseModel):
    send_connection_note: bool = True
    note_max_chars: int = 200  # 300 on Premium
    message_max_chars: int = 900
    followup_after_hours: int = 20
    give_up_after_days: int = 21
    # Free accounts get a handful of personalized invites a month.
    on_note_quota: Literal["send_without_note", "hold"] = "send_without_note"


class Autonomy(BaseModel):
    mode: Literal["review", "auto"] = "review"
    # Reply types the agent may send on its own in auto mode. Everything else waits for you.
    auto_reply_categories: list[str] = ["question_about_me", "asks_for_links", "small_talk"]


class ModelSettings(BaseModel):
    name: str = "claude-opus-5-5"
    max_concurrency: int = 6


class LinkedInServer(BaseModel):
    command: str = "uvx"
    args: list[str] = ["mcp-server-linkedin@4.27.0"]
    tool_timeout: int = 240


class SheetSettings(BaseModel):
    title: str = "Igothired"
    # Emails allowed to view or comment. Anyone with edit access could approve
    # messages from your LinkedIn, so editors other than you always block syncing.
    allowed_viewers: list[str] = []


class Config(BaseModel):
    me_name: str = "Moazzan Imran"
    profile_path: Path = Path("profile/me.md")
    targeting: Targeting = Targeting()
    discovery: Discovery = Discovery()
    limits: Limits = Limits()
    messaging: Messaging = Messaging()
    autonomy: Autonomy = Autonomy()
    model: ModelSettings = ModelSettings()
    linkedin: LinkedInServer = LinkedInServer()
    sheet: SheetSettings = SheetSettings()

    @property
    def first_name(self) -> str:
        return self.me_name.split()[0]

    def profile_file(self) -> Path:
        path = self.profile_path.expanduser()
        return path if path.is_absolute() else REPO_ROOT / path


def config_path() -> Path:
    if env := os.environ.get("IGOTHIRED_CONFIG"):
        return Path(env)
    mine = REPO_ROOT / "config.yaml"
    return mine if mine.exists() else REPO_ROOT / "config.example.yaml"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    data = yaml.safe_load(path.read_text()) if path.exists() else None
    return Config.model_validate(data or {})
