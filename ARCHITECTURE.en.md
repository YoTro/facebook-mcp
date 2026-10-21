# facebook-mcp — Architecture

**Language**: [中文](./ARCHITECTURE.md) · [English](./ARCHITECTURE.en.md)

> Goal: expose "read Facebook data / send messages / keyword search" as an MCP
> server that an LLM agent can call.
> Design principles: **high cohesion, low coupling, easy to extend, one-way
> dependencies**.
>
> This document describes the system **as it is implemented**, not the initial
> plan. Every technical conclusion comes from measurements against the live
> site; the evidence is in §8.

---

## 1. Scope

| Capability | MCP tool | Example | Login required | Status |
|---|---|---|---|---|
| Latest post time | `get_user_latest_post_time` | `https://www.facebook.com/exampleuser` | no (public) | ✅ |
| Recent posts | `get_user_recent_posts` | same | no (public) | ✅ |
| Keyword search: people | `search_people` | `/search/people?q=pest%20control` | **yes** | ✅ |
| Keyword search: pages | `search_pages` | `/search/pages?q=pest%20control` | **yes** | ✅ |
| Send a message (Messenger) | `send_message` | DM to a user/page | **yes + browser** | ✅ |

> **Authorization**: this project is meant for automating accounts you own or are
> authorized to operate. Sending messages and searching must comply with
> Facebook's platform terms and applicable law (anti-spam, privacy).

All usernames, page names and ids in this repository are placeholders
(`exampleuser`, `100000000000001`, …). See CONTRIBUTING.md → *Documentation privacy*.

---

## 2. Technology choices

| Concern | Choice | Why |
|---|---|---|
| MCP framework | **Official `mcp` SDK (`>=1.2,<2`)** → `mcp.server.fastmcp.FastMCP` | Maintained upstream, decorator-based tools, typed schemas, stdio / SSE / streamable-http built in |
| HTTP transport | **curl_cffi** | Browser TLS/JA3 impersonation, pure Python |
| Browser channel | **Chrome DevTools Protocol** (direct `websocket-client`) | Reuses the cookies and execution context of a logged-in Chrome; **no Playwright/Selenium** |
| Request capture | **CDP `Network` domain** (`tools/sniff.py`) | No extra dependency; reads the real `doc_id` and `variables` |
| Data models | **pydantic v2** + **pydantic-settings** | Structured models, validated configuration |
| HTML parsing | **beautifulsoup4 + lxml** | Extracting embedded ServerJS JSON |
| Runtime | **Python 3.11+** | `mcp<2` needs ≥3.10; the system `python3` here is 3.8 and unusable |

> ⚠️ Environment: create a dedicated environment with
> `/opt/homebrew/bin/python3.11 -m venv .venv`.
>
> Note: `mcp-wireshark` and `camoufox-reverse-mcp` are **not used**. Packet
> capture is done through CDP's `Network`/`Fetch` domains, and no signature-class
> anti-bot protection was encountered (`jazoest` can be derived, see §8).

---

## 3. Layered architecture (one-way dependencies)

```
┌──────────────────────────────────────────────────────────────────┐
│ L5  server.py          MCP interface (5 tools)                    │
│     get_user_latest_post_time / get_user_recent_posts /           │
│     search_people / search_pages / send_message                   │
└───────────────────────────────┬──────────────────────────────────┘
                                ↓ depends on core + services only
┌──────────────────────────────────────────────────────────────────┐
│ L4  services/          Use-case orchestration                     │
│     post_service / search_service / message_service               │
│     factory.py — composition root (the only place that wires      │
│                  transport / session / clients)                   │
└───────────────────────────────┬──────────────────────────────────┘
                                ↓ depends on core + clients only
┌──────────────────────────────────────────────────────────────────┐
│ L3  clients/           Per-domain API clients                     │
│     base / profile_client / search_client / message_client        │
└───────────────┬──────────────────────────────┬───────────────────┘
                ↓                              ↓
   ┌────────────────────────────┐  ┌──────────────────────────────────┐
   │ L2  session/               │  │ L2  routes/  (pure data, no deps) │
   │   state / extractor /      │  │   base / rest / graphql           │
   │   manager                  │  │   templates/  ← variables         │
   └─────────────┬──────────────┘  └──────────────────────────────────┘
                 ↓
┌──────────────────────────────────────────────────────────────────┐
│ L1  transport/         Transports (abstraction + implementations)  │
│     base(protocol) / http(curl_cffi) / cdp(DevTools) /             │
│     factory(selection) / proxy(resolution) / browsing(capability)  │
└───────────────────────────────┬──────────────────────────────────┘
                                ↓ depends on core only
┌──────────────────────────────────────────────────────────────────┐
│ L0  core/              Infrastructure (no internal dependencies)  │
│     config / exceptions / accounts / logger / extract / models    │
└──────────────────────────────────────────────────────────────────┘
```

### Dependency rules (as enforced)

1. Dependencies point **downward only**; a lower layer may never import an upper
   one. Verified by a script (exit code 0 means pass):

   ```bash
   .venv/bin/python tools/check_layers.py
   # upward deps: 0
   # import cycles: 0
   ```

2. `core` may **only import its own submodules**; it must not import
   `transport` or anything above it.

3. **Within a layer, one-way references and composition roots are allowed;
   cycles are not**:
   - allowed: `clients/message_client.py` → `clients/profile_client.py`
     (to resolve usernames)
   - allowed: `services/factory.py` assembling all services; **not** allowed:
     `post_service` ↔ `search_service`
   - allowed: `transport/factory.py` importing the concrete `http` / `cdp`

4. **Domain data** crossing a layer boundary uses the pydantic models in
   `core/models`. **Raw protocol payloads** (GraphQL JSON, HTML) are handled as
   `dict` inside a client and are not force-modelled.

