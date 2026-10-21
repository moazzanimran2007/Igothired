"""Google Sheets as the control panel.

The sheet is a view. The local database is the source of truth: each sync
reads your decisions from the Queue tab, applies them locally, then rewrites
the tabs from the database.

Two things keep it safe:
- Every value is written with valueInputOption=RAW. Text from LinkedIn can be
  written by anyone, and a post starting with "=IMPORTXML(...)" must stay text,
  never become a formula that can send your sheet's contents somewhere.
- Anyone who can edit the sheet can approve messages from your LinkedIn. Before
  reading decisions, the sync checks who the sheet is shared with and refuses
  to use it if that's anyone but you (plus read-only viewers you list).

Access uses the drive.file scope, so the token can only touch the one sheet
this app created, not the rest of your Drive.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import gspread
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .config import Config
from .db import Store, parse_stamp, stamp, warnings_of
from .paths import GOOGLE_CLIENT_PATH, GOOGLE_TOKEN_PATH, SHEET_PATH, ensure_home, write_private
from .pipeline import OPEN_DRAFT, Decision, Run, apply_decisions, quota
from .safety import cell, profile_url

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
DECISIONS = ["Approve", "Skip", "Redraft"]

QUEUE = ["ID", "Decision", "Feedback for redraft", "Founder", "Company", "Fit", "Why them",
         "Their words", "Type", "Connection note", "Message", "Checks", "Profile"]
PIPELINE = ["Founder", "Company", "Stage", "Fit", "Why them", "Notes", "Updated", "Profile"]
ACTIVITY = ["When", "Founder", "What", "Text"]
SETTINGS = ["Setting", "Value", "Notes"]

WIDTHS = {
    "Queue": [56, 100, 170, 140, 130, 44, 240, 260, 90, 260, 380, 230, 200],
    "Pipeline": [160, 150, 130, 44, 300, 260, 120, 220],
    "Activity": [120, 160, 150, 600],
    "Settings": [130, 220, 460],
}
STAGES = {
    "new": "Found",
    "review": "Waiting for you",
    "approved": "Approved",
    "invited": "Invite sent",
    "contacted": "Messaged",
    "replied": "They replied",
    "needs_you": "Needs you",
    "closed": "Closed",
    "skipped": "Skipped",
    "no_response": "No response",
    "unreachable": "Can't message",
    "error": "Error",
}


class SheetError(Exception):
    pass


class SheetNotPrivate(SheetError):
    pass


@dataclass
class SheetState:
    decisions: list[Decision]
    paused: bool


@dataclass
class Payload:
    queue: list[list]
    pipeline: list[list]
    activity: list[list]
    settings: list[object]


# -- auth -----------------------------------------------------------------------


def is_connected() -> bool:
    return GOOGLE_TOKEN_PATH.exists() and SHEET_PATH.exists()


def credentials() -> Credentials:
    if not GOOGLE_TOKEN_PATH.exists():
        raise SheetError("Google isn't connected. Run: igothired connect-google")
    creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_PATH), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise SheetError(
                "Google access expired. Run: igothired connect-google "
                "(if this happens every week, set your OAuth app to 'In production', see the README)"
            ) from e
        write_private(GOOGLE_TOKEN_PATH, creds.to_json())
    return creds


def connect(client_secret: Path, cfg: Config) -> str:
    """One-time setup: authorize in the browser, create and format the sheet. Returns its URL."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = json.loads(client_secret.read_text())
    if "installed" not in secret:
        raise SheetError("That file isn't a Desktop app OAuth client. Create one of type 'Desktop app' (see the README).")
    ensure_home()
    if client_secret.resolve() != GOOGLE_CLIENT_PATH.resolve():
        shutil.copyfile(client_secret, GOOGLE_CLIENT_PATH)
    GOOGLE_CLIENT_PATH.chmod(0o600)

    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CLIENT_PATH), SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=0,
        open_browser=True,
        authorization_prompt_message="Opening Google in your browser. If nothing opens, visit:\n{url}",
        success_message="Igothired is connected. You can close this tab.",
    )
    write_private(GOOGLE_TOKEN_PATH, creds.to_json())

    client = gspread.authorize(creds)
    if SHEET_PATH.exists():
        try:
            sh = client.open_by_key(json.loads(SHEET_PATH.read_text())["id"])
            return sh.url
        except (gspread.exceptions.APIError, gspread.exceptions.SpreadsheetNotFound, KeyError):
            pass  # deleted or from another account, so make a fresh one
    sh = _create(client, cfg)
    write_private(SHEET_PATH, json.dumps({"id": sh.id, "url": sh.url}))
    return sh.url


# -- creating and formatting ----------------------------------------------------


