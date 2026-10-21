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

### Why `send_message` needs a browser

Messenger's web app issues the send from its **service worker**, and typing/sync
traffic runs over a binary Lightspeed WebSocket whose frames contain no readable
message body. There is no page-level GraphQL mutation to replay, and the legacy
`/ajax/mercury/send_messages.php` endpoint is gone (404). So `send_message` drives
the real Messenger UI through CDP (type → click send → confirm the composer clears).
See `ARCHITECTURE.en.md` §8.3 for the evidence.

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
| `send_message` | **yes** (+ browser) | Send a Messenger message |

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

Or let the server borrow your live Chrome (cookies bootstrapped lazily):

```bash
FB_TRANSPORT_MODE=auto .venv/bin/facebook-mcp
```

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
`~/.facebook-mcp/accounts/<name>/account.json` (read at request time by
`BaseClient._doc_id()`) and promotes the search `variables` template into the
package when its shape changed. A new Facebook build therefore needs **no
source change and no commit** — the `doc_id` in `routes/graphql.py` is only the
bootstrap default used before the first capture.

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
make check       # everything CI runs: lint, format, layers, tests, MCP smoke
```

| Target | What it does |
|---|---|
| `make lint` | `ruff check src tools tests` |
| `make fmt` | `ruff format` (rewrites) |
| `make fmt-check` | `ruff format --check` |
| `make layers` | verifies the layered import rules + no cycles |
| `make test` | `pytest -q` (194 tests, fully offline) |
| `make smoke` | MCP stdio handshake + `tools/list` validation |
| `make test-commits` | validate commit messages on the current branch |
| `make chrome` / `make chrome-stop` | per-account Chrome: `make chrome ACCOUNT=work` |
| `make check` | all of the above |

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

## License

[MIT](./LICENSE) © facebook-mcp contributors

> Not affiliated with, endorsed by, or supported by Meta/Facebook. Use it only
> with accounts you own or are authorized to automate, and respect Facebook's
> terms of service and local anti-spam / privacy law.
>
> All usernames, page names and ids in this repository are placeholders
> (`exampleuser`, `100000000000001`, …). See CONTRIBUTING.md → *Documentation privacy*.
