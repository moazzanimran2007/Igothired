from __future__ import annotations

import argparse
import asyncio
import fcntl
import getpass
import logging
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import scheduler, sheets
from .config import config_path, load_config
from .db import Store, parse_stamp
from .linkedin import LinkedIn, LinkedInError, server_params
from .llm import LLM, LLMError
from .paths import HOME, LOCK_PATH, LOG_DIR, REPO_ROOT, SECRETS_PATH, ensure_home, load_secrets, save_secret, search_path
from .pipeline import Run, process_redrafts, quota, run_cycle
from .profile import load_profile
from .safety import username_from

console = Console(highlight=False)

OK, BAD, SKIP = "[green]✓[/]", "[red]✗[/]", "[dim]–[/]"


def _store() -> Store:
    ensure_home()
    return Store()


def _need_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("No Anthropic API key yet. Run: igothired setup")


@contextmanager
def _single_run():
    ensure_home()
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit("Another run is already going. Try again when it finishes.") from None
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _open_sheet(cfg):
    if not sheets.is_connected():
        return None
    try:
        return sheets.Sheet(cfg)
    except Exception as e:  # a broken sheet shouldn't stop LinkedIn work
        console.print(f"[yellow]Sheet unavailable:[/] {e}")
        return None


def _notify(text: str) -> None:
    # Only ever called with our own fixed text, never with anything from LinkedIn.
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "Igothired"'], capture_output=True)


# -- commands -----------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> None:
    ensure_home()
    console.print(Panel.fit("[bold]Igothired setup[/]", border_style="dim"))

    if os.environ.get("ANTHROPIC_API_KEY"):
        console.print(f"{OK} Anthropic API key found")
    else:
        key = getpass.getpass("Paste your Anthropic API key (input is hidden): ").strip()
        if not key.startswith("sk-ant-"):
            raise SystemExit("That doesn't look like an Anthropic key (they start with sk-ant-).")
        save_secret("ANTHROPIC_API_KEY", key)
        console.print(f"{OK} Saved to {SECRETS_PATH} (readable only by you)")

    # Your own copies are gitignored, so personal details never end up in the repo.
    for mine, example in (("config.yaml", "config.example.yaml"), ("profile/me.md", "profile/me.example.md")):
        if not (REPO_ROOT / mine).exists():
            shutil.copyfile(REPO_ROOT / example, REPO_ROOT / mine)
            console.print(f"{OK} Created {mine} from the example. Fill it in.")

    cfg = load_config()
    profile = load_profile(cfg.profile_file())
    console.print(f"{OK} Profile loaded: {len(profile.text.split())} words, {len(profile.links)} links")

    if shutil.which("uvx", path=search_path()):
        console.print(f"{OK} uv is installed")
    else:
        console.print(f"{BAD} uv isn't installed. Run: curl -LsSf https://astral.sh/uv/install.sh | sh")

    console.print("\nNext:\n  1. [bold]igothired connect-linkedin[/]\n  2. [bold]igothired connect-google ~/Downloads/client_secret_XXXX.json[/]\n  3. [bold]igothired run[/]")


def cmd_connect_linkedin(args: argparse.Namespace) -> None:
    cfg, store = load_config(), _store()
    params = server_params(cfg, "--import-from-browser" if args.from_browser else "--login")
    if args.from_browser:
        console.print("Reusing the LinkedIn session from your browser. macOS may ask for keychain access once.")
    else:
        console.print("A browser window will open. Sign in to LinkedIn there, 2FA included. Igothired never sees your password.")
    subprocess.run([params.command, *params.args], env=params.env, check=False)

    async def whoami():
        async with LinkedIn(cfg, store) as li:
            return await li.me()

    with console.status("Checking the connection…"):
        try:
            me = asyncio.run(whoami())
        except LinkedInError as e:
            raise SystemExit(f"LinkedIn isn't connected yet: {e}") from e
    username = username_from(str(me.get("url", "")))
    if username:
        store.set("me_username", username)
    store.resume()
    console.print(f"{OK} LinkedIn connected" + (f" as linkedin.com/in/{username}" if username else ""))


def cmd_connect_google(args: argparse.Namespace) -> None:
    cfg = load_config()
    path = Path(args.client_secret).expanduser()
    if not path.exists():
        raise SystemExit(f"Can't find {path}")
    url = sheets.connect(path, cfg)
    console.print(f"{OK} Your sheet is ready: [link={url}]{url}[/link]")
    console.print("[dim]Keep it private. If it's ever shared, Igothired stops reading it.[/]")


