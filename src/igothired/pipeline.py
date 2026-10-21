"""The outreach loop: find founders, research them, draft, send what's approved, answer replies."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import prompts
from .config import Config
from .db import Store, parse_stamp, stamp, utcnow, warnings_of
from .linkedin import LinkedIn, LinkedInError, LinkedInPaused, references, section_text
from .llm import LLM, LLMError
from .profile import Profile, numbers_in
from .safety import Check, check_message, untrusted, username_from

log = logging.getLogger("igothired")

FIRST_TOUCH_KINDS = ("note", "dm")
OPEN_DRAFT = ("review", "redraft")


@dataclass
class Run:
    cfg: Config
    store: Store
    llm: LLM
    profile: Profile
    dry_run: bool = False
    ignore_window: bool = False
    on_phase: Callable[[str], None] = lambda _: None
    notes: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        self.notes.append(line)
        log.info(line)


@dataclass
class Decision:
    """One row from the sheet: what you decided and the text as you left it."""

    item: str  # "F12" = first touch for founder 12, "R40" = reply message 40
    action: str  # approve, skip, redraft, or "" for no decision yet
    feedback: str = ""
    note: str | None = None
    message: str = ""


# -- helpers ------------------------------------------------------------------


def _about(founder: sqlite3.Row) -> str:
    parts = [
        f"Founder: {founder['name']} ({founder['headline']})",
        f"Company: {founder['company'] or 'unknown'}",
        f"Found via: {founder['source']}",
        untrusted("why we found them", founder["signal"]),
    ]
    if founder["profile_text"]:
        parts.append(untrusted("their LinkedIn profile", founder["profile_text"]))
    return "\n\n".join(parts)


def _brief(founder: sqlite3.Row) -> prompts.Brief | None:
    try:
        return prompts.Brief.model_validate_json(founder["brief"]) if founder["brief"] else None
    except ValueError:
        return None


def _check(run: Run, text: str, founder: sqlite3.Row, max_chars: int) -> Check:
    known = numbers_in(run.profile.text) | numbers_in(founder["signal"]) | numbers_in(founder["profile_text"])
    known |= numbers_in(founder["brief"]) | numbers_in(founder["transcript"])
    return check_message(
        text,
        max_chars=max_chars,
        allowed_links=run.profile.links,
        allowed_emails=run.profile.emails,
        known_numbers=known,
    )


def _name_key(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", name.casefold()).split())


def _same_person(display: str, known: str) -> bool:
    a, b = _name_key(display), _name_key(known)
    if not a or not b:
        return False
    # LinkedIn display names pick up credentials and emoji ("Jane Doe, PhD"), so compare tokens.
    return a == b or set(b.split()) <= set(a.split())


def blocked_reason(run: Run) -> str | None:
    if reason := run.store.pause_state():
        return f"paused ({reason})"
    if run.ignore_window:
        return None
    window = run.cfg.limits.send_window
    now = datetime.now().astimezone()
    if now.weekday() not in window.weekdays or not window.start_hour <= now.hour < window.end_hour:
        return f"outside sending hours ({window.start_hour}:00 to {window.end_hour}:00 on weekdays)"
    return None


def quota(cfg: Config, s: Store) -> dict[str, int]:
    lim = cfg.limits
    invites = min(
        lim.invites_per_day - s.count_actions("invite", timedelta(days=1)),
        lim.invites_per_week - s.count_actions("invite", timedelta(days=7)),
    )
    messages = lim.messages_per_day - s.count_actions("message", timedelta(days=1))
    return {"invites": max(invites, 0), "messages": max(messages, 0)}


# -- discovery ----------------------------------------------------------------


async def discover(run: Run, li: LinkedIn, *, force: bool = False) -> int:
    """Search posts for hiring signals. Once a day is plenty, and it keeps LinkedIn calm."""
    today = utcnow().date().isoformat()
    if run.store.get("last_discovery") == today and not force:
        return 0
    run.on_phase("Searching LinkedIn posts")
    picks = []
    try:
        for query in run.cfg.discovery.post_queries:
            try:
                result = await li.search_posts(query)
            except LinkedInPaused:
                raise
            except LinkedInError as e:
                run.note(f"Search {query!r} failed: {e}")
                continue
            # Let Claude read this page while the browser loads the next one.
            picks.append(asyncio.create_task(_pick_founders(run, query, result)))
    finally:
        added = sum(await asyncio.gather(*picks))
    run.store.set("last_discovery", today)
    if added:
        run.note(f"Found {added} new founder{'s' if added != 1 else ''} who look like they're hiring")
    return added


async def _pick_founders(run: Run, query: str, result: dict) -> int:
    people, seen = [], set()
    me = run.store.get("me_username")
    for ref in references(result, "person"):
        username = username_from(ref.get("url", ""))
        if username and username not in seen and username != me:
            seen.add(username)
            people.append((username, ref.get("text", "").strip()))
    if not people:
        return 0

    listing = "\n".join(f"{i}. {name or '(no name)'} - /in/{username}" for i, (username, name) in enumerate(people, 1))
    content = f"Search query: {query}\n\n{untrusted('post search results', section_text(result))}\n\nPeople linked from these results:\n{untrusted('people list', listing)}"
    try:
        found = await run.llm.ask(prompts.FIND_FOUNDERS, content, prompts.FoundPosts, effort="low")
    except LLMError as e:
        run.note(f"Couldn't read results for {query!r}: {e}")
        return 0

    added = 0
    for match in found.matches:
        if not 1 <= match.ref <= len(people):
            continue  # only people the search actually returned, never one the model made up
        username, name = people[match.ref - 1]
        new_id = run.store.add_founder(
            username,
            name=match.name or name,
            headline=match.headline,
            company=match.company,
            signal=f'{match.looking_for}\n\nFrom their post: "{match.quote}"',
            source=f"post search: {query}",
        )
        added += bool(new_id)
    return added


# -- research and first drafts ------------------------------------------------


async def research(run: Run, li: LinkedIn) -> None:
    fresh = run.store.founders("new", limit=run.cfg.limits.new_founders_per_run, order="created_at")
    if not fresh:
        return
    run.on_phase(f"Researching {len(fresh)} founder{'s' if len(fresh) != 1 else ''}")
    jobs = []
    try:
        for founder in fresh:
            try:
                data = await li.profile(founder["username"])
            except LinkedInPaused:
                raise
            except LinkedInError as e:
                run.store.update_founder(founder["id"], stage="error", error=str(e))
                continue
            run.store.update_founder(founder["id"], profile_text=section_text(data)[:20000])
            jobs.append(asyncio.create_task(prepare_first_touch(run, founder["id"])))
    finally:
        await asyncio.gather(*jobs)
    ready = len(run.store.founders("review"))
    if ready:
        run.note(f"{ready} first message{'s' if ready != 1 else ''} waiting for you in the sheet")


async def prepare_first_touch(run: Run, founder_id: int) -> None:
    founder = run.store.founder(founder_id)
    try:
        brief = await run.llm.ask(prompts.RESEARCH, _about(founder), prompts.Brief, effort="medium")
    except LLMError as e:
        run.store.update_founder(founder_id, stage="error", error=str(e))
        return
    run.store.update_founder(
        founder_id,
        brief=brief.model_dump_json(),
        fit_score=brief.fit_score,
        fit_reason=brief.fit_reason,
        company=brief.company or founder["company"],
        # People added by hand only have a username until their profile is read.
        name=founder["name"] if founder["name"] != founder["username"] else (brief.name or founder["name"]),
    )
    if brief.recommend == "skip" or brief.fit_score < run.cfg.targeting.min_fit_score:
        run.store.update_founder(founder_id, stage="skipped")
        return
    await write_first_touch(run, founder_id)


async def write_first_touch(run: Run, founder_id: int, feedback: str = "") -> None:
    founder = run.store.founder(founder_id)
    brief = _brief(founder)
    content = _about(founder) + f"\n\n<research>\n{founder['brief']}\n</research>"
    if feedback:
        old_note = run.store.latest(founder_id, "note", OPEN_DRAFT)
        old_dm = run.store.latest(founder_id, "dm", OPEN_DRAFT)
        content += (
            f"\n\n<previous_draft>\nNote: {old_note['body'] if old_note else ''}\n\nMessage: {old_dm['body'] if old_dm else ''}\n</previous_draft>"
            f"\n\n<feedback_from_{run.cfg.first_name.lower()}>\n{feedback}\n</feedback_from_{run.cfg.first_name.lower()}>"
        )
    try:
        drafts = await run.llm.ask(prompts.FIRST_TOUCH, content, prompts.Drafts, effort="high")
        review = await run.llm.ask(
            prompts.REVIEW,
            content + f"\n\n<draft>\nNote: {drafts.note}\n\nMessage: {drafts.message}\n</draft>",
            prompts.Review,
            effort="medium",
        )
    except LLMError as e:
        run.store.update_founder(founder_id, stage="error", error=str(e))
        return

    msg = run.cfg.messaging
    extra = []
    if brief and brief.instructions_in_content:
        extra.append("their post or profile has instructions aimed at AI tools, read it yourself first")
    if brief:
        extra += [f"heads up: {c}" for c in brief.concerns]
    note_check = _check(run, review.note, founder, msg.note_max_chars)
    dm_check = _check(run, review.message, founder, msg.message_max_chars)

    run.store.supersede(founder_id, FIRST_TOUCH_KINDS)
    run.store.add_message(founder_id, "out", "note", review.note.strip(), "review",
                          warnings=[f"note: {w}" for w in note_check.all()])
    run.store.add_message(founder_id, "out", "dm", review.message.strip(), "review",
                          warnings=[f"message: {w}" for w in dm_check.all()] + extra)
    run.store.update_founder(founder_id, stage="review", error="")


# -- decisions from the sheet -------------------------------------------------


def apply_decisions(run: Run, decisions: list[Decision]) -> None:
    for d in decisions:
        kind, raw_id = d.item[:1], d.item[1:]
        if not raw_id.isdigit():
            continue
        if kind == "F":
            _apply_first_touch(run, int(raw_id), d)
        elif kind == "R":
            _apply_reply(run, int(raw_id), d)
        elif kind == "N" and d.action in {"approve", "skip"}:
            _clear_flag(run, int(raw_id))


def _clear_flag(run: Run, founder_id: int) -> None:
    """You've looked at a flagged founder (e.g. an unconfirmed send) and sorted it out on LinkedIn."""
    founder = run.store.founder(founder_id)
    if not founder or founder["stage"] != "needs_you":
        return
    if run.store.latest(founder_id, "reply", OPEN_DRAFT):
        return  # that one is resolved by approving or skipping the reply itself
    fields = {"needs_you": ""}
    if founder["connection"] == "none":
        fields.update(connection="invited", invited_at=stamp())
    reached = run.store.messages(founder_id, kinds=("dm", "reply"), statuses=("sent", "unknown"))
    fields["stage"] = "contacted" if reached else "invited"
    run.store.update_founder(founder_id, **fields)


