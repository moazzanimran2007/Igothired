# Igothired

An outreach agent for one person: me. It searches LinkedIn for startup founders who are posting that they need people, reads their profile and post, decides whether I'm a real fit, and writes a connection note and a first message in my voice. I approve, edit or reject each one from a Google Sheet on my phone. When a founder writes back, it reads the whole conversation and drafts a reply that answers everything they asked, using only facts from my profile.

It runs on Claude (Opus 5.5) and drives LinkedIn through [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server), which uses a real browser with my own LinkedIn session.

```
 LinkedIn posts ──► find founders who are hiring ──► read their profile ──► score the fit
                                                                              │
     Google Sheet ◄── draft note + first message (checked for facts and tone) ◄┘
          │
   Approve / Edit / Skip / Redraft
          │
          ▼
 connect with note ──► first message after they accept ──► read replies ──► draft answer ──► back to the sheet
```

## What makes it different from a spam bot

- **It only says true things.** Every fact about me comes from `profile/me.md` (start from [`profile/me.example.md`](profile/me.example.md)). A second Claude pass checks each draft against that file, and a code check flags any number, link or email that isn't in my profile or the founder's own post.
- **It writes like a person.** Drafts follow my voice rules (no em dashes, no flattery, lead with their thing, real numbers) and the structural rules from the `/human` skill (start at the interesting point, don't state the moral, end on a real question). Template phrases like "I came across your profile" are flagged automatically.
- **Few, specific messages.** It contacts founders whose need overlaps with something I've actually built, scored 0 to 100. Below 65 it doesn't bother me.
- **I stay in the loop where it matters.** Anything about a call, start date, pay, hours or visa always comes to me, even in auto mode. So does "is this a bot?".

## Setup

You need macOS, [uv](https://docs.astral.sh/uv/), an [Anthropic API key](https://console.anthropic.com/), a LinkedIn account and a Google account. About 15 minutes.

### 1. Install

```bash
git clone https://github.com/moazzanimran2007/Igothired.git
cd Igothired
uv tool install --editable .
```

That puts an `igothired` command on your PATH. (Or skip it and prefix every command with `uv run`.)

### 2. Save your API key

```bash
igothired setup
```

The key is typed into a hidden prompt and stored in `~/.igothired/secrets.env`, readable only by you. Setup also creates your own `profile/me.md` and `config.yaml` from the examples. Both are gitignored, so your details never end up in the repo.

### 3. Tell it about yourself

- `profile/me.md` is everything the agent knows about you. Keep it accurate. Lines tagged `[verify]` stay hidden from the agent until you confirm them and delete the tag. Only links and the email listed under `## Links` can ever appear in a message.
- `config.yaml`: set your name, `targeting.looking_for` (what you want), and the `discovery.post_queries` it should search for. See [`config.example.yaml`](config.example.yaml) for every option.

### 4. Connect LinkedIn

```bash
igothired connect-linkedin
```

A browser window opens. Sign in like normal, 2FA included. The session is saved by the LinkedIn MCP server in `~/.linkedin-mcp`. Igothired never sees your password. If you're already signed in to LinkedIn in Chrome, Brave or Arc, `igothired connect-linkedin --from-browser` reuses that session instead.

### 5. Connect Google Sheets

Google requires your own OAuth client for this. It's a one-time thing:

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a project (any name, e.g. "Igothired").
2. **APIs & Services → Library**: enable **Google Sheets API** and **Google Drive API**.
3. **Google Auth Platform → Branding**: fill in an app name and your email. **Audience**: choose External, add yourself as a test user, then click **Publish app**. Publishing matters: apps left in "Testing" lose access every 7 days. Igothired only asks for the `drive.file` scope, which doesn't need Google's review.
4. **Clients → Create client → Desktop app**, then download the JSON.

```bash
igothired connect-google ~/Downloads/client_secret_XXXX.json
```

Approve in the browser. If Google shows an "unverified app" screen, that's your own app: click Advanced, then continue. Igothired creates a sheet called **Igothired** and prints its link.

### 6. First run

```bash
igothired doctor           # checks every piece and tells you what's missing
igothired run --dry-run    # does everything except send
igothired run
```

### 7. Let it run on its own

```bash
igothired schedule                       # 10:00, 14:00 and 18:00 on weekdays
igothired schedule --times 9:30,16:00    # or pick your own
igothired schedule --off
```

You get a macOS notification when a founder replies.

## Using the sheet

| Tab | What it's for |
| --- | --- |
| **Queue** | Everything waiting for you. Read the draft, edit the **Connection note** or **Message** cells directly if you want, then set **Decision** to Approve, Skip or Redraft. For Redraft, say what to change in **Feedback for redraft**. Rows marked **Needs you** are replies about calls, dates, pay or visas, or sends LinkedIn couldn't confirm. |
| **Pipeline** | Every founder and where they are: waiting for you, invite sent, messaged, they replied, closed. |
| **Activity** | Every message sent and received, newest first. |
| **Settings** | The **Paused** checkbox is a kill switch: tick it and nothing touches LinkedIn until you untick it. Also shows the mode, the last sync and today's usage. |

Decisions are picked up on the next run. To apply them right away without touching LinkedIn: `igothired sync`.

A cleared Connection note means "connect without a note". A reply with a `[placeholder]` in it can't be approved until you fill it in.

## Review mode and auto mode

Set `autonomy.mode` in `config.yaml`.

- **review** (default): nothing goes out until you approve it.
- **auto**: first messages and replies that pass every check go out on their own. A draft with any warning still waits for you, and so does every reply about calls, logistics, technical questions it can't honestly answer, or anything suspicious. Only the reply types in `autonomy.auto_reply_categories` can ever be sent automatically.

Start in review mode and switch once you trust the drafts.

## How replies work

Each run checks your inbox for new messages from founders you've contacted. When one has replied, it reads the full conversation, then drafts an answer using your profile, its notes on the founder and the whole thread. If they ask for your GitHub, it gives the link. If they want a resume, it offers to email it and includes your address. If they ask for a call, it drafts the reply with a `[Your name: two times that work]` placeholder and flags it for you. If they reply before your first message has gone out, the queued opener is cancelled so they don't get two messages.

## Safety

**Your LinkedIn account**
- LinkedIn's terms don't allow automated tools, and accounts that behave like bots get restricted. Igothired stays well under the usual limits (12 invites a day, 70 a week, 25 messages a day), waits a random 45 to 150 seconds between sends, only sends on weekdays between 9:00 and 19:00, and searches for new founders once a day. All of this is in `config.yaml`. The risk is lower, not zero.
- Any rate limit, restriction or expired session pauses everything. Rate limits back off on their own; a restriction waits for you (`igothired resume`).
- A send that LinkedIn didn't confirm is never retried, because a retry could send the same message twice. It's flagged for you to check instead.

**Prompt injection.** Founders' posts, profiles and messages can contain anything, including instructions aimed at AI tools. That text is always fenced off as data. The model never gets LinkedIn tools: code decides who gets a message, and only people who actually appeared in a search can be added. Links and emails that aren't yours are flagged. Profiles containing instructions for AI tools are flagged and never auto-sent.

**Your secrets.** The API key, Google token and local database live in `~/.igothired` with owner-only permissions. The LinkedIn server runs with a minimal environment, so it never sees your API key. Google access uses the `drive.file` scope, which only covers the one sheet Igothired created, not the rest of your Drive.

**The sheet.** Anyone who can edit it could approve messages from your LinkedIn, and anyone who can see it sees founders' details. So every sync first checks who it's shared with. If it's anyone but you, Igothired won't read from it or write to it until you remove the sharing (read-only viewers can be allowed in `sheet.allowed_viewers`). Everything is written as plain text, so a LinkedIn post that starts with `=IMPORTXML(...)` stays text and can't run as a formula.

**Data you keep.** Founders' names, posts and messages are stored in your local database and your private sheet. Delete `~/.igothired/igothired.db` to wipe it.

## Commands

| Command | |
| --- | --- |
| `igothired setup` | Save your API key, check the basics |
| `igothired connect-linkedin [--from-browser]` | Sign in to LinkedIn |
| `igothired connect-google CLIENT.json` | Authorize Google and create the sheet |
| `igothired run [--dry-run] [--now] [--discover] [--no-discover] [--no-send]` | One full cycle. `--now` ignores sending hours |
| `igothired sync` | Apply sheet decisions and redrafts without touching LinkedIn |
| `igothired status` | Pipeline counts, what needs you, today's limits |
| `igothired add URL [--why TEXT]` | Add a founder by hand |
| `igothired pause [reason]` / `resume` | Stop or restart all LinkedIn activity |
| `igothired schedule [--times ...] [--off]` | Run automatically on weekdays |
| `igothired doctor [--offline]` | Check every connection and permission |

Logs are in `~/.igothired/logs/`.

## Troubleshooting

- **"LinkedIn session expired"**: run `igothired connect-linkedin`.
- **"Press Enter to Send is on"**: in LinkedIn messaging settings, switch to "Click Send to send", then `igothired resume`.
- **Google access expires every week**: your OAuth app is still in Testing. Publish it (setup step 5.3), then run `connect-google` again.
- **"The sheet is shared beyond you"**: open Share in the sheet and remove everyone else.
- **Free account note limit**: LinkedIn gives free accounts a few personalized invites a month. When they run out, invites go without a note and the first message does the work (`messaging.on_note_quota`).

## Development

```bash
uv sync
uv run pytest
```

The tests run the full loop (discover, draft, approve, send, reply, pause) against a fake LinkedIn MCP server over real stdio, with Claude stubbed out. Nothing in the test suite touches LinkedIn, Google or the Claude API.

```
src/igothired/
  cli.py        commands
  pipeline.py   the loop: discover, research, draft, send, replies
  prompts.py    everything the model is told, and the answer formats
  linkedin.py   MCP client, pacing, error handling
  llm.py        Claude calls with structured output and prompt caching
  sheets.py     Google Sheet setup, sharing check, sync
  safety.py     message checks, untrusted-text fencing
  db.py         SQLite store (source of truth)
  profile.py    loads profile/me.md
  scheduler.py  launchd schedule
```