def cmd_run(args: argparse.Namespace) -> None:
    _need_api_key()
    cfg, store = load_config(), _store()
    profile = load_profile(cfg.profile_file())
    sheet = _open_sheet(cfg)
    run = Run(cfg, store, LLM(cfg, profile), profile, dry_run=args.dry_run, ignore_window=args.now)

    def cycle():
        return run_cycle(run, sheet=sheet, discover_new=not args.no_discover, force_discover=args.discover, send=not args.no_send)

    with _single_run():
        if args.quiet:
            asyncio.run(cycle())
        else:
            with console.status("Starting…", spinner="dots") as status:
                run.on_phase = lambda text: status.update(f"{text}…")
                asyncio.run(cycle())

    if args.quiet:
        for line in run.notes:
            print(line)
        replied = sum(1 for n in run.notes if n.endswith(" replied"))
        if replied:
            _notify(f"{replied} founder{'s' if replied != 1 else ''} replied. Drafts are in your sheet.")
        return
    _summary(run, store, sheet)


def _summary(run: Run, store: Store, sheet) -> None:
    lines = "\n".join(f"• {n}" for n in run.notes) or "Nothing new this time."
    console.print(Panel(lines, title="This run", title_align="left", border_style="dim"))
    counts = store.stage_counts()
    left = quota(run.cfg, store)
    waiting = counts.get("review", 0) + counts.get("needs_you", 0) + counts.get("replied", 0)
    console.print(
        f"[bold]{waiting}[/] waiting for you · {counts.get('invited', 0)} invites out · "
        f"{counts.get('contacted', 0)} messaged · {left['invites']} invites and {left['messages']} messages left today"
    )
    if sheet:
        console.print(f"[dim]Sheet:[/] [link={sheet.url}]{sheet.url}[/link]")


def cmd_sync(args: argparse.Namespace) -> None:
    _need_api_key()
    cfg, store = load_config(), _store()
    sheet = _open_sheet(cfg)
    if not sheet:
        raise SystemExit("No sheet connected. Run: igothired connect-google")
    profile = load_profile(cfg.profile_file())
    run = Run(cfg, store, LLM(cfg, profile), profile)

    async def go():
        await sheets.sync(run, sheet)
        if store.messages(statuses=("redraft",)):
            await process_redrafts(run)
            await sheets.sync(run, sheet)

    with _single_run(), console.status("Syncing the sheet…"):
        asyncio.run(go())
    for line in run.notes:
        console.print(f"• {line}")
    console.print(f"{OK} Synced")


def cmd_status(args: argparse.Namespace) -> None:
    cfg, store = load_config(), _store()
    counts = store.stage_counts()
    table = Table(title="Pipeline", title_justify="left", show_header=False, box=None, padding=(0, 2))
    for stage, label in sheets.STAGES.items():
        if counts.get(stage):
            table.add_row(label, str(counts[stage]))
    console.print(table if counts else "Nothing in the pipeline yet. Run: igothired run")

    flagged = store.founders("needs_you")
    if flagged:
        console.print("\n[bold]Needs you[/]")
        for f in flagged:
            console.print(f"  • {f['name']}: {f['needs_you']}")

    left = quota(cfg, store)
    console.print(f"\nLeft in the next 24h: {left['invites']} invites, {left['messages']} messages")
    if reason := store.pause_state():
        console.print(f"[yellow]Paused:[/] {reason}")
    last = store.get("last_run")
    console.print(f"Last run: {parse_stamp(last).astimezone():%b %d, %H:%M}" if last else "Never run yet")
    console.print(f"Mode: {cfg.autonomy.mode}")


def cmd_add(args: argparse.Namespace) -> None:
    username = username_from(args.url)
    if not username:
        raise SystemExit("That doesn't look like a LinkedIn profile URL.")
    store = _store()
    added = store.add_founder(username, name=username, signal=args.why or "Added by hand", source="added by you")
    console.print(f"{OK} Added. They'll be researched on the next run." if added else "Already in the pipeline.")


def cmd_pause(args: argparse.Namespace) -> None:
    _store().pause(args.reason or "paused by you")
    console.print("Paused. Nothing will touch LinkedIn until you run: igothired resume")


def cmd_resume(args: argparse.Namespace) -> None:
    store = _store()
    store.resume()
    console.print(f"{OK} Resumed")
    if store.get("sheet_paused") == "1":
        console.print("[yellow]The Paused box in the sheet is still ticked. Untick it too.[/]")


def cmd_schedule(args: argparse.Namespace) -> None:
    if args.off:
        console.print(f"{OK} Schedule removed" if scheduler.remove() else "There was no schedule.")
        return
    cfg = load_config()
    times = []
    for t in args.times.split(","):
        hour, _, minute = t.strip().partition(":")
        times.append((int(hour), int(minute or 0)))
    scheduler.install(times, cfg.limits.send_window.weekdays)
    console.print(f"{OK} Runs on its own at {args.times} on weekdays. Log: {LOG_DIR / 'scheduled.log'}")