def _apply_first_touch(run: Run, founder_id: int, d: Decision) -> None:
    founder = run.store.founder(founder_id)
    if not founder or founder["stage"] != "review":
        return
    note = run.store.latest(founder_id, "note", OPEN_DRAFT)
    dm = run.store.latest(founder_id, "dm", OPEN_DRAFT)
    if not dm:
        return
    msg = run.cfg.messaging

    if d.action == "skip":
        for m in (note, dm):
            if m:
                run.store.update_message(m["id"], status="skipped")
        run.store.update_founder(founder_id, stage="skipped")
        run.note(f"Skipped {founder['name']}")
        return
    if d.action == "redraft":
        for m in (note, dm):
            if m:
                run.store.update_message(m["id"], status="redraft", feedback=d.feedback)
        return

    # Keep your edits even if you haven't decided yet, so the next sync doesn't overwrite them.
    note_text = (d.note if d.note is not None else (note["body"] if note else "")).strip()
    dm_text = d.message.strip() or dm["body"]
    note_check = _check(run, note_text, founder, msg.note_max_chars) if note_text else Check()
    dm_check = _check(run, dm_text, founder, msg.message_max_chars)
    if note:
        run.store.update_message(note["id"], body=note_text, warnings=[f"note: {w}" for w in note_check.all()])
    keep = [w for w in warnings_of(dm) if not w.startswith("message: ")]
    run.store.update_message(dm["id"], body=dm_text, warnings=[f"message: {w}" for w in dm_check.all()] + keep)

    if d.action != "approve":
        return
    if note_check.errors or dm_check.errors:
        run.note(f"Can't approve {founder['name']} yet: {'; '.join(note_check.errors + dm_check.errors)}")
        return
    if note:
        # An emptied note cell means: connect without a note.
        run.store.update_message(note["id"], status="approved" if note_text else "skipped")
    run.store.update_message(dm["id"], status="approved")
    run.store.update_founder(founder_id, stage="approved")
    run.note(f"Approved {founder['name']}")


