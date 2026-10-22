# facebook-mcp

[![CI](https://github.com/YoTro/facebook-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/YoTro/facebook-mcp/actions/workflows/ci.yml)

Facebook automation exposed as an MCP (Model Context Protocol) server.

**Capabilities:**

- `get_user_latest_post_time` — a user's latest post publish time (e.g. `exampleuser`)
- `get_user_recent_posts` — recent posts with text, time and permalink
- `search_people` — keyword search for people (`/search/people?q=...`)
- `search_pages` — keyword search for pages (`/search/pages?q=...`)
- `send_message` — send a Messenger message

Design docs: [ARCHITECTURE.en.md](./ARCHITECTURE.en.md) (English) · [ARCHITECTURE.md](./ARCHITECTURE.md) (中文).

## Status

- **M0** scaffolding — done
- **M1** transport (curl_cffi + proxy + retry/throttle) — done
- **M2** session + routes + profile client + `get_user_latest_post_time` — done, verified live
- **M3** search (`search_people` / `search_pages`) — done, verified live over pure HTTP
- **M4** CDP transport (cookie bootstrap + packet capture) — done
- **M5** `send_message` — done, verified live (browser path)

All five tools work against the live site.

### How `send_message` works

Messages travel over `wss://gateway.facebook.com/ws/lightspeed` (JSON tasks in a
16-byte binary frame) — not over HTTP and not via a service worker. Connect with
the account's cookies, send task 46, and the server answers
`replaceOptimsiticMessage` with a real `message_id`.

A `Couldn't send` verdict is ambiguous: the recipient may have messaging turned
off (common for pages), the thread type may need the DOM path, or the thread may
need extra context. The client reports it as-is; `auto` mode then tries the UI
once to tell those apart, since the server has already stated nothing was sent.

```bash
FB_MESSAGE_TRANSPORT=lightspeed   # protocol only (no browser, no fallback)
FB_MESSAGE_TRANSPORT=auto         # protocol first, UI fallback (default)
FB_MESSAGE_TRANSPORT=ui           # legacy CDP-driven DOM path
```

The UI path remains as a fallback (and is the only way to start a conversation),
but sending no longer needs a browser. See `ARCHITECTURE.en.md` §8.3 for the frame
layout and what the gateway's verdict does and does not prove.

## Install

```bash
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Requires **`mcp>=1.2,<2`** (official MCP Python SDK, `mcp.server.fastmcp`).

> ⚠️ This project intentionally pins `mcp<2`.

## Configure

```bash
cp .env.example .env
# fill in FB_COOKIE / FB_COOKIE_FILE for authenticated operations
```

> A proxy is often required to reach Facebook. The transport resolves it
automatically: `FB_PROXY` → `HTTPS_PROXY` env → macOS system proxy.

## Tools

| Tool | Needs login | Description |
|---|---|---|
| `get_user_latest_post_time` | no | Latest post publish time (UTC) for a username |
| `get_user_recent_posts` | no | Recent posts (text, time, permalink) |
| `search_people` | **yes** | Keyword search for people |
| `search_pages` | **yes** | Keyword search for pages |
| `send_message` | **yes** (+ browser) | Send a Messenger message; the result's `delivery` says whether a retry is safe |

## Credentials: `~/.facebook-mcp/`

Cookies live **outside the repository**, one directory per Facebook account:

```
~/.facebook-mcp/                      # 0700
└── accounts/
    └── default/                      # 0700
        ├── cookies.txt               # 0600, Netscape format
        └── account.json              # 0600, metadata + per-account overrides
```

**Which tools need credentials** — measured in an incognito Chrome with zero
Facebook cookies (`ARCHITECTURE.en.md` §8.1):

| Tool | Needs cookies | Evidence when logged out |
|---|---|---|
| `get_user_latest_post_time` / `get_user_recent_posts` | **no** | 200 + full timeline |
| `search_people` / `search_pages` | **yes** | 404 on the search page; GraphQL replies "Rate limit exceeded" |
| `send_message` | no (but needs a browser) | uses Chrome's own session |

### Populate an account

```bash
# A. Export from a logged-in Chrome (one time)
bash tools/launch_chrome_debug.sh
.venv/bin/python tools/export_cookies.py --account default

# B. Or decrypt straight from your Chrome profile
.venv/bin/python tools/import_cookies.py --account default

# Inspect
.venv/bin/python tools/export_cookies.py --list
```

Then run without a browser:

```bash
FB_TRANSPORT_MODE=http .venv/bin/facebook-mcp     # uses accounts/default
```

Or let the server borrow your live Chrome **once**: cookies are bootstrapped on
the first authenticated call and written into the account store, so later runs
are pure HTTP.

```bash
FB_TRANSPORT_MODE=auto .venv/bin/facebook-mcp
```

Search never goes through the browser once cookies exist — the CDP hint is only
used when the session has no cookies at all.

## Multiple accounts

**Per process** (fully isolated, recommended):

```json
{
  "mcpServers": {
    "facebook-default": {
      "command": ".../.venv/bin/facebook-mcp",
      "env": { "FB_ACCOUNT": "default", "FB_TRANSPORT_MODE": "http" }
    },
    "facebook-work": {
      "command": ".../.venv/bin/facebook-mcp",
      "env": { "FB_ACCOUNT": "work", "FB_TRANSPORT_MODE": "http" }
    }
  }
}
```

**Per call** — every tool takes an optional `account` argument:

```python
search_pages(q="pest control", account="work")
send_message(recipient="exampleuser", text="hi", account="work")
```

> Each account can bind its own browser via `cdp_url` in its `account.json`
> (e.g. `{"cdp_url": "http://127.0.0.1:9223"}`), so two accounts never fight
> over the same Chrome tab. The same file holds that account's live GraphQL
> `doc_id`s under `"doc_ids"`, so a Facebook build change is picked up per
> account without editing the repository.

## Maintenance: Facebook ships a new frontend build

GraphQL `doc_id`s and `variables` templates change. Re-capture them:

```bash
.venv/bin/python tools/sniff.py --navigate "https://www.facebook.com/search/pages?q=pest%20control" \
                               --seconds 20 --save-templates
```

`--save-templates` records the fresh `doc_id`s in
`~/.facebook-mcp/accounts/<name>/account.json` and promotes the search
`variables` template into the package when its shape changed.

- **`doc_id` rotation only** → no source change, no commit: the registry value
  in `routes/graphql.py` is just the bootstrap default used before the first
  capture.
- **`variables` shape changed** → the promoted template *is* a tracked file
  (`src/facebook_mcp/routes/templates/search_results.json`), so that case does
  produce a diff and belongs in a commit.

> The override is read when the server starts, so **restart the MCP server** to
> pick up a refreshed `doc_id`.

## Helpers

| Script | Purpose |
|---|---|
| `tools/probe.py` | Ad-hoc live check of a service |
| `tools/sniff.py` | Capture GraphQL `doc_id` / `variables`; `--save-templates` records them in the account store |
| `tools/export_cookies.py` | Export Chrome cookies into `~/.facebook-mcp/accounts/<name>/` |
| `tools/import_cookies.py` | Decrypt cookies straight from the Chrome profile |
| `tools/launch_chrome_debug.sh` | Launch Chrome with DevTools + dedicated profile |
| `tools/check_layers.py` | Verify the layered import rules (CI-friendly) |

## Development

```bash
make install     # .venv (Python 3.11) + dev dependencies
make hooks       # commit-msg hook + commit template
make check       # main local checks: lint, format, layers, docs, tests, smoke
```

| Target | What it does |
|---|---|
| `make lint` | `ruff check src tools tests` |
| `make fmt` | `ruff format` (rewrites) |
| `make fmt-check` | `ruff format --check` |
| `make layers` | verifies the layered import rules + no cycles |
| `make docs` | asserts ARCHITECTURE.md and ARCHITECTURE.en.md stay in sync |
| `make test` | `pytest -q` (344 tests, fully offline) |
| `make smoke` | MCP stdio handshake + `tools/list` validation |
| `make test-commits` | validate commit messages on the current branch |
| `make chrome` / `make chrome-stop` | per-account Chrome: `make chrome ACCOUNT=work` |
| `make check` | lint + fmt-check + layers + docs + test + smoke (CI also runs the commit-message check, the credential guard and the wheel build) |

### CI (`.github/workflows/ci.yml`)

| Job | Runs |
|---|---|
| `lint & architecture` | ruff lint + format, `tools/check_layers.py`, commit-message convention (PRs), secret-leak guard |
| `test` | pytest on Python 3.11/3.12/3.13 (Ubuntu) and 3.11 (macOS) + MCP smoke |
| `build & install wheel` | `python -m build`, asserts `routes/templates/*.json` is packaged, installs the wheel in a clean venv |

Tests are **fully offline** — a local `http.server` and a mock DevTools
(WebSocket) server stand in for Facebook and Chrome, so CI needs no network or
credentials.

## Verify against the live site

```bash
.venv/bin/python tools/probe.py profile exampleuser
.venv/bin/python tools/probe.py search pages "pest control"
```

## Run

```bash
.venv/bin/facebook-mcp            # stdio transport (default)
# or
.venv/bin/python -m facebook_mcp
```

HTTP transports are available too (the SDK supports them):

```bash
# streamable-http on 127.0.0.1:8000/mcp
.venv/bin/python -c "from facebook_mcp.server import mcp; mcp.run('streamable-http')"
```

## MCP client config (example)

```json
{
  "mcpServers": {
    "facebook": {
      "command": "/Users/jin/Documents/GitHub/facebook-mcp/.venv/bin/facebook-mcp",
      "env": { "FB_COOKIE_FILE": "/path/to/cookies.txt" }
    }
  }
}
```


## Commit convention

[Conventional Commits](https://www.conventionalcommits.org/), enforced locally by
`make hooks` and in CI by `tools/commit_msg.py`:

```
feat(search): paginate results with page_info.end_cursor
fix(accounts): keep cookies out of the repository
docs: explain why send_message needs a browser
```

Types: `feat fix docs test refactor perf build ci chore revert`.
Scopes: `core transport session routes clients services server accounts cdp
http proxy profile post search message tools tests ci docs deps release`.
Subject: imperative, lowercase, no trailing period, header ≤ 72 chars.

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full rules and examples.

> Pushing over SSH? Use a repo-scoped deploy key
> (`~/.ssh/github-action-key-*`, `0600`, never committed, one repo per key).
> CI needs none: `actions/checkout` uses the built-in `GITHUB_TOKEN`.

## License

[MIT](./LICENSE) © facebook-mcp contributors

> Not affiliated with, endorsed by, or supported by Meta/Facebook. Use it only
> with accounts you own or are authorized to automate, and respect Facebook's
> terms of service and local anti-spam / privacy law.
>
> All usernames, page names and ids in this repository are placeholders
> (`exampleuser`, `100000000000001`, …). See CONTRIBUTING.md → *Documentation privacy*.