---

## 4. Directory layout (actual)

```
facebook-mcp/
├── ARCHITECTURE.md                  # this document (Chinese)
├── ARCHITECTURE.en.md               # this document (English)
├── README.md
├── CONTRIBUTING.md                  # commit rules / doc privacy / PR rules
├── CHANGELOG.md                     # Keep a Changelog
├── LICENSE                          # MIT
├── Makefile                         # make check / hooks / chrome …
├── .gitmessage                      # commit template
├── pyproject.toml                   # deps + entry point facebook-mcp = facebook_mcp.server:main
├── .env.example                     # every FB_* setting
├── .github/workflows/ci.yml         # CI: lint / multi-version tests / packaging
├── src/facebook_mcp/
│   ├── __init__.py                  # __version__ = "0.1.0"
│   ├── __main__.py                  # python -m facebook_mcp
│   ├── server.py                    # L5 MCP tool registration
│   │
│   ├── core/                        # L0 infrastructure
│   │   ├── config.py                #   Settings (FB_* environment variables)
│   │   ├── exceptions.py            #   exception hierarchy (§7)
│   │   ├── accounts.py              #   credential layout (~/.facebook-mcp/accounts/*)
│   │   ├── logger.py                #   logging (stderr, keeps stdio clean)
│   │   ├── extract.py               #   generic embedded-JSON helpers (no FB knowledge)
│   │   └── models/
│   │       ├── profile.py           #   Profile / Timeline
│   │       ├── post.py              #   Post / PostTimeResult
│   │       ├── search.py            #   SearchCategory / SearchResult
│   │       └── message.py           #   Recipient / SendResult
│   │
│   ├── transport/                   # L1 transports
│   │   ├── base.py                  #   Transport protocol + HttpRequest/HttpResponse
│   │   ├── http.py                  #   CurlCffiTransport
│   │   ├── cdp.py                   #   CdpTransport (WebSocket → DevTools)
│   │   ├── proxy.py                 #   explicit → env → macOS system proxy
│   │   ├── browsing.py              #   cdp_from(): does this transport drive a browser?
│   │   └── factory.py               #   get_transport() + HybridTransport
│   │
│   ├── session/                     # L2 session layer
│   │   ├── state.py                 #   SessionState (cookies + fb_dtsg + lsd)
│   │   ├── extractor.py             #   cookie string / Netscape file / page tokens
│   │   └── manager.py               #   SessionManager: load / require_auth / rotate / bootstrap
│   │
│   ├── routes/                      # L2 route registry (pure data)
│   │   ├── base.py                  #   Route dataclass + FACEBOOK_BASE / GRAPHQL_PATH
│   │   ├── rest.py                  #   page entry points (profile / search URLs)
│   │   ├── graphql.py               #   doc_id + fb_api_req_friendly_name registry
│   │   └── templates/
│   │       ├── __init__.py          #   build_search_variables(): template → variables
│   │       └── search_results.json  #   captured search variables template (42 fields)
│   │
│   ├── clients/                     # L3 clients
│   │   ├── base.py                  #   BaseClient (requests, error mapping, GraphQL form)
│   │   ├── profile_client.py        #   profile HTML → Profile + Timeline
│   │   ├── search_client.py         #   GraphQL search → SearchResult
│   │   └── message_client.py        #   CDP-driven Messenger UI send
│   │
│   └── services/                    # L4 services
│       ├── factory.py               #   Services composition root + get_services()
│       ├── post_service.py          #   get_latest_post_time / get_recent_posts
│       ├── search_service.py        #   search_people / search_pages
│       └── message_service.py       #   send_message (exceptions → structured result)
│
├── tools/                           # reverse-engineering / ops helpers (not runtime)
│   ├── probe.py                     # single-service probe for manual verification
│   ├── sniff.py                     # CDP capture → doc_id / variables (--save-templates)
│   ├── export_cookies.py            # CDP → browser cookies → Netscape file
│   ├── import_cookies.py            # decrypt a local Chrome profile's Cookies
│   ├── check_layers.py              # verify the layering + detect import cycles
│   ├── smoke_mcp.py                 # MCP stdio handshake + tools/list smoke test
│   ├── commit_msg.py                # Conventional Commits validator (git hook + CI)
│   └── launch_chrome_debug.sh       # per-account Chrome with the DevTools port
│
└── tests/                           # 177 tests (all offline)
    ├── conftest.py                  # points FB_HOME at a temp dir, isolating real credentials
    ├── fixtures/                    # profile_page.html / search_response.json
    ├── test_skeleton.py             # tool registration and schemas
    ├── test_transport.py            # local HTTP server: retry / throttle / error mapping
    ├── test_accounts.py             # account store: names, permissions, resolution, multi-account
    ├── test_m2_profile.py           # extraction, session, routes, profile parsing
    ├── test_m3_search.py            # variables template, result parsing, jazoest
    ├── test_m4_cdp.py               # CDP protocol, hybrid routing, cookie bootstrap
    ├── test_m4_cdp_integration.py   # real mock DevTools server (HTTP + WebSocket)
    ├── test_m5_message.py           # send flow, fallback chain, recipient resolution
    ├── test_logged_out_matrix.py    # regression tests for the incognito baseline
    └── test_commit_msg.py           # the commit-message validator itself
```

---

## 5. Module responsibilities

### 5.1 L0 core