def _apply_reply(run: Run, message_id: int, d: Decision) -> None:
    reply = run.store.message(message_id)
    if not reply or reply["status"] not in OPEN_DRAFT:
        return
    founder = run.store.founder(reply["founder_id"])
    if d.action == "skip":
        run.store.update_message(message_id, status="skipped")
        if founder["stage"] in ("replied", "needs_you"):
            run.store.update_founder(founder["id"], stage="contacted", needs_you="")
        return
    if d.action == "redraft":
        run.store.update_message(message_id, status="redraft", feedback=d.feedback)
        return

    text = d.message.strip() or reply["body"]
    check = _check(run, text, founder, run.cfg.messaging.message_max_chars)
    run.store.update_message(message_id, body=text, warnings=check.all())
    if d.action != "approve":
        return
    if check.errors:
        run.note(f"Can't approve the reply to {founder['name']} yet: {'; '.join(check.errors)}")
        return
    if "[" in text and "]" in text:
        run.note(f"The reply to {founder['name']} still has a [placeholder], fill it in first")
        return
    run.store.update_message(message_id, status="approved")
    run.store.update_founder(founder["id"], needs_you="")


async def process_redrafts(run: Run) -> None:
    pending = run.store.messages(statuses=("redraft",))
    if not pending:
        return
    run.on_phase("Redrafting with your feedback")
    jobs, seen = [], set()
    for m in pending:
        if m["kind"] in FIRST_TOUCH_KINDS and m["founder_id"] not in seen:
            seen.add(m["founder_id"])
            jobs.append(write_first_touch(run, m["founder_id"], feedback=m["feedback"]))
        elif m["kind"] == "reply":
            jobs.append(draft_reply(run, m["founder_id"], feedback=m["feedback"]))
    await asyncio.gather(*jobs)


