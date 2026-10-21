"""End to end against the fake LinkedIn server: discover, draft, approve, send, reply."""

from igothired.pipeline import Decision, apply_decisions, run_cycle
from igothired.db import warnings_of
from igothired import prompts


async def _first_cycle(run):
    await run_cycle(run)
    founder = run.store.founder_by_username("ada-founder")
    assert founder is not None
    return founder


async def test_discovers_researches_and_waits_for_review(run, linkedin):
    founder = await _first_cycle(run)

    assert founder["stage"] == "review"
    assert founder["fit_score"] == 82
    # Never picks yourself, even though you're in the search references.
    assert run.store.founder_by_username("me-test") is None
    drafts = run.store.messages(founder["id"], statuses=("review",))
    assert {m["kind"] for m in drafts} == {"note", "dm"}
    # Review mode: nothing was sent.
    assert not linkedin.calls("connect_with_person")
    assert not linkedin.calls("send_message")


async def test_only_people_from_the_search_can_be_added(run, llm, linkedin):
    llm.found = [prompts.FoundPost(ref=7, name="Injected", headline="", company="", looking_for="", quote="")]
    await run_cycle(run)
    assert run.store.founders() == []


async def test_low_fit_is_skipped(run, llm):
    llm.fit = 40
    founder = await _first_cycle(run)
    assert founder["stage"] == "skipped"
    assert not run.store.messages(founder["id"])


async def test_approved_message_is_sent_exactly_as_edited(run, linkedin):
    founder = await _first_cycle(run)
    edited = "Edited by hand. What's the hardest part of your pipeline?"
    apply_decisions(run, [Decision(item=f"F{founder['id']}", action="approve", note="Short note.", message=edited)])
    assert run.store.founder(founder["id"])["stage"] == "approved"

    await run_cycle(run)

    invite = linkedin.calls("connect_with_person")
    sent = linkedin.calls("send_message")
    assert invite == [{"tool": "connect_with_person", "linkedin_username": "ada-founder", "note": "Short note."}]
    assert len(sent) == 1 and sent[0]["message"] == edited and sent[0]["confirm_send"] is True
    assert run.store.founder(founder["id"])["stage"] == "contacted"

    # A second run must not send anything again.
    await run_cycle(run)
    assert len(linkedin.calls("send_message")) == 1
    assert len(linkedin.calls("connect_with_person")) == 1


async def test_cleared_note_connects_without_one(run, linkedin):
    founder = await _first_cycle(run)
    apply_decisions(run, [Decision(item=f"F{founder['id']}", action="approve", note="", message="")])
    await run_cycle(run)
    assert linkedin.calls("connect_with_person")[0]["note"] is None


async def test_reply_about_logistics_waits_for_you_even_in_auto(run, llm, linkedin):
    run.cfg.autonomy.mode = "auto"
    founder = await _first_cycle(run)  # auto mode approves and sends the clean first touch
    assert run.store.founder(founder["id"])["stage"] == "contacted"
    sent_before = len(linkedin.calls("send_message"))

    llm.inbox = [prompts.InboxRow(name="Ada Founder", snippet="Could you start in January?", last_from_me=False)]
    linkedin.set("inbox", {"sections": {"inbox": "Ada Founder: Could you start in January?"},
                           "references": {"inbox": [{"kind": "conversation", "url": "/messaging/thread/t-1/", "text": "Ada Founder"}]}})
    await run_cycle(run)

    founder = run.store.founder(founder["id"])
    assert founder["stage"] == "needs_you"
    assert "start date" in founder["needs_you"]
    reply = run.store.latest(founder["id"], "reply", ("review",))
    assert reply is not None and reply["category"] == "logistics"
    assert len(linkedin.calls("send_message")) == sent_before
    assert linkedin.calls("get_conversation")[-1]["thread_id"] == "t-1"


