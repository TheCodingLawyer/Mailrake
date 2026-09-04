# mailrake

Unsubscribe from mailing lists in bulk and find out where your Gmail storage
actually went — from a browser UI or the terminal.

**Everything runs on your machine.** There is no server, no account to create,
and no third party that ever sees your email. Your Google token is stored in
your own OS config directory and used only by the process you started.

```bash
uvx mailrake --ui
```

---

## Why local-first

Every hosted competitor in this space (Unroll.me, Clean Email, Leave Me Alone)
holds your Gmail token on their servers. Google requires that: any app touching
restricted Gmail data ["from or through a third-party server"][casa] must pass a
third-party security assessment, annually.

This tool sidesteps that entirely by never having a server. That is a privacy
property first and a cost property second — but it means the thing you install
is the thing that runs, and you can read all of it.

[casa]: https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification

## What it does

**Unsubscribe** — finds every sender using the `List-Unsubscribe` header,
groups them, and unsubscribes properly: a one-click POST per [RFC 8058][rfc]
where the sender supports it, `mailto` otherwise. Falls back through every
available method before giving up, then offers a Gmail auto-trash filter for
senders that simply ignore unsubscribe requests.

**Storage** — Gmail tells you that you have used 14.2 of 15 GB and almost
nothing about why. This shows the breakdown: biggest senders by megabyte,
biggest individual messages, and how much of it belongs to senders you could
unsubscribe from right now.

**Safety** — senders that look financial, governmental, medical or
security-related are flagged and skipped unless you override them one at a
time. Nothing is ever deleted permanently: the OAuth scope used here
*cannot* bypass the trash, so everything is recoverable for 30 days.

[rfc]: https://www.rfc-editor.org/rfc/rfc8058

## Setup

You need your own Google OAuth credentials. This is a one-time, ten-minute
step, and it is the reason the tool needs no verification, no audit, and no
subscription: you are the developer of your own client.

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create a project
2. **APIs & Services → Library** → search "Gmail API" → **Enable**
3. **APIs & Services → OAuth consent screen** → User type **External** → add
   your own Gmail address under **Test users**
4. **Credentials → Create credentials → OAuth client ID** → application type
   **Desktop app** → **Download JSON**
5. Save that file as `credentials.json` in the config directory:

```bash
mailrake --where
```

Then run it. Your browser opens Google's consent screen once; after that a
refresh token is cached locally.

```bash
uvx mailrake --ui
```

### Running from source

```bash
pip install .
npm --prefix web install && npm --prefix web run build
mailrake --ui
```

For development, `pip install -e .` plus `python -m mailrake --ui` from the
project root. Note that on some setups an editable install's `.pth` file is
silently ignored, which leaves the `mailrake` console script unable to import
the package — running via `python -m mailrake` from the project root always
works, because the current directory is on the path.

## Usage

The browser UI and the terminal share one engine, so they behave identically.

```
mailrake [options]

  --ui               Open the browser control panel
  --storage          Show the storage breakdown (uses cache, no network)
  --days N           Scan window in days (default 90; 0 = all time)
  --all-time         Same as --days 0
  --all-mail         Scan every category, not just Promotions/Updates/Forums
  --max N            Max senders to process (default 200)
  --max-emails N     Max emails to fetch (default 2000)
  --dry-run          Preview everything, change nothing
  --no-trash         Unsubscribe only; leave existing mail alone
  --no-filters       Drop the gmail.settings.basic scope entirely
  --cleanup          Trash remaining mail from senders you already left
  --retry-failed     Retry failures with alternative methods
  --block-remaining  Auto-trash filters for senders that keep ignoring you
  --fresh            Ignore the local cache and rescan
  --where            Print the config directory
```

Keys in both UIs: `j`/`k` move, `space` selects, `a` selects all,
`u` unsubscribes, `esc` clears.

**Start with `--dry-run`.** It prints exactly what would happen and touches
nothing.

## Scanning, and why it takes the time it does

Gmail meters API use against a per-minute quota. When you exhaust it, it
returns `403` — not `429`, and with no `Retry-After` header to tell you how
long to wait. Pushing harder does not help; it just converts requests into
errors. Measured against a real mailbox, 25 seconds per probe:

| requests/sec | messages lost to 403 |
|---|---|
| 5 | 0.0% |
| 10 | 0.0% |
| 15 | 9.3% |
| 25 | 20.1% |

That table measures *bursts*, though, and the quota is a per-minute budget —
so a rate that passes a 25-second probe can still exhaust the bucket over a
longer run. A sustained 800-message scan settles nearer 5 messages/second than
10. This is exactly why the scanner adapts rather than hardcoding a number:
the safe rate depends on your quota, your mailbox and how long you have been
running.

**The point of this is completeness, not speed.** Throughput is capped by
Gmail either way. What changed is that failed requests are retried, stragglers
get a final sweep once the quota refills, and anything still unreadable is
reported to you rather than quietly skipped — a dropped message means a
missing sender and a wrong storage total.

Measured on a real mailbox, 800 messages: **800 fetched, 0 dropped, 156s.**
For comparison, an unpaced build firing 15 requests at once looked faster and
silently lost 18%.

Three things keep the work down:

- The default scan targets the Promotions, Updates and Forums categories,
  where bulk mail actually lives, instead of walking your whole mailbox.
  Use `--all-mail` to widen it.
- Results are cached locally, so later scans fetch only what is new.
- **Scans are resumable.** Results are written to the cache as they arrive,
  not at the end. Press Ctrl+C, close the terminal, lose your connection —
  whatever was fetched is kept, and running again picks up where it stopped.

A first scan over a large mailbox genuinely can take half an hour. `mailrake`
tells you the estimate before it starts and shows a running ETA, so narrow it
with `--days 90` if you would rather not wait.

## Where your data lives

One directory, which `--where` will print:

| Platform | Location |
|---|---|
| macOS | `~/Library/Application Support/mailrake/` |
| Linux | `~/.config/mailrake/` |
| Windows | `%APPDATA%\mailrake\` |

It holds `credentials.json` (your OAuth client), `token.json` (your refresh
token) and `state.db` (SQLite: scanned senders, storage figures, and an
append-only ledger of every action taken). Deleting that directory removes
every trace of the tool.

To revoke access entirely, remove it at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions) —
deleting the local token stops this tool, but only Google can revoke the grant.

### About the local server

`--ui` starts a server bound to `127.0.0.1` on a random port, protected by a
per-session token generated at launch and passed in the URL it opens. Every
request is checked for that token and rejected unless its `Origin` and `Host`
are loopback, which blocks both stray cross-origin requests and DNS rebinding.
The server exists only while the command is running.

## Permissions

| Scope | Why | Optional? |
|---|---|---|
| `gmail.modify` | Read headers, trash mail, send `mailto` unsubscribes. **Cannot delete permanently.** | Required |
| `gmail.settings.basic` | Create auto-trash filters for senders that ignore unsubscribes | Yes — `--no-filters` |

## Development

```bash
pip install -e ".[dev]"
pytest                              # engine, storage, pacer, server hardening
npm --prefix web run dev            # frontend with hot reload
```

## Disclaimer

This sends real unsubscribe requests and moves real email to Trash. Trash is
recoverable for 30 days. Use `--dry-run` first.

## License

MIT