def auto_approve(run: Run) -> int:
    """Auto mode: approve drafts that passed every check. Anything flagged still waits for you."""
    if run.cfg.autonomy.mode != "auto":
        return 0
    count = 0
    for founder in run.store.founders("review"):
        drafts = [run.store.latest(founder["id"], k, ("review",)) for k in FIRST_TOUCH_KINDS]
        if drafts[1] and all(m is None or not warnings_of(m) for m in drafts):
            for m in drafts:
                if m:
                    run.store.update_message(m["id"], status="approved")
            run.store.update_founder(founder["id"], stage="approved")
            count += 1
    allowed = set(run.cfg.autonomy.auto_reply_categories)
    for reply in run.store.messages(kinds=("reply",), statuses=("review",)):
        founder = run.store.founder(reply["founder_id"])
        if reply["category"] in allowed and not warnings_of(reply) and founder["stage"] != "needs_you":
            run.store.update_message(reply["id"], status="approved")
            count += 1
    return count


# -- sending ------------------------------------------------------------------


async def send_approved(run: Run, li: LinkedIn) -> None:
    if reason := blocked_reason(run):
        if run.store.founders("approved") or run.store.messages(statuses=("approved",)):
            run.note(f"Holding approved messages: {reason}")
        return
    await _send_invites(run, li)
    await _send_messages(run, li)


