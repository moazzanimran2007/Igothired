from __future__ import annotations

import asyncio
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from . import prompts
from .config import Config
from .profile import Profile

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLM:
    """Thin wrapper around Claude: structured answers, a concurrency cap, and the shared cached prefix."""

    def __init__(self, cfg: Config, profile: Profile, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self.client = client or anthropic.AsyncAnthropic(max_retries=3)
        self.system = prompts.base_system(cfg, profile)
        self._slots = asyncio.Semaphore(cfg.model.max_concurrency)

    async def ask(self, task: str, content: str, schema: type[T], *, effort: str = "medium") -> T:
        async with self._slots:
            try:
                response = await self.client.beta.messages.parse(
                    model=self.cfg.model.name,
                    max_tokens=16000,
                    system=[
                        # Profile + rules are identical on every call, so they're cached once per run.
                        {"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": prompts.fill(task, self.cfg)},
                    ],
                    messages=[{"role": "user", "content": content}],
                    output_config={"effort": effort},
                    output_format=schema,
                    # If a safety classifier declines, retry server-side on the recommended fallback model.
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.AuthenticationError as e:
                raise LLMError("Anthropic API key was rejected. Run: igothired setup") from e
            except anthropic.RateLimitError as e:
                raise LLMError("Anthropic rate limit, try again in a few minutes") from e
            except anthropic.APIStatusError as e:
                raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise LLMError("Couldn't reach the Claude API") from e

        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this one")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude's answer was cut off")
        if response.parsed_output is None:
            raise LLMError("Claude didn't return the expected format")
        return response.parsed_output
