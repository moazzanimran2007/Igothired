"""Talks to LinkedIn through stickerdaniel/linkedin-mcp-server.

The server drives a real browser with your saved LinkedIn session. This
wrapper paces every call, logs it, and turns the server's errors into either a
per-item failure or a pause for the whole run (rate limits, restrictions,
expired sessions).
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from .config import Config
from .db import Store
from .paths import LOG_DIR, ensure_home, search_path


class LinkedInError(Exception):
    """One call failed. The run carries on with the next item."""


class LinkedInPaused(LinkedInError):
    """LinkedIn wants us to stop. The run stops touching LinkedIn."""

    def __init__(self, reason: str, retry_after: timedelta | None = None):
        super().__init__(reason)
        self.retry_after = retry_after


def classify_error(text: str) -> LinkedInError:
    low = text.lower()
    if "rate limit" in low:
        wait = re.search(r"wait (\d+) seconds", low)
        seconds = max(int(wait.group(1)) if wait else 0, 1800)
        return LinkedInPaused("LinkedIn rate limit. Backing off.", timedelta(seconds=seconds))
    if "restricted" in low:
        return LinkedInPaused("LinkedIn says the account is restricted. Check linkedin.com before resuming.")
    if "--login" in low or "session expired" in low or "authentication failed" in low:
        return LinkedInPaused("LinkedIn session expired. Run: igothired connect-linkedin")
    if "in progress" in low and ("setup" in low or "login" in low or "authentication" in low):
        return LinkedInPaused("LinkedIn browser is still setting up.", timedelta(minutes=5))
    return LinkedInError(text.strip()[:500] or "unknown LinkedIn error")


def server_params(cfg: Config, *extra_args: str) -> StdioServerParameters:
    command = shutil.which(cfg.linkedin.command, path=search_path()) or cfg.linkedin.command
    # The server only gets a minimal environment, so your API key never reaches it.
    env = {**get_default_environment(), "PATH": search_path(), "UV_HTTP_TIMEOUT": "300"}
    return StdioServerParameters(command=command, args=[*cfg.linkedin.args, *extra_args], env=env)


def _unwrap(result: Any) -> dict[str, Any]:
    data = getattr(result, "structured_content", None)
    if not data:
        text = "\n".join(c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"text": text}
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
        data = data["result"]
    return data if isinstance(data, dict) else {"result": data}


def section_text(data: dict[str, Any]) -> str:
    sections = data.get("sections")
    if isinstance(sections, dict):
        return "\n\n".join(f"## {name}\n{text}" for name, text in sections.items() if text)
    return str(data.get("text", ""))


def references(data: dict[str, Any], kind: str) -> list[dict[str, str]]:
    refs = data.get("references") or {}
    groups = refs.values() if isinstance(refs, dict) else [refs]
    return [r for group in groups for r in (group or []) if isinstance(r, dict) and r.get("kind") == kind]


class LinkedIn:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._last_call = 0.0
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> LinkedIn:
        ensure_home()
        self._stack = AsyncExitStack()
        errlog = self._stack.enter_context(open(LOG_DIR / "linkedin-mcp.log", "a"))
        read, write = await self._stack.enter_async_context(stdio_client(server_params(self.cfg), errlog=errlog))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack:
            await self._stack.aclose()

    async def _pace(self, write: bool) -> None:
        low, high = self.cfg.limits.write_delay if write else self.cfg.limits.read_delay
        if self._last_call:
            wait = random.uniform(low, high) - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    async def call(self, tool: str, args: dict[str, Any], *, write: bool = False) -> dict[str, Any]:
        assert self.session, "use `async with LinkedIn(...)`"
        await self._pace(write)
        target = str(args.get("linkedin_username") or args.get("keywords") or args.get("thread_id") or "")
        try:
            result = await self.session.call_tool(tool, args, read_timeout_seconds=self.cfg.linkedin.tool_timeout)
        except Exception as e:  # transport errors, timeouts
            self.store.log_action(tool, target, False, repr(e))
            raise LinkedInError(f"{tool} didn't answer: {e}") from e
        finally:
            self._last_call = time.monotonic()

        if getattr(result, "is_error", False):
            text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
            self.store.log_action(tool, target, False, text)
            raise classify_error(text)
        data = _unwrap(result)
        self.store.log_action(tool, target, True, str(data.get("status", "")))
        return data

    async def list_tools(self) -> list[str]:
        assert self.session
        result = await self.session.list_tools()
        return [t.name for t in result.tools]

    # Reads

    async def me(self) -> dict[str, Any]:
        return await self.call("get_my_profile", {})

    async def search_posts(self, keywords: str) -> dict[str, Any]:
        d = self.cfg.discovery
        return await self.call("search_posts", {"keywords": keywords, "date_posted": d.date_posted, "max_pages": d.max_pages})

    async def profile(self, username: str) -> dict[str, Any]:
        return await self.call("get_person_profile", {"linkedin_username": username, "sections": "experience,posts"})

    async def inbox(self, limit: int = 25) -> dict[str, Any]:
        return await self.call("get_inbox", {"limit": limit})

    async def conversation(self, *, username: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        args = {"thread_id": thread_id} if thread_id else {"linkedin_username": username}
        return await self.call("get_conversation", args)

    # Writes. Callers check the returned status; these never retry on their own,
    # because a retry after an unknown outcome can send the same thing twice.

    async def connect(self, username: str, note: str | None) -> dict[str, Any]:
        args: dict[str, Any] = {"linkedin_username": username}
        if note:
            args["note"] = note
        return await self.call("connect_with_person", args, write=True)

    async def send(self, username: str, text: str) -> dict[str, Any]:
        return await self.call(
            "send_message", {"linkedin_username": username, "message": text, "confirm_send": True}, write=True
        )