- `config.py` — `Settings` (pydantic-settings, environment prefix **`FB_`**):

  | Field | Env var | Default |
  |---|---|---|
  | `transport_mode` | `FB_TRANSPORT_MODE` | `auto` |
  | `impersonate` | `FB_IMPERSONATE` | `chrome` |
  | `request_timeout` | `FB_REQUEST_TIMEOUT` | `30.0` |
  | `max_retries` | `FB_MAX_RETRIES` | `3` |
  | `min_request_interval` | `FB_MIN_REQUEST_INTERVAL` | `2.0` |
  | `account` | `FB_ACCOUNT` | `default` |
  | `home` | `FB_HOME` | `~/.facebook-mcp` |
  | `cookie` | `FB_COOKIE` | – |
  | `cookie_file` | `FB_COOKIE_FILE` | – |
  | `user_agent` | `FB_USER_AGENT` | – |
  | `proxy` | `FB_PROXY` | – |
  | `proxy_from_system` | `FB_PROXY_FROM_SYSTEM` | `true` |
  | `cdp_url` | `FB_CDP_URL` | `http://127.0.0.1:9222` |
  | `cdp_page_url` | `FB_CDP_PAGE_URL` | `https://www.facebook.com/` |
  | `cdp_timeout` | `FB_CDP_TIMEOUT` | `60.0` |
  | `cdp_cookie_bootstrap` | `FB_CDP_COOKIE_BOOTSTRAP` | `true` |

- `exceptions.py` — exception hierarchy (§7).
- `accounts.py` — credential layout (§5.8): `AccountPaths`, name validation,
  `0600`/`0700` permissions, account listing.
- `logger.py` — logs always go to **stderr** (stdout is reserved for MCP JSON-RPC).
- `extract.py` — pure helpers: `iter_json_scripts` / `walk` / `find_first` /
  `find_all` / `dig`. Deliberately contains **no Facebook domain knowledge**;
  domain parsing lives in the client layer.
- `models/` — domain models, pure data.

### 5.2 L1 transport

`base.py` defines the protocol and data structures:

```python
@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] | None = None          # query string
    data: str | dict[str, str] | None = None      # form body
    json: Any | None = None                       # JSON body
    impersonate: str | None = None                # per-request TLS fingerprint override
    transport_hint: Literal["http", "cdp"] | None = None   # hybrid routing hint

@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    url: str
    headers: dict[str, str]
    text: str
    content: bytes
    # .ok / .json() / .raise_for_status()

class Transport(Protocol):
    name: str
    def request(self, req: HttpRequest) -> HttpResponse: ...
    def close(self) -> None: ...
```

- `http.py` — `CurlCffiTransport`: fixed impersonation, exponential-backoff retry
  (connection errors plus `429/500/502/503/504`, honouring `Retry-After`),
  minimum interval between requests, **loopback addresses bypass the proxy**.
- `cdp.py` — `CdpTransport`:
  - discovers pages via `GET /json/version` and `/json/list`; creates one with
    `PUT /json/new` when no usable page exists
  - performs the WebSocket handshake with `suppress_origin=True` (Chrome answers
    `Origin`-bearing handshakes with 403)
  - `request()` executes `fetch()` in the page's main world, so the browser
    supplies its own cookies / UA / TLS fingerprint; headers the browser refuses
    to let scripts set (`Cookie`/`User-Agent`/`Sec-*`/`Host`, …) are stripped
  - `cookies()` / `evaluate()` / `navigate()` / `send()` / `drain_events()`
  - **event buffering**: CDP events received while waiting for a command reply
    are kept instead of dropped (required for packet capture)
- `proxy.py` — three-tier resolution: explicit `FB_PROXY` → `HTTPS_PROXY`-style
  environment variables → macOS `scutil --proxy`.
- `browsing.py` — `cdp_from(transport)`: structurally decides whether a
  transport can drive a browser (a `CdpTransport`, or the `.cdp` half of a
  `HybridTransport`).
- `factory.py` — `get_transport()` and `HybridTransport` (routes by
  `transport_hint`).

> **Implicit contract**: `_DEFAULT_HEADERS` in `base.py` always sends
> `Accept: text/html,application/xhtml+xml,…`. Without it, Facebook serves a
> "reduced" profile page that has no `timeline_list_feed_units`, so the scraper
> silently gets zero posts. `tests/test_logged_out_matrix.py` locks this down.

### 5.3 L2 session

- `state.py` — `SessionState`: a `cookies` dict plus `fb_dtsg`, `lsd` and
  `user_agent`; exposes `is_authenticated` (requires `c_user` + `xs`),
  `cookie_header()`, `as_headers()`, `merge_tokens()`.
- `extractor.py` — `from_cookie_string()` (Netscape header string),
  `from_cookie_file()` (Netscape `cookies.txt`), `tokens_from_html()`
  (`DTSGInitialData` / `LSD`), `user_id_from_html()`.
- `manager.py` — `SessionManager`:
  - load priority: `FB_COOKIE_FILE` → `~/.facebook-mcp/accounts/<account>/cookies.txt`
    → `FB_COOKIE` → anonymous (see §5.8)
  - `require_auth()`: when logged out it **tries** to bootstrap cookies through
    `cookie_provider`, and only then raises `AuthError`
  - `learn_from_html()`: refreshes `lsd` / `fb_dtsg` from a page
  - `from_config`: whether cookies came from configuration or the account store
    rather than the browser; this drives the protocol-first transport choice (§9.3)

> Cookies are bootstrapped through
> `SessionManager(cookie_provider=cdp.cookies)`, injected by
> `services/factory.py`. `extractor.py` itself never touches CDP.

### 5.4 L2 routes (where reverse-engineering output lands)

This layer is **pure data plus data transformation** and depends on no other
layer (`base.py` only defines dataclasses).

- `base.py` — `Route(op, method, path, doc_id, friendly_name, requires_auth)`.
- `rest.py` — page entry points: `PROFILE_PAGE` (`/{username}`) is used by
  `profile_client`; `SEARCH_PEOPLE_PAGE` / `SEARCH_PAGES_PAGE` are **records
  only** (search itself goes through GraphQL and no production code calls them);
  they are kept for navigation and diagnostics.