def cmd_doctor(args: argparse.Namespace) -> None:
    table = Table(show_header=False, box=None, padding=(0, 1))

    def row(ok: bool | None, name: str, detail: str = "") -> None:
        table.add_row(OK if ok else SKIP if ok is None else BAD, name, f"[dim]{detail}[/]")

    try:
        cfg = load_config()
        row(True, "Config", str(config_path()))
    except Exception as e:
        row(False, "Config", str(e))
        console.print(table)
        return

    try:
        profile = load_profile(cfg.profile_file())
        row(bool(profile.links), "Profile", f"{len(profile.text.split())} words, {len(profile.links)} links, {len(profile.emails)} email")
    except FileNotFoundError as e:
        row(False, "Profile", str(e))

    private = HOME.exists() and not HOME.stat().st_mode & 0o077
    private &= not SECRETS_PATH.exists() or not SECRETS_PATH.stat().st_mode & 0o077
    row(private, "Private folder", f"{HOME} is owner-only" if private else f"fix with: chmod -R go-rwx {HOME}")

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic

            anthropic.Anthropic().models.retrieve(cfg.model.name)
            row(True, "Claude API", cfg.model.name)
        except Exception as e:
            row(False, "Claude API", str(e)[:120])
    else:
        row(False, "Claude API", "no key, run: igothired setup")

    uvx = shutil.which("uvx", path=search_path())
    row(bool(uvx), "uv", uvx or "install from https://docs.astral.sh/uv/")

    if args.offline:
        row(None, "LinkedIn", "skipped (--offline)")
    elif uvx:
        store = _store()

        async def probe():
            async with LinkedIn(cfg, store) as li:
                tools = await li.list_tools()
                me = await li.me()
                return tools, me

        try:
            with console.status("Starting the LinkedIn server…"):
                tools, me = asyncio.run(probe())
            missing = {"search_posts", "get_person_profile", "connect_with_person", "send_message", "get_inbox"} - set(tools)
            row(not missing, "LinkedIn server", f"{cfg.linkedin.args[0]}, {len(tools)} tools" + (f", missing {missing}" if missing else ""))
            row(True, "LinkedIn login", str(me.get("url", "signed in")))
        except LinkedInError as e:
            row(False, "LinkedIn login", f"{e} (run: igothired connect-linkedin)")

    if sheets.is_connected():
        try:
            sheet = sheets.Sheet(cfg)
            problems = sheet.sharing_problems()
            row(True, "Google Sheet", sheet.url)
            row(not problems, "Sheet is private", "; ".join(problems) or "only you have access")
        except Exception as e:
            row(False, "Google Sheet", str(e)[:160])
    else:
        row(None, "Google Sheet", "not connected, run: igothired connect-google")

    row(scheduler.installed() or None, "Schedule", "installed" if scheduler.installed() else "not installed, run: igothired schedule")
    console.print(table)


# -- wiring -------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="igothired", description="Find founders who are hiring and reach out in your voice.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(name: str, func, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, description=help)
        p.set_defaults(func=func)
        return p

    add("setup", cmd_setup, "Save your API key and check the basics")

    p = add("connect-linkedin", cmd_connect_linkedin, "Sign in to LinkedIn in a browser window")
    p.add_argument("--from-browser", action="store_true", help="reuse the session from Chrome/Brave/Arc instead of signing in")

    p = add("connect-google", cmd_connect_google, "Authorize Google and create your control sheet")
    p.add_argument("client_secret", help="the OAuth client JSON you downloaded from Google Cloud")

    p = add("run", cmd_run, "One full cycle: send approved, read replies, find and draft new founders")
    p.add_argument("--dry-run", action="store_true", help="do everything except send anything on LinkedIn")
    p.add_argument("--now", action="store_true", help="send even outside the configured sending hours")
    p.add_argument("--discover", action="store_true", help="search for new founders even if it already ran today")
    p.add_argument("--no-discover", action="store_true", help="skip searching for new founders")
    p.add_argument("--no-send", action="store_true", help="don't send anything this run")
    p.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)

    add("sync", cmd_sync, "Pull your decisions from the sheet and push the latest state (no LinkedIn)")
    add("status", cmd_status, "Show the pipeline and today's limits")

    p = add("add", cmd_add, "Add a founder by hand")
    p.add_argument("url", help="their LinkedIn profile URL")
    p.add_argument("--why", help="what made you want to reach out")

    p = add("pause", cmd_pause, "Stop all LinkedIn activity")
    p.add_argument("reason", nargs="?")
    add("resume", cmd_resume, "Undo a pause")

    p = add("schedule", cmd_schedule, "Run automatically on weekdays (macOS)")
    p.add_argument("--times", default="10:00,14:00,18:00", help="comma-separated, 24h (default 10:00,14:00,18:00)")
    p.add_argument("--off", action="store_true", help="remove the schedule")

    p = add("doctor", cmd_doctor, "Check that everything is wired up")
    p.add_argument("--offline", action="store_true", help="skip the LinkedIn check")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    load_secrets()
    ensure_home()
    logging.basicConfig(
        filename=LOG_DIR / "igothired.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "anthropic", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        args.func(args)
    except KeyboardInterrupt:
        console.print("\nStopped.")
        sys.exit(130)
    except (LLMError, sheets.SheetError, FileNotFoundError) as e:
        console.print(f"[red]{e}[/]")
        sys.exit(1)
