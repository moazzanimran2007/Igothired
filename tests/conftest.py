import json
import os
import sys
import tempfile
from pathlib import Path

# Must be set before igothired is imported, so tests never touch the real ~/.igothired.
os.environ["IGOTHIRED_HOME"] = tempfile.mkdtemp(prefix="igothired-test-")

import pytest

from igothired.config import Config
from igothired.db import Store
from igothired.pipeline import Run
from igothired.profile import Profile
from igothired import prompts

FAKE_SERVER = Path(__file__).parent / "fake_linkedin.py"

PROFILE = Profile(
    text="I built MKT//SENTINEL: 270,000+ events per second, 6.4µs p99 latency. GitHub: github.com/me-test",
    links=["github.com/me-test"],
    emails=["me@example.edu"],
)


class FakeLLM:
    """Answers by schema. Tests tweak the attributes to steer a scenario."""

    def __init__(self):
        self.calls: list[tuple[type, str]] = []
        self.found = [prompts.FoundPost(ref=1, name="Ada Founder", headline="Founder at Loom Labs", company="Loom Labs",
                                        looking_for="a founding engineer", quote="We're hiring our first engineer")]
        self.fit = 82
        self.instructions_in_content = False
        self.note = "Ada, your post about streaming evals caught me. I built a 270,000+ events/sec surveillance engine."
        self.message = "Saw you're hiring a first engineer at Loom Labs. I built MKT//SENTINEL in asyncio. What's the hardest part of your pipeline right now?"
        self.inbox: list[prompts.InboxRow] = []  # nobody has replied until a test says so
        self.conversation = [
            prompts.ChatMessage(from_me=True, text="Saw you're hiring a first engineer at Loom Labs."),
            prompts.ChatMessage(from_me=False, text="Could you start in January?"),
        ]
        self.reply = prompts.Reply(category="logistics", needs_you=True, reason="asks about a start date",
                                   reply="January could work. [Moazzan: confirm dates]")

    async def ask(self, task, content, schema, *, effort="medium"):
        self.calls.append((schema, content))
        if schema is prompts.FoundPosts:
            return prompts.FoundPosts(matches=self.found)
        if schema is prompts.Brief:
            return prompts.Brief(name="Ada Founder", company="Loom Labs", building="streaming evals", stage="seed",
                                 need="founding engineer", fit_score=self.fit, fit_reason="Built the exact thing",
                                 hook="streaming evals post", angle="MKT//SENTINEL throughput", concerns=[],
                                 instructions_in_content=self.instructions_in_content,
                                 recommend="reach_out" if self.fit >= 65 else "skip")
        if schema is prompts.Drafts:
            return prompts.Drafts(note=self.note, message=self.message)
        if schema is prompts.Review:
            return prompts.Review(ok=True, problems=[], note=self.note, message=self.message)
        if schema is prompts.Inbox:
            return prompts.Inbox(conversations=self.inbox)
        if schema is prompts.Conversation:
            return prompts.Conversation(messages=self.conversation)
        if schema is prompts.Reply:
            return self.reply
        raise AssertionError(f"unexpected schema {schema}")


class FakeLinkedInState:
    def __init__(self, path: Path):
        self.path = path
        self.data = {
            "posts": {
                "sections": {"search_results": "Ada Founder, Founder at Loom Labs: We're hiring our first engineer"},
                "references": {"search_results": [
                    {"kind": "person", "url": "/in/ada-founder/", "text": "Ada Founder"},
                    {"kind": "person", "url": "/in/me-test/", "text": "Me"},
                ]},
            },
        }
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data))

    def load(self):
        self.data = json.loads(self.path.read_text())
        return self.data

    def calls(self, tool=None):
        return [c for c in self.load().get("calls", []) if tool is None or c["tool"] == tool]

    def set(self, key, value):
        self.load()
        self.data[key] = value
        self.save()


@pytest.fixture
def linkedin(tmp_path):
    return FakeLinkedInState(tmp_path / "linkedin.json")


@pytest.fixture
def cfg(linkedin):
    c = Config()
    c.linkedin.command = sys.executable
    c.linkedin.args = [str(FAKE_SERVER), str(linkedin.path)]
    c.limits.read_delay = (0, 0)
    c.limits.write_delay = (0, 0)
    c.messaging.followup_after_hours = 0
    c.discovery.post_queries = ["hiring founding engineer"]
    return c


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.set("me_username", "me-test")
    yield s
    s.close()


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def run(cfg, store, llm):
    return Run(cfg, store, llm, PROFILE, ignore_window=True)