def _rgb(hex_color: str) -> dict[str, float]:
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _create(client: gspread.Client, cfg: Config) -> gspread.Spreadsheet:
    sh = client.create(cfg.sheet.title)
    queue = sh.sheet1
    queue.update_title("Queue")
    queue.resize(rows=500, cols=len(QUEUE))
    sh.add_worksheet("Pipeline", rows=1000, cols=len(PIPELINE))
    sh.add_worksheet("Activity", rows=1000, cols=len(ACTIVITY))
    sh.add_worksheet("Settings", rows=12, cols=len(SETTINGS))

    sh.values_batch_update({
        "valueInputOption": "RAW",
        "data": [
            {"range": "Queue!A1", "values": [QUEUE]},
            {"range": "Pipeline!A1", "values": [PIPELINE]},
            {"range": "Activity!A1", "values": [ACTIVITY]},
            {"range": "Settings!A1", "values": [
                SETTINGS,
                ["Paused", False, "Tick to stop everything on LinkedIn. Checked at the start of every run."],
                ["Mode", "", "review: nothing goes out until you approve it. auto: clean drafts send on their own. Change it in config.yaml."],
                ["Last sync", "", ""],
                ["Last 24 hours", "", "Limits are in config.yaml. They keep the account well under LinkedIn's radar."],
                ["Status", "", ""],
                ["How to use", "", "Queue: read the draft, edit it right in the cell if you like, then pick Approve, Skip or Redraft (say what to change in the Feedback column). Decisions are picked up on the next run or `igothired sync`."],
            ]},
        ],
    })
    _format(sh)
    return sh