- `graphql.py` — the `doc_id` registry:

  | Route constant | `doc_id` | `friendly_name` | Status |
  |---|---|---|---|
  | `PROFILE_HEADER` | `28585259111117102` | `ProfileCometHeaderQuery` | captured, unused |
  | `PROFILE_TIMELINE_REFETCH` | – | `ProfileCometTimelineFeedRefetchQuery` | not captured (not needed) |
  | `SEARCH_RESULTS` | `39411546341825607` | `SearchCometResultsPaginatedResultsQuery` | ✅ in use |
  | `SEND_MESSAGE` | – | `SendMessageMutation` | does not exist at page level (see §10 M5) |

- The `doc_id` values above are only **bootstrap defaults**:
  `BaseClient._doc_id()` prefers the `doc_ids` map in `account.json`
  (`{"search.results": "..."}`). `doc_id` rotates with every frontend build and
  can differ per account, so the live value lives in the credential store —
  refreshing it needs no source change and produces no commit.

- `templates/` — the search `variables` template. It has 42 fields (27 of them
  Relay feature gates) and is managed **as data**; `build_search_variables()`
  only replaces `args.text` / `args.experience.type` / `args.context.bsid` /
  `count` / `cursor`.

### 5.5 L3 clients

- `base.py` — `BaseClient(transport, session, settings)`:
  - `_request()` / `_get()`: merge default + session headers; **decides
    `transport_hint`**
  - `_graphql(route, variables)`: builds the Relay form with `fb_dtsg` / `lsd` /
    **`jazoest` (derived from `fb_dtsg`)**
  - `_ensure_dtsg()`: fetches the home page to fill in missing tokens
  - `_raise_for_facebook_status()` / `_raise_for_graphql_errors()`: HTTP status
    and Relay `errors` → project exceptions
