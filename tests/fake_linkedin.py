"""Stand-in for linkedin-mcp-server in tests: same tool names and result shapes, canned answers.

State lives in a JSON file (path given as the first argument) so a test can
change what LinkedIn "says" between runs and inspect every call that was made.
"""

import json
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

STATE = Path(sys.argv[1])
server = MCPServer("fake-linkedin")


def _record(tool: str, **args: Any) -> dict:
    state = json.loads(STATE.read_text())
    state.setdefault("calls", []).append({"tool": tool, **args})
    STATE.write_text(json.dumps(state))
    if tool in state.get("errors", {}):
        raise ToolError(state["errors"][tool])
    return state


@server.tool()
def get_my_profile() -> dict:
    _record("get_my_profile")
    return {"url": "https://www.linkedin.com/in/me-test/", "sections": {"main_profile": "Test User"}}


@server.tool()
def search_posts(keywords: str, date_posted: str | None = None, max_pages: int = 3) -> dict:
    return _record("search_posts", keywords=keywords).get("posts", {"sections": {}})


@server.tool()
def get_person_profile(linkedin_username: str, sections: str | None = None) -> dict:
    state = _record("get_person_profile", linkedin_username=linkedin_username)
    return state.get("profiles", {}).get(linkedin_username, {"sections": {"main_profile": f"{linkedin_username} profile"}})


@server.tool()
def connect_with_person(linkedin_username: str, note: str | None = None) -> dict:
    state = _record("connect_with_person", linkedin_username=linkedin_username, note=note)
    return state.get("connect", {}).get(linkedin_username, {"status": "connected", "note_sent": bool(note)})


@server.tool()
def send_message(linkedin_username: str, message: str, confirm_send: bool, profile_urn: str | None = None) -> dict:
    state = _record("send_message", linkedin_username=linkedin_username, message=message, confirm_send=confirm_send)
    return state.get("send", {}).get(linkedin_username, {"status": "sent", "sent": True, "retry_safe": False, "thread_id": "t-1"})


@server.tool()
def get_inbox(limit: int = 20) -> dict:
    return _record("get_inbox").get("inbox", {"sections": {"inbox": ""}})


@server.tool()
def get_conversation(linkedin_username: str | None = None, thread_id: str | None = None, index: int = 0) -> dict:
    state = _record("get_conversation", linkedin_username=linkedin_username, thread_id=thread_id)
    return state.get("conversation", {"sections": {"conversation": ""}})


if __name__ == "__main__":
    server.run("stdio")
