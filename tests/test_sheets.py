"""The sheet layer, with Google replaced by small stubs."""

from igothired import sheets
from igothired.pipeline import run_cycle
from igothired.sheets import Payload, SheetNotPrivate, SheetState, sync


class MemorySheet:
    """Implements the read/write surface sync() uses."""

    def __init__(self):
        self.decisions = []
        self.paused = False
        self.private = True
        self.written: Payload | None = None

    def read(self):
        if not self.private:
            raise SheetNotPrivate("shared")
        return SheetState(decisions=self.decisions, paused=self.paused)

    def write(self, payload):
        self.written = payload


async def test_queue_rows_and_approval_round_trip(run, linkedin):
    sheet = MemorySheet()
    await run_cycle(run, sheet=sheet)
    rows = sheet.written.queue
    assert len(rows) == 1 and rows[0][0].startswith("F") and rows[0][8] == "First touch"
    assert rows[0][12] == "https://www.linkedin.com/in/ada-founder/"

    from igothired.pipeline import Decision

    sheet.decisions = [Decision(item=rows[0][0], action="approve", note=rows[0][9], message="Hand edited.")]
    await sync(run, sheet)
    assert sheet.written.queue == []  # approved rows leave the queue
    assert run.store.founder_by_username("ada-founder")["stage"] == "approved"


async def test_shared_sheet_is_ignored(run):
    sheet = MemorySheet()
    await run_cycle(run, sheet=sheet)
    founder = run.store.founder_by_username("ada-founder")

    from igothired.pipeline import Decision

    sheet.private = False
    sheet.decisions = [Decision(item=f"F{founder['id']}", action="approve", message="")]
    sheet.written = None
    await sync(run, sheet)
    assert run.store.founder(founder["id"])["stage"] == "review"
    assert sheet.written is None  # nothing written to a shared sheet either
    assert any("shared" in n for n in run.notes)


async def test_paused_box_stops_linkedin(run, linkedin):
    sheet = MemorySheet()
    sheet.paused = True
    await run_cycle(run, sheet=sheet)
    assert linkedin.calls() == []


class _Response:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _StubSpreadsheet:
    def __init__(self, values, paused=False):
        self.values = values
        self.paused = paused
        self.updates = []

    def values_batch_get(self, ranges, params=None):
        return {"valueRanges": [{"values": self.values}, {"values": [[self.paused]]}]}

    def values_batch_clear(self, body=None):
        pass

    def values_batch_update(self, body):
        self.updates.append(body)


def _stub_sheet(cfg, permissions, values=(), paused=False):
    sheet = sheets.Sheet.__new__(sheets.Sheet)
    sheet.cfg, sheet.id = cfg, "abc"
    sheet.sh = _StubSpreadsheet(list(values), paused)
    sheet.client = type("C", (), {"http_client": type("H", (), {"request": lambda self, *a, **k: _Response({"permissions": permissions})})()})()
    return sheet


def test_sharing_rules(cfg):
    owner = {"type": "user", "role": "owner", "emailAddress": "me@x.com"}
    assert _stub_sheet(cfg, [owner]).sharing_problems() == []
    assert _stub_sheet(cfg, [owner, {"type": "anyone", "role": "reader"}]).sharing_problems()
    assert _stub_sheet(cfg, [owner, {"type": "user", "role": "writer", "emailAddress": "friend@x.com"}]).sharing_problems()
    cfg.sheet.allowed_viewers = ["Mentor@x.com"]
    assert _stub_sheet(cfg, [owner, {"type": "user", "role": "reader", "emailAddress": "mentor@x.com"}]).sharing_problems() == []
    assert _stub_sheet(cfg, [owner, {"type": "user", "role": "writer", "emailAddress": "mentor@x.com"}]).sharing_problems()


def test_read_parses_decisions_and_ignores_junk(cfg):
    owner = {"type": "user", "role": "owner"}
    values = [
        ["F3", "Approve", "", "Ada", "", "", "", "", "First touch", "note text", "message text"],
        ["R9", "Redraft", "shorter please"],
        ["=1+1", "Approve"],
        [],
    ]
    state = _stub_sheet(cfg, [owner], values, paused=True).read()
    assert [d.item for d in state.decisions] == ["F3", "R9"]
    assert state.decisions[0].action == "approve" and state.decisions[0].note == "note text"
    assert state.decisions[1].feedback == "shorter please" and state.decisions[1].note is None
    assert state.paused is True


def test_writes_are_raw(cfg):
    sheet = _stub_sheet(cfg, [])
    sheet.write(Payload(queue=[["F1", "", "", '=HYPERLINK("http://evil")']], pipeline=[], activity=[], settings=["review", "", "", ""]))
    body = sheet.sh.updates[0]
    assert body["valueInputOption"] == "RAW"
    assert ['F1', '', '', '=HYPERLINK("http://evil")'] in body["data"][1]["values"]