- `profile_client.py` — **a single GET of the profile HTML** does two things:
  - `_parse_profile()`: anchors on `userVanity` to find `userID` (and matches
    the `actors` id for the display name)
  - `_parse_posts()`: parses `timeline_list_feed_units.edges[*].node`
  - public API: `fetch_timeline(username) -> Timeline`, `resolve(username) -> Profile`
  - `Timeline.payload_complete`: `False` means the page had no timeline section
    at all (Facebook's "reduced" variant); in that case "0 posts" does not mean
    the user never posted
  - **no GraphQL**, so no `doc_id` or cookie is required
- `search_client.py` — `search(query, category, limit)`:
  - `build_search_variables()` → `_graphql(SEARCH_RESULTS, …)`
  - `_parse_results()`: reads
    `data.serpResponse.results.edges[*].rendering_strategy.view_model.profile`,
    de-duplicates, skips malformed nodes, splits Page/User by `__typename`
  - `next_cursor()`: reads `page_info.end_cursor` (pagination hook, unused so far)
  - **does not parse the search result HTML** (results are client-rendered and
    absent from the initial HTML)
- `message_client.py` — `send(recipient, text)`:
  - `resolve_recipient()`: uid / username / `@vanity` /
    `facebook.com/<vanity>` / `profile.php?id=`
  - `_open_thread()`: **three fallbacks** — `direct thread URL` → `thread-list
    row` → `profile "Message" button` (a never-messaged recipient needs the third
    one; the button requires a real mouse event via `Input.dispatchMouseEvent`,
    because JS `.click()` does not reach React handlers)
  - composer detection requires an `aria-label` matching `^Write to .+` (a
    non-empty name), which excludes the thread list's `Write to ` new-message
    search box
  - `_compose_and_send()`: focus → `Input.insertText` → **verify the composer
    actually received the text** → click `Press enter to send` (fallback: Enter)
    → **poll until the composer clears** as the delivery signal
  - `send_via_graphql()`: a retained protocol entry point that currently raises
    `UnsupportedError` and explains why

### 5.6 L4 services

Thin orchestration: argument validation, client calls, exceptions → structured
results.

- `PostService.get_latest_post_time(username) -> PostTimeResult`
- `PostService.get_recent_posts(username, limit) -> list[Post]`
- `SearchService.search_people(q, limit)` / `search_pages(q, limit) -> list[SearchResult]`
- `MessageService.send_message(recipient, text) -> SendResult` (**never raises**;
  failures are written to `error`)
- `factory.Services` — the single composition point: settings → transport →
  session → clients → services. `get_services(account)` **caches one instance per
  account** (each with its own transport and session); `build_settings(account)`
  applies per-account overrides from `account.json` (e.g. `cdp_url`). In `auto`
  mode it injects `transport.cdp.cookies` into the session.

### 5.7 L5 server

`mcp.server.fastmcp.FastMCP` registers **5 tools**; input and output are pydantic
models:

| Tool | Signature | Returns |
|---|---|---|
| `get_user_latest_post_time` | `(username: str, account: str \| None = None)` | `PostTimeResult` |
| `get_user_recent_posts` | `(username: str, limit: int = 10, account: ...)` | `list[Post]` |
| `search_people` | `(q: str, limit: int = 10, account: ...)` | `list[SearchResult]` |
| `search_pages` | `(q: str, limit: int = 10, account: ...)` | `list[SearchResult]` |
| `send_message` | `(recipient: str, text: str, account: ...)` | `SendResult` |

`account` is always optional; when omitted, `FB_ACCOUNT` is used.
`tools/smoke_mcp.py` verifies in CI that every tool exposes `account` and that
it is not required.

> The SDK's `FastMCP` does not accept a `version` argument, so `server.py`
> rewrites `_mcp_server.version` after construction, making `initialize` report
> the project version `0.1.0`.

### 5.8 L0 credential isolation: the account store

Credentials **never enter the repository**. They live in the user's home
directory, one directory per account:

```
~/.facebook-mcp/                     # 0700
└── accounts/
    ├── default/                     # 0700
    │   ├── cookies.txt              # 0600, Netscape format
    │   └── account.json             # 0600, metadata + per-account overrides
    └── work/
        ├── cookies.txt
        └── account.json
```

**Cookie resolution order** (`SessionManager._load`):

| # | Source | Notes |
|---|---|---|
| 1 | `FB_COOKIE_FILE` | explicit path, highest priority (containers / ad-hoc debugging) |
| 2 | `~/.facebook-mcp/accounts/<FB_ACCOUNT>/cookies.txt` | **default**, isolated and persistent |
| 3 | `FB_COOKIE` | inline string; shows up in `ps` and shell history — last resort only |

With no source at all the server runs anonymously (public pages only).

**Per-account overrides supported by `account.json`**:

| Key | Effect |
|---|---|
| `c_user` / `authenticated` / `updated_at` | metadata written on export, used by `--list` |
| `cdp_url` | binds this account to **its own** Chrome instance (separate profile + port) |
| `cdp_page_url` / `user_agent` / `impersonate` | same, overriding the global settings |

**Which operations really need cookies**

| Operation | Needs cookies | Why |
|---|---|---|
| `get_user_latest_post_time` / `get_user_recent_posts` | **no** | the profile HTML is public |
| `search_people` / `search_pages` | **yes** | logged-out search returns 404; GraphQL needs `fb_dtsg` |
| `send_message` | **no (but needs a browser)** | sending uses Chrome's own session, never this project's cookie file |

**Permission safeguards**

- directories `0700`, files `0600`, `chmod` applied on write;
- if `cookies.txt` is readable by others when loading, a `WARNING` suggests
  `chmod 600`;
- account names are validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`,
  which blocks `../` path traversal.

### 5.9 Multiple accounts

Two levels, pick what you need:

**L1 — process-level switching (default)**

One MCP server process = one active account, chosen by `FB_ACCOUNT`. Multiple
accounts means multiple processes, each with its own env — full isolation, no
concurrency issues:

```json
{
  "mcpServers": {
    "facebook-default": {
      "command": ".../facebook-mcp",
      "env": { "FB_ACCOUNT": "default", "FB_TRANSPORT_MODE": "http" }
    },
    "facebook-work": {
      "command": ".../facebook-mcp",
      "env": { "FB_ACCOUNT": "work", "FB_TRANSPORT_MODE": "http" }
    }
  }
}
```

**L2 — call-level switching (implemented)**

Inside one process, pick the account per call: every MCP tool accepts an optional
`account` argument, and `get_services(account)` caches one `Services` per account
name (independent transport + session).

```
search: search_pages(q="pest control", account="work")
default: search_pages(q="pest control")          # uses FB_ACCOUNT
```

Concurrency caveat: concurrent calls for the same account share one curl_cffi
session. **Sending a message** takes over the browser page, so different accounts
must bind different `cdp_url` values (see `account.json`) or they will fight over
the same Chrome tab.

**Per-account browser instances** (`tools/launch_chrome_debug.sh`):

```bash
tools/launch_chrome_debug.sh --account default          # port 9222
tools/launch_chrome_debug.sh --account work             # port derived, 9223
tools/launch_chrome_debug.sh --account work --seed      # first run: seed cookies from the main Chrome
tools/launch_chrome_debug.sh --account work --stop      # stop only this account
```

| Behaviour | Detail |
|---|---|
| profile location | `~/.facebook-mcp/accounts/<account>/chrome` (0700) |
| port | derived from account order when unset (`9222`, `9223`, …) and written back to `cdp_url` in `account.json` |
| main Chrome | **never touched**; the user's everyday browser does not need to quit |
| `--seed` | copies only `Cookies` + `Local State` (~3.6 MB) instead of the whole 13 GB profile |
| `--stop` | matches processes by `--user-data-dir`, so other accounts are unaffected |

---

## 6. Data flow (actual paths)

### 6.1 Latest post time (public, zero cookies)

```
server.get_user_latest_post_time("exampleuser")
  → PostService.get_latest_post_time("exampleuser")
    → ProfileClient.fetch_timeline("exampleuser")
      → GET https://www.facebook.com/exampleuser          # curl_cffi, no login
      → iter_json_scripts(html)                            # 84 data-sjs JSON blocks
      → _parse_profile: userVanity + userID → Profile(uid=100000000000001)
      → _parse_posts:   timeline_list_feed_units.edges[].node
                       → Post(id, text, created_at, url, author_uid)
    → Timeline.latest_post (max by created_at)
  → PostTimeResult(ok, latest_post_time=..., post_url, post_text)
```

### 6.2 Keyword search (login required, pure protocol)

```
server.search_pages("pest control")
  → SearchService.search_pages(q, limit)
    → SearchClient.search(q, SearchCategory.PAGES, limit)
      → build_search_variables(q, "pages", limit)          # template + 5 substitutions
      → BaseClient._graphql(SEARCH_RESULTS, variables)
          → SessionManager.require_auth()                  # bootstraps from the browser if needed
          → _ensure_dtsg()                                 # fetches the home page when fb_dtsg is missing
          → POST /api/graphql/  doc_id=39411546341825607
               fb_dtsg / lsd / jazoest / variables
      → _parse_results(payload) → list[SearchResult]
      → payload contains errors → ParseError / AuthError (see §7)
```

The `transport_hint` rule decides whether this hop uses curl_cffi or the browser
(§9.3).

### 6.3 Send a message (login + browser required)

```
server.send_message(recipient="exampleuser", text="...")
  → MessageService.send_message(...)
    → MessageClient.send(recipient, text)
      → resolve_recipient(): username → uid (needs ProfileClient)
      → cdp_from(transport)                                # must be a browser transport
      → navigate  https://www.facebook.com/messages/t/<uid>/
      → wait for the composer; otherwise click the thread-list row
      → otherwise open the profile and click "Message"
      → Input.insertText → verify the composer received it
      → click "Press enter to send" (fallback: Enter key)
      → poll until the composer is empty
  → SendResult(ok=True, thread_id=...)
```

> There is no GraphQL here: the send is issued by Facebook's service worker.
> Details in §8.3 and §10 "M5".

---

## 7. Exception hierarchy

```
FacebookError                    core/exceptions.py — base class for every error
├── AuthError                    session missing/expired (fb_dtsg invalid, login demanded)
├── RateLimitError               rate limited (may carry retry_after)
├── ChallengeError               captcha / risk check — defined, never raised today
├── NotFoundError                user/page/thread does not exist (HTTP 404)
├── TransportError               transport failure
│   ├── HttpStatusError          non-2xx (4xx/5xx, excluding 404/403/401/429)
│   ├── ParseError               response shape changed, GraphQL errors, stale template
│   └── CdpError                 transport/cdp.py — a DevTools protocol call failed
└── UnsupportedError             this transport cannot perform the operation
                                 (e.g. sending a message without a browser)

TemplateError (routes/templates) — RuntimeError subclass: missing/corrupt template
                                  or unsupported category
```

**Translation rules**

| Source | Condition | Result |
|---|---|---|
| HTTP | `404` | `NotFoundError` |
| HTTP | `401` / `403` | `AuthError` |
| HTTP | `429` | `RateLimitError` |
| HTTP | any other `>= 400` | `HttpStatusError` |
| Relay `errors` | contains `missing_required_variable` | `ParseError` (asks you to re-capture the template) |
| Relay `errors` | `rate limit` while **not** authenticated (code `1675004`) | `AuthError` |
| Relay `errors` | contains `log in` / `login` / `permission` / `checkpoint` / … | `AuthError` |
| Relay `errors` | anything else | `ParseError` |

**How it surfaces through MCP**: `search_*` / `get_*` raise, and the SDK turns
that into `isError`; `send_message` is caught by `MessageService` and returned as
`SendResult(ok=False, error=...)`.

---

## 8. Reverse engineering and measured findings

### 8.1 Logged-out baseline (2026-10)

First, the **incognito baseline**: a fresh Chrome instance with an empty
`--user-data-dir`, `Network.clearBrowserCookies` before each scenario — i.e. a
visitor with **zero Facebook cookies**.

| Scenario | Incognito result | Logged in | Needs cookies |
|---|---|---|---|
| `GET /<username>` **with** `Accept: text/html` | **200, 963 KB, full timeline** | 200 | **no** |
| `GET /<username>` **without** `Accept` | 200 but only 509 KB, **no timeline** | – | no, but **silently degrades** |
| `GET /search/people?q=` | **404 `Not Found`** | 200 | **yes** |
| `GET /search/pages?q=` | **404** | 200 | **yes** |
| `GET /search/top?q=` | **404** | 200 | **yes** |
| `POST /api/graphql/` search (anonymous) | 200 + `{"message":"Rate limit exceeded","code":1675004}` | 200 + data | **yes** |
| `POST /ajax/mercury/send_messages.php` | 404 (removed) | 404 | – |

> ⚠️ **Anonymous GraphQL masquerades as rate limiting**: when logged out,
> Facebook does not return an auth error but `Rate limit exceeded`
> (code `1675004`). `BaseClient._raise_for_graphql_errors` distinguishes the two:
> logged out → `AuthError` (with advice to configure cookies); logged in →
> `RateLimitError`.

> ⚠️ **The `Accept` header is an implicit contract**: without `text/html`,
> Facebook serves a "reduced" profile page (no `timeline_list_feed_units`), which
> looks like "no posts" instead of an error. `_DEFAULT_HEADERS` always sends it,
> and `Timeline.payload_complete` marks "the page had no timeline section", with
> `PostService` suggesting a retry in the error message.

**Conclusion**

| Operation | Needs cookies | Evidence |
|---|---|---|
| `get_user_latest_post_time` / `get_user_recent_posts` | **no** | incognito: 200 with a complete timeline |
| `search_people` / `search_pages` | **yes** | incognito: 404; anonymous GraphQL rejected |
| `send_message` | **no, but needs a browser login** | uses Chrome's own session |

**Key structure of the profile HTML** (publicly readable):

```
84 <script type="application/json" data-sjs> blocks (all valid JSON)
├── "userVanity":"exampleuser" + "userID":"100000000000001"   → identity anchor
├── "LSD",[],{"token":"..."}                                    → lsd token
└── ...data.<user|page>.timeline_list_feed_units.edges[0].node
      ├── post_id / creation_time (Unix seconds) / permalink_url
      ├── actors[0].{id,name}                                   → author
      └── comet_sections.content.story.message.text             → body text
```

When logged out, `DTSGInitialData` is `{}` (empty) while `lsd` still has a value.

**Search result structure** (GraphQL, measured):

```
POST /api/graphql/  doc_id=39411546341825607
     fb_api_req_friendly_name=SearchCometResultsPaginatedResultsQuery
  → data.serpResponse.results.edges[*].rendering_strategy
        .view_model.profile.{id, name, url, is_verified, profile_picture.uri}
  → data.serpResponse.results.page_info.{has_next_page, end_cursor}
```

### 8.2 Key finding: `jazoest` can be derived

```python
jazoest = "2" + str(sum(ord(c) for c in fb_dtsg))   # measured 25724 == 25724
```

So **no browser is needed to compute the signature**. `fb_dtsg` can simply be
read from `DTSGInitialData` in the page HTML. That makes search fully doable with
curl_cffi; the browser is only needed for one-off cookie bootstrapping.

### 8.3 Messenger send path (M5 findings)

| Hypothesis | How it was checked | Result |
|---|---|---|
| page-level GraphQL mutation | captured `Network.requestWillBeSent` | ❌ only `FBScreenTimeLogger_syncMutation` |
| Lightspeed WebSocket | captured `Network.webSocketFrameSent` and base64-decoded | ❌ binary frames contain **no** message body |
| `Fetch`-domain interception | page-level `Fetch.enable` + `*graphql*` | ❌ not interceptable — issued by the **service worker** |
| legacy REST endpoint | `POST /ajax/mercury/send_messages.php` | ❌ 404 |
| clue | `bootloader-endpoint` loads `LSBumpThread` / `LSReplaceOptimisticMessage` | Lightspeed sync path |

→ The protocol cannot be forged cheaply, so `send_message` **drives the real UI
through CDP**.

### 8.4 Two Chrome remote-debugging pitfalls (already handled)

1. **Chrome 136+ refuses remote debugging on the default profile** — an explicit
   `--user-data-dir` is mandatory. This project uses
   `~/.facebook-mcp/accounts/<account>/chrome` per account, optionally seeded
   once with `Cookies` / `Local State` (~3.6 MB) to inherit an existing login
   without copying the 13 GB profile (§5.9).
2. **The DevTools WebSocket rejects handshakes carrying an `Origin` header**
   (403) — the client uses `suppress_origin=True` instead of loosening Chrome
   with `--remote-allow-origins`.

### 8.5 Channel selection (cheapest first)

| Priority | Channel | Use |
|---|---|---|
| 1 | `curl_cffi` | public pages, GraphQL read calls |
| 2 | CDP `Network` domain | discovering real endpoints / `doc_id` / `variables` |
| 3 | CDP page execution / UI driving | when login state or the service worker is involved |
| 4 | JSVMP reversing (not used) | only if signature-class protection appears |

### 8.6 Maintaining the integration after a Facebook release

1. Find the failing tool and decide whether the `doc_id` expired
   (`ParseError: … stale`) or the layout changed.
2. Re-capture with `tools/sniff.py`:

   ```bash
   .venv/bin/python tools/sniff.py \
       --navigate "https://www.facebook.com/search/pages?q=pest%20control" \
       --seconds 20 --save-templates
   ```

3. `--save-templates` records the captured `doc_id`s in
   `~/.facebook-mcp/accounts/<account>/account.json` under `doc_ids`, and
   rewrites `routes/templates/search_results.json` (only for queries on the
   `PROMOTED_TEMPLATES` allow-list); everything else stays in the gitignored
   `.cache/routes.json`, so dead templates (and the session ids inside them)
   never ship in the wheel.
4. That is normally the whole job — `routes/graphql.py` needs no edit. Only bump
   the `Route(...)` default when you want fresh installs to work before their
   first capture.
5. Only `routes/` changes — client and service logic stay untouched.

---

## 9. Key design decisions

### 9.1 The transport abstraction comes first

The `Transport` protocol is the only contract; `CurlCffiTransport`,
`CdpTransport` and `HybridTransport` all implement it. Upper-layer clients have
no idea how a request physically travels.

### 9.2 Routes are separate from clients

Interface definitions (`doc_id`, endpoints, `variables` templates) are **data**
that changes with every Facebook release; clients are **logic**. A release only
touches `routes/`. See §8.6.

### 9.3 Protocol-first transport selection

`BaseClient` decides `transport_hint`:

| Condition | Channel |
|---|---|
| public read (`requires_auth=False`) | curl_cffi |
| login required **and** cookies from the account store/config (`session.from_config`) | **curl_cffi (pure protocol)** |
| login required **and** no configured cookies | CDP (browser acts for us, bootstrapping cookies on first use) |
| sending a message | forced CDP (`cdp_from()`); raises `UnsupportedError` on HTTP-only transports |

### 9.4 Session lifecycle is its own layer

Cookie/token loading, validation, refresh and browser bootstrapping all live in
`SessionManager`, so no client has to deal with login state.

### 9.5 Services are use cases; clients are capabilities

An LLM calls `search_pages(q)` and never sees `doc_id` / `variables`; the MCP
layer only forwards, it contains no business logic.

---

## 10. Milestones

| Phase | Content | Status |
|---|---|---|
| M0 | Scaffolding: pyproject, venv (3.11), layered directories, core models/config/exceptions | ✅ |
| M1 | Transport layer: base + curl_cffi + proxy + retry/throttle + unit tests | ✅ |
| M2 | session + routes + profile_client + post_service | ✅ verified live |
| M3 | search_client + search_service + MCP tools + variables template | ✅ verified live (pure protocol) |
| M4 | CDP transport: discovery/connect/execute/cookie bootstrap/event buffering/capture tooling | ✅ verified live |
| M5 | message_client + message_service + `send_message` | ✅ verified live (browser path) |
| M6 | Tests (177), README, editable install, CI, MIT license | ✅; PyPI release not done |

### M1 details

- `transport/base.py`: `Transport` protocol + `HttpRequest` / `HttpResponse`
- `transport/http.py`: `CurlCffiTransport` (impersonation, backoff retry, throttling, loopback proxy bypass)
- `transport/proxy.py`: three-tier proxy resolution
- `transport/factory.py`: `get_transport()`

> **Measured here**: the machine has no direct DNS, but the system proxy
> `127.0.0.1:10808` works and the transport picks it up automatically.

### M2 details

- `core/extract.py`, `session/{state,extractor,manager}.py`
- `routes/{base,rest,graphql}.py`
- `clients/base.py` + `clients/profile_client.py`
- `services/post_service.py`, `services/factory.py`

### M3 details

- `routes/graphql.py`: `SEARCH_RESULTS` (`doc_id=39411546341825607`)
- `routes/templates/search_results.json`: captured `variables` template
- `routes/templates/__init__.py`: `build_search_variables()`
- `clients/search_client.py`, `services/search_service.py`
- `clients/base.py`: `jazoest` derivation, `_ensure_dtsg()`, Relay error mapping
- `tools/sniff.py` (`--navigate` / `--save-templates`), `tools/export_cookies.py`

### M4 details

- `transport/cdp.py`: `CdpTransport` (target discovery, `suppress_origin`,
  in-page `fetch`, `cookies` / `evaluate` / `navigate` / `drain_events`, event buffering)
- `transport/factory.py`: `HybridTransport` (routes by `transport_hint`)
- `transport/browsing.py`: `cdp_from()`
- `session/manager.py`: `cookie_provider` + `from_config`
- `services/factory.py`: injects the CDP cookie capability into the session (lazily)
- `tools/launch_chrome_debug.sh`, `tools/import_cookies.py`

### M5 details

- `clients/message_client.py`: recipient resolution, thread navigation (with
  fallbacks), input verification, send, delivery signal
- `services/message_service.py`: exceptions → `SendResult(ok=False, error=...)`
- Evidence and alternatives in §8.3; `send_via_graphql()` is a retained entry
  point that currently raises `UnsupportedError`

### M6 details

- Tests: 177 across 10 files (including a CDP integration test against a mock DevTools server)
- Layering check: `tools/check_layers.py` prints `upward deps: 0` / `import cycles: 0`
- Docs: `ARCHITECTURE.md` (zh) / `ARCHITECTURE.en.md` (en) / `README.md` / `CONTRIBUTING.md`
- CI: `.github/workflows/ci.yml` (lint / multi-version tests / packaging)
- `Makefile`: `make check` is locally equivalent to CI; `make hooks` installs the commit-msg hook
- `CONTRIBUTING.md` + `.gitmessage` + `LICENSE` (MIT)
- Install: `pip install -e ".[dev]"` works; not published to PyPI yet

---

## 11. Risks and compliance

- **Anti-abuse controls**: Facebook enforces strict rate limits and behavioural
  detection on bulk search/messaging. Throttle (`min_request_interval`) and avoid
  hammering from brand-new accounts.
- **Frontend churn**: `doc_id`, `variables` templates and HTML structure all
  change; iterate through `tools/sniff.py` + `routes/` (§8.6).
- **Fragile send path**: `send_message` depends on Messenger's DOM
  (`contenteditable`, `aria-label="Press enter to send"`). Layout changes cause a
  `ParseError` and require updating the selectors.
- **Compliance**: authorized accounts only. Marketing messages must comply with
  Facebook's platform policies and anti-spam law (CAN-SPAM, GDPR, …).

---

## 12. Dependency constraints

| Package | Version | Purpose |
|---|---|---|
| `mcp` | `>=1.2,<2` | **Official MCP Python SDK**; the server uses `mcp.server.fastmcp.FastMCP` |
| `curl_cffi` | `>=0.9` | browser TLS/JA3 impersonation |
| `pydantic` / `pydantic-settings` | `>=2.10` / `>=2.6` | models and configuration |
| `beautifulsoup4` + `lxml` | `>=4.12` / `>=5.2` | extracting embedded JSON from HTML |
| `websocket-client` | `>=1.8` | CDP (Chrome DevTools) transport |

`pip install -e ".[dev]"` additionally installs:

| Package | Purpose |
|---|---|
| `pytest` | running the test suite |
| `ruff` | lint + format (enforced in CI) |
| `websockets` | the mock DevTools server in `test_m4_cdp_integration.py` |
| `cryptography` | decrypting Chrome `v10` cookies in `tools/import_cookies.py` |

> ⚠️ **The standalone `fastmcp` package is not used** — only the `FastMCP`
> bundled with the official `mcp<2` SDK. Upgrading to `mcp>=2` requires
> re-validating `FastMCP.tool()`'s signature and the `serverInfo.version` rewrite.

---

## 13. Continuous integration

`.github/workflows/ci.yml` has three jobs:

| Job | Content |
|---|---|
| `lint & architecture` | `ruff check` + `ruff format --check`, `tools/check_layers.py`, commit-message convention (PRs), credential-leak guard (fails if `cookies.txt` / `account.json` / `.env` are tracked) |
| `test` | `pytest` on Python 3.11 / 3.12 / 3.13 (Ubuntu) and 3.11 (macOS), then `tools/smoke_mcp.py` |
| `build & install wheel` | `python -m build` → asserts `routes/templates/*.json` is packaged → installs the wheel in a clean venv and imports it |

**Why CI needs no network or credentials**: every test is offline —
`tests/test_transport.py` starts a local `http.server`,
`tests/test_m4_cdp_integration.py` starts a mock DevTools server with
`websockets` (HTTP discovery endpoints + the WebSocket protocol), and
`tests/conftest.py` points `FB_HOME` at a temp directory so no test can read the
real `~/.facebook-mcp`.

**Local equivalent**:

```bash
make check     # lint + fmt-check + layers + test + smoke
```

**Code style**: `ruff` (line-length 100, target py311) with
`E/F/I/B/BLE/SIM/RUF/UP`; `tests/*` relaxes `BLE001` (tests intentionally catch
broad exceptions).

**Commit convention**: Conventional Commits, validated by
`tools/commit_msg.py` — installed as a `commit-msg` hook by `make hooks`, and run
in CI over `--range origin/<base>..HEAD` on pull requests. Types:
`feat fix docs test refactor perf build ci chore revert`; scopes are listed in
`CONTRIBUTING.md`.