def _format(sh: gspread.Spreadsheet) -> None:
    ids = {ws.title: ws.id for ws in sh.worksheets()}
    header, band, white = _rgb("#1f2430"), _rgb("#f4f6f9"), _rgb("#ffffff")
    editable = _rgb("#eef4ff")
    requests: list[dict] = []

    for title, columns in (("Queue", QUEUE), ("Pipeline", PIPELINE), ("Activity", ACTIVITY), ("Settings", SETTINGS)):
        sid = ids[title]
        requests += [
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1},
                "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP", "textFormat": {"fontSize": 10}}},
                "fields": "userEnteredFormat(verticalAlignment,wrapStrategy,textFormat)",
            }},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": header,
                    "textFormat": {"bold": True, "foregroundColor": white, "fontSize": 10},
                    "verticalAlignment": "MIDDLE",
                    "padding": {"top": 6, "bottom": 6, "left": 6, "right": 6},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,padding)",
            }},
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 32},
                "fields": "pixelSize",
            }},
        ]
        requests += [
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }}
            for i, width in enumerate(WIDTHS[title])
        ]
        if title in ("Pipeline", "Activity"):
            requests.append({"addBanding": {"bandedRange": {
                "range": {"sheetId": sid, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": len(columns)},
                "rowProperties": {"headerColor": header, "firstBandColor": white, "secondBandColor": band},
            }}})
            # These tabs are rewritten on every sync, so editing them by hand is pointless.
            requests.append({"addProtectedRange": {"protectedRange": {
                "range": {"sheetId": sid}, "warningOnly": True,
                "description": "Written by Igothired on every sync",
            }}})

    queue = ids["Queue"]
    # The columns you're meant to type in get a light tint.
    for col in (1, 2, 9, 10):
        requests.append({"repeatCell": {
            "range": {"sheetId": queue, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1},
            "cell": {"userEnteredFormat": {"backgroundColor": editable}},
            "fields": "userEnteredFormat.backgroundColor",
        }})
    requests.append({"setDataValidation": {
        "range": {"sheetId": queue, "startRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 2},
        "rule": {
            "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in DECISIONS]},
            "strict": True,
            "showCustomUi": True,
        },
    }})
    row_colors = {"Approve": "#e3f5e7", "Skip": "#ececec", "Redraft": "#fff5d6"}
    for value, color in row_colors.items():
        requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": queue, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(QUEUE)}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f'=$B2="{value}"'}]},
                "format": {"backgroundColor": _rgb(color)},
            },
        }}})
    requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [{"sheetId": queue, "startRowIndex": 1, "startColumnIndex": 8, "endColumnIndex": 9}],
        "booleanRule": {
            "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "Needs you"}]},
            "format": {"backgroundColor": _rgb("#fde2e1"), "textFormat": {"bold": True}},
        },
    }}})
    for start, end in ((0, 1), (3, 9), (11, 13)):
        requests.append({"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": queue, "startRowIndex": 1, "startColumnIndex": start, "endColumnIndex": end},
            "warningOnly": True,
            "description": "Filled in by Igothired",
        }}})

    settings = ids["Settings"]
    requests += [
        {"setDataValidation": {
            "range": {"sheetId": settings, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 1, "endColumnIndex": 2},
            "rule": {"condition": {"type": "BOOLEAN"}},
        }},
        {"repeatCell": {
            "range": {"sheetId": settings, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }},
    ]
    sh.batch_update({"requests": requests})


# -- reading and writing ----------------------------------------------------------


class Sheet:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not SHEET_PATH.exists():
            raise SheetError("No sheet yet. Run: igothired connect-google")
        self.client = gspread.authorize(credentials())
        self.id = json.loads(SHEET_PATH.read_text())["id"]
        try:
            self.sh = self.client.open_by_key(self.id)
        except gspread.exceptions.SpreadsheetNotFound as e:
            raise SheetError("The sheet is gone or not accessible. Run: igothired connect-google") from e

    @property
    def url(self) -> str:
        return self.sh.url

    def sharing_problems(self) -> list[str]:
        response = self.client.http_client.request(
            "get",
            f"https://www.googleapis.com/drive/v3/files/{self.id}/permissions",
            params={"fields": "permissions(type,role,emailAddress)"},
        )
        allowed = {e.lower() for e in self.cfg.sheet.allowed_viewers}
        problems = []
        for p in response.json().get("permissions", []):
            role, kind, email = p.get("role"), p.get("type"), (p.get("emailAddress") or "").lower()
            if role == "owner":
                continue
            if kind == "user" and email in allowed and role in ("reader", "commenter"):
                continue
            who = email or ("anyone with the link" if kind == "anyone" else kind)
            problems.append(f"{who} can {'edit' if role == 'writer' else 'view'}")
        return problems

    def read(self) -> SheetState:
        if problems := self.sharing_problems():
            raise SheetNotPrivate(
                "The sheet is shared beyond you (" + "; ".join(problems) + "). Nothing was read from or written to it. "
                "Anyone who can see it sees founders' details, and anyone who can edit it could send messages from your LinkedIn. "
                "Remove the sharing, or list read-only viewers under sheet.allowed_viewers in config.yaml."
            )
        result = self.sh.values_batch_get(
            [f"Queue!A2:{_col(len(QUEUE))}", "Settings!B2"],
            params={"valueRenderOption": "UNFORMATTED_VALUE"},
        )
        queue_values = result["valueRanges"][0].get("values", [])
        paused_values = result["valueRanges"][1].get("values", [[False]])
        paused = paused_values[0][0] if paused_values and paused_values[0] else False

        decisions = []
        for row in queue_values:
            row = [str(v) if v is not None else "" for v in row] + [""] * (len(QUEUE) - len(row))
            item = row[0].strip()
            if len(item) < 2 or item[0] not in "FRN" or not item[1:].isdigit():
                continue
            decisions.append(Decision(
                item=item,
                action=row[1].strip().lower(),
                feedback=row[2].strip(),
                note=row[9] if item.startswith("F") else None,
                message=row[10],
            ))
        return SheetState(decisions=decisions, paused=paused is True or str(paused).upper() == "TRUE")

    def write(self, payload: Payload) -> None:
        self.sh.values_batch_clear(body={"ranges": [f"Queue!A2:{_col(len(QUEUE))}", f"Pipeline!A2:{_col(len(PIPELINE))}"]})
        data = [{"range": "Settings!B3", "values": [[cell(v)] for v in payload.settings]}]
        if payload.queue:
            data.append({"range": "Queue!A2", "values": [[cell(v) for v in row] for row in payload.queue]})
        if payload.pipeline:
            data.append({"range": "Pipeline!A2", "values": [[cell(v) for v in row] for row in payload.pipeline]})
        self.sh.values_batch_update({"valueInputOption": "RAW", "data": data})
        if payload.activity:
            rows = [[cell(v) for v in row] for row in reversed(payload.activity)]  # newest on top
            self.sh.worksheet("Activity").insert_rows(rows, row=2, value_input_option="RAW")


def _col(n: int) -> str:
    return chr(ord("A") + n - 1)


# -- building rows from the database ------------------------------------------------


def _local(ts: str | None) -> str:
    if not ts:
        return ""
    return parse_stamp(ts).astimezone().strftime("%b %d, %H:%M")


def _bullets(warnings: list[str]) -> str:
    return "\n".join(f"• {w}" for w in warnings)


def queue_rows(store: Store, cfg: Config) -> list[list]:
    needs, replies, firsts = [], [], []
    msg_max, note_max = cfg.messaging.message_max_chars, cfg.messaging.note_max_chars

    for reply in store.messages(kinds=("reply",), statuses=OPEN_DRAFT):
        f = store.founder(reply["founder_id"])
        inbound = store.latest(f["id"], "inbound", ("received",))
        flagged = f["stage"] == "needs_you"
        checks = []
        if flagged and f["needs_you"]:
            checks.append(f"Needs you: {f['needs_you']}")
        if reply["status"] == "redraft":
            checks.append("Redrafting with your feedback")
        checks += warnings_of(reply)
        checks.append(f"{len(reply['body'])}/{msg_max} characters")
        row = [f"R{reply['id']}", "", "", f["name"], f["company"], f["fit_score"] or "", f["fit_reason"],
               inbound["body"] if inbound else "", "Needs you" if flagged else "Reply", "", reply["body"],
               _bullets(checks), profile_url(f["username"])]
        (needs if flagged else replies).append(row)

    with_reply = {r["founder_id"] for r in store.messages(kinds=("reply",), statuses=OPEN_DRAFT)}
    for f in store.founders("needs_you"):
        if f["id"] in with_reply:
            continue
        inbound = store.latest(f["id"], "inbound", ("received",))
        needs.append([f"N{f['id']}", "", "", f["name"], f["company"], f["fit_score"] or "", f["fit_reason"],
                      inbound["body"] if inbound else "", "Needs you", "", "",
                      _bullets([f["needs_you"], "Sort it out on LinkedIn, then pick Approve (done) or Skip (dismiss)."]),
                      profile_url(f["username"])])

    for f in store.founders("review", order="fit_score DESC"):
        note = store.latest(f["id"], "note", OPEN_DRAFT)
        dm = store.latest(f["id"], "dm", OPEN_DRAFT)
        if not dm:
            continue
        checks = []
        if dm["status"] == "redraft":
            checks.append("Redrafting with your feedback")
        checks += (warnings_of(note) if note else []) + warnings_of(dm)
        sizes = f"message {len(dm['body'])}/{msg_max}"
        if note:
            sizes = f"note {len(note['body'])}/{note_max}, " + sizes
        checks.append(sizes + " characters")
        firsts.append([f"F{f['id']}", "", "", f["name"], f["company"], f["fit_score"] or "", f["fit_reason"],
                       f["signal"], "First touch", note["body"] if note else "", dm["body"],
                       _bullets(checks), profile_url(f["username"])])
    return needs + replies + firsts


def pipeline_rows(store: Store) -> list[list]:
    rows = []
    for f in store.founders():
        if f["stage"] == "new":
            continue
        rows.append([f["name"], f["company"], STAGES.get(f["stage"], f["stage"]), f["fit_score"] or "",
                     f["fit_reason"], f["needs_you"] or f["error"], _local(f["updated_at"]), profile_url(f["username"])])
    return rows


def activity_rows(store: Store, since: str) -> list[list]:
    what = {"note": "Connection note", "dm": "Message", "reply": "Reply"}
    rows = []
    for m in store.activity_since(since):
        if m["direction"] == "in":
            rows.append([_local(m["created_at"]), m["founder_name"], "They wrote", m["body"]])
        elif m["status"] == "sent":
            rows.append([_local(m["sent_at"]), m["founder_name"], f"Sent {what.get(m['kind'], m['kind']).lower()}", m["body"]])
        elif m["status"] == "unknown":
            rows.append([_local(m["sent_at"]), m["founder_name"], "Unconfirmed send", m["body"]])
    return rows


def settings_values(run: Run) -> list[object]:
    left = quota(run.cfg, run.store)
    lim = run.cfg.limits
    used = f"{lim.invites_per_day - left['invites']} of {lim.invites_per_day} invites, " \
           f"{lim.messages_per_day - left['messages']} of {lim.messages_per_day} messages"
    reason = run.store.get("pause_reason")
    status = f"Paused: {reason}" if reason else "Running normally"
    return [run.cfg.autonomy.mode, datetime.now().astimezone().strftime("%b %d, %H:%M"), used, status]


async def sync(run: Run, sheet: Sheet) -> None:
    """Read decisions, apply them, write everything back. Read and write happen back to back so edits aren't lost."""
    try:
        state = await asyncio.to_thread(sheet.read)
    except SheetNotPrivate as e:
        run.note(str(e))
        return
    except (gspread.exceptions.APIError, SheetError, RefreshError) as e:
        run.note(f"Couldn't read the sheet: {e}")
        return

    apply_decisions(run, state.decisions)
    run.store.set("sheet_paused", "1" if state.paused else "0")
    synced_at = stamp()
    payload = Payload(
        queue=queue_rows(run.store, run.cfg),
        pipeline=pipeline_rows(run.store),
        activity=activity_rows(run.store, run.store.get("activity_synced_at", "")),
        settings=settings_values(run),
    )
    try:
        await asyncio.to_thread(sheet.write, payload)
        run.store.set("activity_synced_at", synced_at)
    except (gspread.exceptions.APIError, RefreshError) as e:
        run.note(f"Couldn't update the sheet: {e}")