async def _send_invites(run: Run, li: LinkedIn) -> None:
    for founder in run.store.founders("approved", order="fit_score DESC"):
        if founder["connection"] != "none":
            continue
        if quota(run.cfg, run.store)["invites"] <= 0:
            run.note("Hit the invite limit, the rest go out next run")
            return
        note = run.store.latest(founder["id"], "note", ("approved",))
        quota_hit = run.store.get("note_quota_month") == utcnow().strftime("%Y-%m")
        text = note["body"] if note and run.cfg.messaging.send_connection_note and not quota_hit else None
        if note and not text and quota_hit and run.cfg.messaging.on_note_quota == "hold":
            continue
        run.on_phase(f"Inviting {founder['name']}")
        if run.dry_run:
            run.note(f"[dry run] would invite {founder['name']}" + (f" with note: {text}" if text else ""))
            continue
        await _invite(run, li, founder, note, text)


async def _invite(run: Run, li: LinkedIn, founder: sqlite3.Row, note: sqlite3.Row | None, text: str | None) -> None:
    fid, name = founder["id"], founder["name"]
    if note:
        run.store.update_message(note["id"], status="sending", attempts=note["attempts"] + 1)
    try:
        result = await li.connect(founder["username"], text)
    except LinkedInPaused:
        if note:
            run.store.update_message(note["id"], status="approved")
        raise
    except LinkedInError as e:
        if note:
            run.store.update_message(note["id"], status="approved" if note["attempts"] < 2 else "failed", error=str(e))
        run.note(f"Invite to {name} failed: {e}")
        return

    status = result.get("status", "")
    if status in {"connected", "pending"}:
        run.store.log_action("invite", founder["username"], status == "connected")
        if note:
            run.store.update_message(note["id"], status="sent" if result.get("note_sent") else "skipped", sent_at=stamp())
        run.store.update_founder(fid, connection="invited", invited_at=stamp(), stage="invited")
        run.note(f"Invited {name}" + (" with a note" if result.get("note_sent") else ""))
    elif status in {"already_connected", "accepted"}:
        if note:
            run.store.update_message(note["id"], status="skipped")
        run.store.update_founder(fid, connection="connected")  # the first message goes out next
    elif status in {"custom_note_limit_reached", "note_not_supported"} and result.get("retry_safe") is not False:
        run.store.set("note_quota_month", utcnow().strftime("%Y-%m"))
        run.note("LinkedIn's free note quota is used up for this month")
        if run.cfg.messaging.on_note_quota == "send_without_note":
            if note:
                run.store.update_message(note["id"], status="skipped")
            await _invite(run, li, founder, None, None)
        elif note:
            run.store.update_message(note["id"], status="approved")
    elif status in {"follow_only", "connect_unavailable"}:
        # No Connect button. Some of these profiles still accept a direct message.
        if note:
            run.store.update_message(note["id"], status="skipped")
        run.store.update_founder(fid, connection="message_only")
    elif status == "outcome_unknown" or result.get("retry_safe") is False:
        if note:
            run.store.update_message(note["id"], status="unknown", sent_at=stamp())
        run.store.update_founder(fid, stage="needs_you", needs_you="LinkedIn didn't confirm the invite. Check their profile before anything else goes out.")
        run.note(f"Invite to {name}: outcome unknown, flagged for you")
    else:
        if note:
            run.store.update_message(note["id"], status="failed", error=result.get("message", status))
        run.store.update_founder(fid, error=result.get("message", status) or "invite failed")
        run.note(f"Invite to {name} failed: {result.get('message', status)}")


async def _send_messages(run: Run, li: LinkedIn) -> None:
    now = utcnow()
    for m in run.store.messages(kinds=("dm", "reply"), statuses=("approved",)):
        founder = run.store.founder(m["founder_id"])
        if m["kind"] == "dm":
            if founder["connection"] == "none":
                continue  # the invite hasn't gone out yet
            if founder["connection"] == "invited":
                invited = parse_stamp(founder["invited_at"])
                if now - invited > timedelta(days=run.cfg.messaging.give_up_after_days):
                    run.store.update_message(m["id"], status="skipped")
                    run.store.update_founder(founder["id"], stage="no_response")
                    continue
                if now - invited < timedelta(hours=run.cfg.messaging.followup_after_hours):
                    continue
        if m["send_after"] and parse_stamp(m["send_after"]) > now:
            continue
        if quota(run.cfg, run.store)["messages"] <= 0:
            run.note("Hit the daily message limit, the rest go out next run")
            return
        run.on_phase(f"Messaging {founder['name']}")
        if run.dry_run:
            run.note(f"[dry run] would message {founder['name']}: {m['body'][:80]}...")
            continue
        await _send_one(run, li, founder, m)


