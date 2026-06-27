# Regulatory Document Ingestion Agent

An email-driven agent that turns a request like *"can you give me Other Documents
from M12205?"* into a ZIP of the relevant regulatory documents plus a summary
reply — fully automated.

Built for the Senpilot Software Engineering Intern challenge. It powers the data
ingestion side of a Regulatory Agent: collecting filings from the Nova Scotia
UARB Public Documents Database so they can be searched and analyzed.

## What it does

A user emails the agent a **matter number** (e.g. `M12205`) and a **document
type** (`Exhibits`, `Key Documents`, `Other Documents`, `Transcripts`, or
`Recordings`). The agent then:

1. **Reads** unread mail over IMAP and regex-parses the matter number + type.
2. **Scrapes** the [UARB database](https://uarb.novascotia.ca/fmi/webd/UARB15)
   with Playwright: opens the matter, scrapes the metadata and per-tab document
   counts, and downloads up to 10 files of the requested type ("GO GET IT").
3. **Zips** the downloads into `attachments.zip`.
4. **Summarizes** the matter with a lightweight LLM via OpenRouter.
5. **Replies** to the sender over SMTP with the summary + ZIP, then marks the
   original email as read.

```
Email request ─► IMAP fetch ─► parse (matter + type)
                                     │
                                     ▼
                  Playwright: open matter ─► scrape metadata + counts
                                     │                 │
                                     ▼                 ▼
                       download ≤10 of type ─► attachments.zip
                                     │
                                     ▼
                   OpenRouter LLM drafts the summary email
                                     │
                                     ▼
              SMTP reply (+ZIP) ─► mark original email read
```

## Talking to the agent

Give people a **dedicated address**: a Gmail **`+alias`** of your account, e.g.
`youraccount+senpilot@gmail.com`. Mail to it lands in your normal inbox with no
extra setup. Set it as `AGENT_ADDRESS` in `.env`.

> ### How regular email is filtered out
> When `AGENT_ADDRESS` is set, the agent only treats an email as a request if it
> was **addressed to that alias** *and* contains a valid matter number and
> document type. Your everyday email (newsletters, personal mail, etc.) goes to
> your bare address, never the alias, so it is ignored. This was verified: an
> email to the bare address containing `M11111`/`Exhibits` was skipped, while a
> request to the `+senpilot` alias was processed.

Example request email:

> **To:** youraccount+senpilot@gmail.com
> **Subject:** Document request
> **Body:** Hi Agent, can you give me Other Documents files from M12205? Thanks!

## Setup

Requires **Python 3.11** (the version Playwright's browsers are installed for).

```bash
pip install -r requirements.txt
playwright install chromium          # one-time browser download
cp .env.example .env                 # then edit .env (see below)
```

### `.env` configuration

| Variable | What it is | Default |
| --- | --- | --- |
| `EMAIL_ADDRESS` | Gmail address the agent reads from / replies with | – (required) |
| `EMAIL_APP_PASSWORD` | A Gmail **App Password** (16 chars), not your login password | – (required) |
| `AGENT_ADDRESS` | The `+alias` people email to reach the agent (filters your other mail) | – (recommended) |
| `IMAP_HOST` / `IMAP_PORT` | IMAP server for reading | `imap.gmail.com` / `993` |
| `SMTP_HOST` / `SMTP_PORT` | SMTP server (auto: SSL on 465, STARTTLS otherwise) | `smtp.gmail.com` / `587` |
| `MAILBOX` | Folder to scan | `INBOX` |
| `MAX_EMAILS_TO_SCAN` | Max recent unread emails to inspect per cycle | `25` |
| `POLL_INTERVAL_SECONDS` | How often `--loop` re-checks the inbox | `60` |
| `OPENROUTER_API_KEY` | OpenRouter API key | – (required) |
| `OPENROUTER_BASE_URL` | OpenRouter base URL | `https://openrouter.ai/api/v1` |
| `OPENROUTER_MODEL` | Any lightweight chat model on OpenRouter | `openai/gpt-4o-mini` |
| `HEADLESS` | `false` to watch the browser run | `true` |

**Gmail App Password:** enable 2-Step Verification, then create one at
<https://myaccount.google.com/apppasswords>.
**OpenRouter key:** <https://openrouter.ai/keys>.

## Running

**One pass** — process all pending requests once, then exit:

```bash
python agent.py
```

**Continuous** — keep checking the inbox on an interval (use this for a live
demo or always-on operation):

```bash
python agent.py --loop                 # uses POLL_INTERVAL_SECONDS
python agent.py --loop --interval 30   # check every 30s
```

Stop with `Ctrl+C`.

### Example reply

> M12205 is about the Halifax Regional Water Commission - Windsor Street Exchange
> Redevelopment Project $69,275,000. It relates to Capital Expenditure Approvals
> within the Water category. The matter had an initial filing on April 7, 2025
> and a final filing on October 23, 2025. I found 13 Exhibits, 5 Key Documents,
> 42 Other Documents, and 0 Transcripts or Recordings. I downloaded 10 out of the
> 42 Other Documents and am attaching them as a ZIP here.

## Hosting / deployment

You **don't need a public server**. The agent *pulls* requests via IMAP, so
anyone can email the alias and the agent will pick it up the next time it polls.
Options, simplest first:

- **Your laptop (demo):** just run `python agent.py --loop`. Great for the demo
  window — leave it running and email the alias.
- **Always-on background service (macOS/Linux):** run it under a process manager
  so it restarts automatically:
  - **launchd / systemd:** wrap `python agent.py --loop` in a service unit.
  - **tmux/screen:** `tmux new -s agent 'python agent.py --loop'`.
- **Free/cheap cloud VM:** Fly.io, Railway, Render (background worker), or any
  small VPS. Install Python 3.11 + `playwright install --with-deps chromium`,
  set the env vars, and run `python agent.py --loop`. No inbound ports needed.

(A `cron` job running `python agent.py` every minute also works if you prefer not
to keep a long-lived process.)

## Design notes

- **Deterministic parsing.** Matter number (`M\d{5}`) and document type are
  extracted by regex from the email subject + body.
- **Resilient scraping.** The UARB site is a FileMaker WebDirect (Vaadin) app
  with auto-generated DOM ids, so metadata is read by element *geometry* (column/
  row position) and tab counts come from the tab labels (`Other Documents - 42`).
  Every UI interaction is wrapped in error handling so a missing element degrades
  gracefully instead of crashing. WebDirect commits typed values asynchronously,
  so the agent waits for the value to sync and verifies the detail page loaded.
- **LLM with a safety net.** If the OpenRouter call fails, the agent builds the
  identical fixed-structure summary deterministically in Python.
- **Safe email handling.** Mail is fetched with `BODY.PEEK` so it is *not*
  implicitly marked read; the `\Seen` flag is only set after the reply is sent.
  The agent scans newest-first and caps how many emails it inspects, so it stays
  fast even on an inbox with tens of thousands of unread messages.

## Files

| File | Purpose |
| --- | --- |
| `agent.py` | The complete agent (all five stages + CLI / loop). |
| `requirements.txt` | Python dependencies. |
| `.env.example` | Template for credentials/config. |
| `.gitignore` | Keeps `.env` and runtime artifacts out of git. |
| `README.md` | This file. |

> **Security:** `.env` holds your credentials and is gitignored — never commit it.
