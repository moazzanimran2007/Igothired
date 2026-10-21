"""Checks the request we send to Claude, with the HTTP layer mocked out."""

import json

import anthropic
import httpx2
import pytest

from igothired import prompts
from igothired.llm import LLM, LLMError

from conftest import PROFILE


def _client(captured: list, *, stop_reason="end_turn", payload=None):
    payload = payload or {"note": "n", "message": "m"}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    return anthropic.AsyncAnthropic(api_key="test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


async def test_request_shape(cfg):
    captured = []
    llm = LLM(cfg, PROFILE, client=_client(captured))
    drafts = await llm.ask(prompts.FIRST_TOUCH, "about them", prompts.Drafts, effort="high")
    assert drafts == prompts.Drafts(note="n", message="m")

    request = captured[0]
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
    # The profile prefix is cached; the task text comes after it, filled in.
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "MKT//SENTINEL" in body["system"][0]["text"]
    assert "{first}" not in body["system"][1]["text"] and "Moazzan" in body["system"][1]["text"]


async def test_refusal_is_an_error(cfg):
    llm = LLM(cfg, PROFILE, client=_client([], stop_reason="refusal"))
    with pytest.raises(LLMError):
        await llm.ask(prompts.FIRST_TOUCH, "x", prompts.Drafts)


@pytest.mark.parametrize("schema", [prompts.FoundPosts, prompts.Brief, prompts.Drafts, prompts.Review,
                                    prompts.Inbox, prompts.Conversation, prompts.Reply])
def test_schemas_are_plain_json_schema(schema):
    # Structured outputs need every object closed and every field present.
    from anthropic.lib._parse._transform import transform_schema

    schema_json = transform_schema(schema)
    assert schema_json["type"] == "object"
    assert schema_json.get("additionalProperties") is False