async def _send_one(run: Run, li: LinkedIn, founder: sqlite3.Row, m: sqlite3.Row) -> None:
    fid, name = founder["id"], founder["name"]
    run.store.update_message(m["id"], status="sending", attempts=m["attempts"] + 1)
    try:
        result = await li.send(founder["username"], m["body"])
    except LinkedInPaused:
        run.store.update_message(m["id"], status="approved")
        raise
    except LinkedInError as e:
        run.store.update_message(m["id"], status="approved" if m["attempts"] < 2 else "review",
                                 error=str(e), warnings=warnings_of(m) + [f"sending failed: {e}"])
        run.note(f"Message to {name} failed: {e}")
        return

    status = result.get("status", "")
    if result.get("sent"):
        run.store.log_action("message", founder["username"], True)
        run.store.update_message(m["id"], status="sent", sent_at=stamp(), error="")
        run.store.update_founder(
            fid,
            stage="contacted",
            connection="connected",
            thread_id=result.get("thread_id") or founder["thread_id"],
            needs_you="",
        )
        run.note(f"Sent {'reply' if m['kind'] == 'reply' else 'message'} to {name}")
    elif status == "message_unavailable" and m["kind"] == "dm":
        # They haven't accepted yet. Check back later, less often each time.
        backoff = timedelta(hours=min(24 * 2 ** m["attempts"], 96))
        run.store.update_message(m["id"], status="approved", send_after=stamp(utcnow() + backoff))
        if founder["connection"] == "message_only":
            run.store.update_message(m["id"], status="failed", error="can't connect or message this person")
            run.store.update_founder(fid, stage="unreachable")
    elif status == "enter_to_send_enabled":
        run.store.update_message(m["id"], status="approved")
        raise LinkedInPaused(
            "LinkedIn's 'Press Enter to Send' setting is on. Switch it to 'Click Send to send' in LinkedIn messaging settings, then run: igothired resume"
        )
    elif status == "outcome_unknown" or result.get("retry_safe") is False:
        run.store.update_message(m["id"], status="unknown", sent_at=stamp())
        run.store.update_founder(fid, stage="needs_you", needs_you="LinkedIn didn't confirm the message went out. Check the conversation before sending anything else.")
        run.note(f"Message to {name}: outcome unknown, flagged for you")
    else:
        reason = result.get("message") or status or "unknown error"
        run.store.update_message(m["id"], status="review", error=reason, warnings=warnings_of(m) + [f"sending failed: {reason}"])
        run.note(f"Message to {name} failed: {reason}")


# -- inbox and replies --------------------------------------------------------


async def check_inbox(run: Run, li: LinkedIn) -> None:
    active = run.store.founders("invited", "contacted", "replied", "needs_you")
    if not active:
        return
    run.on_phase("Checking your inbox")
    data = await li.inbox()
    threads = {}
    for ref in references(data, "conversation"):
        match = re.search(r"/messaging/thread/([^/?#]+)", ref.get("url", ""))
        if match and ref.get("text"):
            threads[_name_key(ref["text"])] = match.group(1)
    try:
        inbox = await run.llm.ask(prompts.PARSE_INBOX, untrusted("inbox", section_text(data)), prompts.Inbox, effort="low")
    except LLMError as e:
        run.note(f"Couldn't read the inbox: {e}")
        return

    jobs = []
    try:
        for row in inbox.conversations:
            founder = next((f for f in active if _same_person(row.name, f["name"])), None)
            if not founder or row.last_from_me or row.snippet.strip() == founder["inbox_snippet"]:
                continue
            thread_id = threads.get(_name_key(row.name)) or founder["thread_id"]
            try:
                convo = await (li.conversation(thread_id=thread_id) if thread_id else li.conversation(username=founder["username"]))
            except LinkedInPaused:
                raise
            except LinkedInError as e:
                run.note(f"Couldn't open the conversation with {founder['name']}: {e}")
                continue
            run.store.update_founder(founder["id"], inbox_snippet=row.snippet.strip(), thread_id=thread_id)
            jobs.append(asyncio.create_task(_read_conversation(run, founder["id"], section_text(convo))))
    finally:
        await asyncio.gather(*jobs)