async def test_simple_reply_goes_out_on_its_own_in_auto(run, llm, linkedin):
    run.cfg.autonomy.mode = "auto"
    founder = await _first_cycle(run)
    llm.inbox = [prompts.InboxRow(name="Ada Founder", snippet="What's your GitHub?", last_from_me=False)]
    llm.conversation[-1] = prompts.ChatMessage(from_me=False, text="What's your GitHub?")
    llm.reply = prompts.Reply(category="asks_for_links", needs_you=False, reason="", reply="Here it is: github.com/me-test")
    linkedin.set("inbox", {"sections": {"inbox": "Ada Founder: What's your GitHub?"}})

    await run_cycle(run)

    assert linkedin.calls("send_message")[-1]["message"] == "Here it is: github.com/me-test"
    assert run.store.founder(founder["id"])["stage"] == "contacted"


async def test_unknown_outcome_is_never_retried(run, linkedin):
    founder = await _first_cycle(run)
    linkedin.set("send", {"ada-founder": {"status": "outcome_unknown", "retry_safe": False}})
    apply_decisions(run, [Decision(item=f"F{founder['id']}", action="approve", message="")])

    await run_cycle(run)
    await run_cycle(run)

    assert len(linkedin.calls("send_message")) == 1
    founder = run.store.founder(founder["id"])
    assert founder["stage"] == "needs_you"
    assert run.store.latest(founder["id"], "dm", ("unknown",)) is not None


async def test_not_accepted_yet_backs_off(run, linkedin):
    founder = await _first_cycle(run)
    linkedin.set("send", {"ada-founder": {"status": "message_unavailable", "sent": False, "retry_safe": True}})
    apply_decisions(run, [Decision(item=f"F{founder['id']}", action="approve", message="")])

    await run_cycle(run)
    await run_cycle(run)

    assert len(linkedin.calls("send_message")) == 1  # second run waits for the backoff
    dm = run.store.latest(founder["id"], "dm", ("approved",))
    assert dm["send_after"] is not None


async def test_daily_limits_hold_the_rest(run, linkedin):
    run.cfg.limits.invites_per_day = 0
    founder = await _first_cycle(run)
    apply_decisions(run, [Decision(item=f"F{founder['id']}", action="approve", message="")])
    await run_cycle(run)
    assert not linkedin.calls("connect_with_person")
    assert any("invite limit" in n for n in run.notes)


async def test_rate_limit_pauses_everything(run, linkedin):
    linkedin.set("errors", {"search_posts": "Rate limit detected. Wait 300 seconds before trying again."})
    await run_cycle(run)
    assert run.store.pause_state() is not None

    calls = len(linkedin.calls())
    linkedin.set("errors", {})
    await run_cycle(run)
    assert len(linkedin.calls()) == calls  # paused runs don't even start the browser


async def test_sending_window_is_respected(run, linkedin):
    run.ignore_window = False
    run.cfg.limits.send_window.weekdays = []
    founder = await _first_cycle(run)
    apply_decisions(run, [Decision(item=f"F{founder['id']}", action="approve", message="")])
    await run_cycle(run)
    assert not linkedin.calls("connect_with_person")


async def test_injection_warning_blocks_auto_send(run, llm, linkedin):
    run.cfg.autonomy.mode = "auto"
    llm.instructions_in_content = True
    founder = await _first_cycle(run)
    assert founder["stage"] == "review"
    dm = run.store.latest(founder["id"], "dm", ("review",))
    assert any("instructions aimed at AI" in w for w in warnings_of(dm))
    assert not linkedin.calls("send_message")


async def test_redraft_uses_your_feedback(run, llm):
    founder = await _first_cycle(run)
    apply_decisions(run, [Decision(item=f"F{founder['id']}", action="redraft", feedback="mention JusticeLens", message="")])
    llm.message = "Second version mentioning JusticeLens."
    await run_cycle(run, discover_new=False)

    dm = run.store.latest(founder["id"], "dm", ("review",))
    assert dm["body"] == "Second version mentioning JusticeLens."
    drafts_call = [c for schema, c in llm.calls if schema is prompts.Drafts][-1]
    assert "mention JusticeLens" in drafts_call