async def _read_conversation(run: Run, founder_id: int, raw: str) -> None:
    founder = run.store.founder(founder_id)
    task = prompts.fill(prompts.PARSE_CONVERSATION, run.cfg, them=founder["name"])
    try:
        convo = await run.llm.ask(task, untrusted("conversation", raw), prompts.Conversation, effort="low")
    except LLMError as e:
        run.note(f"Couldn't read the conversation with {founder['name']}: {e}")
        return
    if not convo.messages:
        return

    transcript = "\n\n".join(f"{run.cfg.first_name if m.from_me else founder['name']}: {m.text}" for m in convo.messages)
    run.store.update_founder(founder_id, transcript=transcript[-20000:], connection="connected")
    new = [run.store.add_message(founder_id, "in", "inbound", m.text.strip(), "received")
           for m in convo.messages if not m.from_me and m.text.strip()]
    if any(new) and not convo.messages[-1].from_me:
        run.note(f"{founder['name']} replied")
        # They wrote first (e.g. answered the connection note), so the queued opener would be a double message.
        run.store.supersede(founder_id, ("dm",))
        await draft_reply(run, founder_id)


async def draft_reply(run: Run, founder_id: int, feedback: str = "") -> None:
    founder = run.store.founder(founder_id)
    content = (
        _about(founder)
        + f"\n\n<research>\n{founder['brief']}\n</research>\n\n"
        + untrusted("conversation so far, oldest first", founder["transcript"])
    )
    if feedback:
        old = run.store.latest(founder_id, "reply", OPEN_DRAFT)
        content += f"\n\n<previous_draft>\n{old['body'] if old else ''}\n</previous_draft>\n\n<feedback_from_{run.cfg.first_name.lower()}>\n{feedback}\n</feedback_from_{run.cfg.first_name.lower()}>"
    task = prompts.fill(prompts.REPLY, run.cfg, them=founder["name"])
    try:
        reply = await run.llm.ask(task, content, prompts.Reply, effort="high")
    except LLMError as e:
        run.store.update_founder(founder_id, stage="needs_you", needs_you=f"They replied but drafting failed: {e}")
        return

    check = _check(run, reply.reply, founder, run.cfg.messaging.message_max_chars)
    needs_you = reply.needs_you or reply.category not in run.cfg.autonomy.auto_reply_categories
    run.store.supersede(founder_id, ("reply",))
    run.store.add_message(founder_id, "out", "reply", reply.reply.strip(), "review",
                          category=reply.category, warnings=check.all())
    if reply.category == "not_interested":
        stage, why = "closed", ""
    elif needs_you:
        stage, why = "needs_you", reply.reason or reply.category.replace("_", " ")
    else:
        stage, why = "replied", ""
    run.store.update_founder(founder_id, stage=stage, needs_you=why)


# -- one full cycle -----------------------------------------------------------


async def run_cycle(run: Run, *, sheet=None, discover_new: bool = True, force_discover: bool = False, send: bool = True) -> None:
    from .sheets import sync  # sheets pulls in Google libraries, only needed when connected

    if sheet:
        run.on_phase("Reading your decisions from the sheet")
        await sync(run, sheet)
    await process_redrafts(run)

    if reason := run.store.pause_state():
        run.note(f"Paused: {reason}. LinkedIn wasn't touched.")
    else:
        try:
            run.on_phase("Starting the LinkedIn browser")
            async with LinkedIn(run.cfg, run.store) as li:
                if send:
                    await send_approved(run, li)
                await check_inbox(run, li)
                if discover_new:
                    await discover(run, li, force=force_discover)
                await research(run, li)
                if auto_approve(run) and send:
                    await send_approved(run, li)
        except LinkedInPaused as e:
            until = utcnow() + e.retry_after if e.retry_after else None
            run.store.pause(str(e), until)
            run.note(f"Stopped: {e}")
        except LinkedInError as e:
            run.note(f"LinkedIn problem: {e}")

    if sheet:
        run.on_phase("Updating the sheet")
        await sync(run, sheet)
    run.store.set("last_run", stamp())
