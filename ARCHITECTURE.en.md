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
| Send a message (Messenger) | `send_message` | DM to a user/page | **yes** (protocol first, UI fallback) | ✅ |

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
│     factory.py — composition root: wires transport/session/clients│
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

### Dependency injection (as implemented)

Every cross-layer collaborator is **constructor-injected**, and
`services/factory.py` is the only assembly point:

```
Services(settings)                       # get_services(account) caches one graph per account
  ├─ get_transport(settings) ──────────→ CurlCffiTransport / CdpTransport / HybridTransport
  ├─ SessionManager(settings, cookie_provider=transport.cdp.cookies)
  ├─ ProfileClient(transport, session, settings)
  ├─ SearchClient(transport, session, settings)
  ├─ MessageClient(transport, session, settings, profile_client)   # reuses username resolution
  ├─ PostService(profile_client)
  ├─ SearchService(search_client)
  └─ MessageService(message_client)
```

Tests can therefore replace any single layer (`FakeCdp`, `_RecordingTransport`, a stub
`ProfileClient`), clients never instantiate each other, and `BaseClient` holds only
`transport` / `session` / `settings` (plus an optional service lookups it never
constructs itself).

The one deliberate exception is the **rate limiter**: `transport/throttle.py` keeps a
process-wide `limiter_for(settings)` registry keyed by `(account, interval)`, because
HTTP, CDP and the UI steps of a send must share one budget. It is injected into every
transport and client at construction, so the shared state never leaks into business
code.

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
│   │       └── message.py           #   SendResult
│   │
│   ├── transport/                   # L1 transports
│   │   ├── base.py                  #   Transport protocol + HttpRequest/HttpResponse
│   │   ├── http.py                  #   CurlCffiTransport
│   │   ├── cdp.py                   #   CdpTransport (WebSocket → DevTools)
│   │   ├── proxy.py                 #   explicit → env → macOS system proxy
│   │   ├── browsing.py              #   BrowserTransport protocol + cdp_from()
│   │   ├── throttle.py              #   RateLimiter: per account, shared by HTTP/CDP/UI
│   │   └── factory.py               #   get_transport() + HybridTransport
│   │
│   ├── session/                     # L2 session layer
│   │   ├── state.py                 #   SessionState (cookies + tokens + Page actor probe)
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
│   ├── sniff.py                     # CDP capture → doc_id / variables; --save-templates writes doc_ids to the account store
│   ├── export_cookies.py            # CDP → browser cookies → Netscape file
│   ├── import_cookies.py            # decrypt a local Chrome profile's Cookies
│   ├── check_layers.py              # verify the layering + detect import cycles
│   ├── smoke_mcp.py                 # MCP stdio handshake + tools/list smoke test
│   ├── commit_msg.py                # Conventional Commits validator (git hook + CI)
│   ├── lightspeed_probe.py          # protocol probe against the Lightspeed WS
│   └── launch_chrome_debug.sh       # per-account Chrome with the DevTools port
│
└── tests/                           # 355 tests (all offline)
    ├── conftest.py                  # points FB_HOME at a temp dir, isolating real credentials
    ├── fixtures/                    # profile_page.html / search_response.json
    ├── test_skeleton.py             # tool registration and schemas
    ├── test_transport.py            # local HTTP server: retry / throttle / error mapping
    ├── test_accounts.py             # account store: names, permissions, resolution, multi-account
    ├── test_m2_profile.py           # extraction, session, routes, profile parsing
    ├── test_m3_search.py            # variables template, result parsing, jazoest
    ├── test_m4_cdp.py               # CDP protocol, hybrid routing, cookie bootstrap
    ├── test_m4_cdp_integration.py   # real mock DevTools server (HTTP + WebSocket)
    ├── test_m5_message.py           # send flow, fallback chain, recipient resolution, Page refusal
    ├── test_route_doc_ids.py        # doc_id overrides, error envelopes, template promotion
    ├── test_smoke_mcp.py            # smoke-client pipe / EOF behaviour
    ├── test_sniff_capture.py         # capture loop reads already-buffered events
    ├── test_tools_account_binding.py # --account selects the browser, not just the folder
    ├── test_launch_script.py         # port allocation / exact --stop matching (--dry-run only)
    ├── test_check_layers.py          # the layering checker itself (incl. injected violations)
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
  | `route_doc_ids` | `FB_ROUTE_DOC_IDS` | `{}` (normally injected from `doc_ids` in `account.json`, §5.4) |

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
    credentials: Literal["include", "omit"] = "include"    # omit: browser fetch sends no cookies

@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    url: str
    headers: dict[str, str]
    text: str
    content: bytes
    # .ok / .json() / .raise_for_status()
    # .json() strips Facebook's `for (;;);` anti-hijacking prefix (GraphQL error envelopes carry it)

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
- `browsing.py` — the `BrowserTransport` protocol (`evaluate` / `navigate` /
  `send` / `cookies`) and `cdp_from(transport)`: structurally decides whether a
  transport can drive a browser (a `CdpTransport`, or the `.cdp` half of a
  `HybridTransport`). Clients that drive the UI depend on this protocol and
  import **no concrete transport class**.
- `throttle.py` — `RateLimiter`: one instance per account
  (`limiter_for(settings)`), shared by HTTP, CDP and the UI operations of
  `send_message`, so mixing channels cannot double the request rate.
- `factory.py` — `get_transport()` and `HybridTransport` (routes by
  `transport_hint`).

> **Implicit contract**: `_DEFAULT_HEADERS` in `base.py` always sends
> `Accept: text/html,application/xhtml+xml,…`. Without it, Facebook serves a
> "reduced" profile page that has no `timeline_list_feed_units`, so the scraper
> silently gets zero posts. `tests/test_logged_out_matrix.py` locks this down.

### 5.3 L2 session

- `state.py` — `SessionState`: a `cookies` dict plus `fb_dtsg`, `lsd` and
  `user_agent`; exposes `is_authenticated` (requires `c_user` + `xs`),
  `cookie_header()`, `as_headers()`, `merge_tokens()`, and the actor probe
  `impersonator_id` / `acting_as_page` (an `i_user` cookie means the session is
  switched into a Page, §5.5).
- `extractor.py` — `from_cookie_string()` (Netscape header string),
  `from_cookie_file()` (Netscape `cookies.txt`), `tokens_from_html()`
  (`DTSGInitialData` / `LSD`), `user_id_from_html()`.
- `manager.py` — `SessionManager`:
  - load priority: `FB_COOKIE_FILE` → `~/.facebook-mcp/accounts/<account>/cookies.txt`
    → `FB_COOKIE` → anonymous (see §5.8)
  - `require_auth()`: when logged out it **tries** to bootstrap cookies through
    `cookie_provider`, and only then raises `AuthError`
  - **persist right after bootstrapping**: the cookies land in the account store
    (0600) with `source: cdp-bootstrap` in `account.json`, so the next start is
    pure HTTP (`FB_PERSIST_BROWSER_COOKIES=false` disables it). A user-managed
    `FB_COOKIE` / `FB_COOKIE_FILE` is never overwritten
  - `invalidate()`: a jar written by the bootstrap is **deleted**, so the next
    `require_auth()` re-reads the browser; a user-managed jar is only voided in
    memory and the file is kept
  - `invalidate(reason)`: called after Facebook rejects the credentials. Always
    drops `fb_dtsg` / `lsd`; a browser-backed session also clears its cookies so
    the next `require_auth()` re-reads the live jar. A file/string jar cannot be
    refreshed in-process, so it is kept but marked rejected: later authenticated
    calls fail with "re-export and restart" instead of replaying a dead cookie
  - `rejected`: why the session was last rejected (`None` when healthy)
  - `learn_from_html()`: refreshes `lsd` / `fb_dtsg` from a page
  - `from_config`: whether cookies came from configuration or the account store
    rather than the browser; this drives the protocol-first transport choice (§9.3)

> Cookies are bootstrapped through
> `SessionManager(cookie_provider=cdp.cookies)`, injected by
> `services/factory.py`. `extractor.py` itself never touches CDP.

### 5.4 L2 routes (where reverse-engineering output lands)

This layer is **pure data plus data transformation** and depends on no other
layer (`base.py` only defines dataclasses).

- `base.py` — `Route(op, method, path, doc_id, friendly_name, requires_auth, anonymous)`.
  `anonymous=True` means **no session cookies are sent**: that is the profile
  page's situation — with cookies Facebook returns its logged-in SPA shell
  (`userVanity` / `timeline_list_feed_units` disappear) and only the logged-out
  variant embeds the ServerJS payload.
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
- ⚠️ That override is read **when `Services` is constructed** and cached per
  account (`get_services(account)` is `lru_cache`d). After refreshing
  `account.json`, **restart the MCP server** for it to take effect.

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
  - **no GraphQL and no cookies**: `PROFILE_PAGE.anonymous=True` skips the
    session headers, because the logged-in page is a client-rendered shell that
    parses to zero posts
- `search_client.py` — `search(query, category, limit)`:
  - `build_search_variables()` → `_graphql(SEARCH_RESULTS, …)`
  - `_parse_results()`: reads
    `data.serpResponse.results.edges[*].rendering_strategy.view_model.profile`,
    de-duplicates and skips malformed nodes. **The search tab decides the
    category**: Facebook labels page results `__typename="User"` with no category
    fields in the pages tab (measured 2026-10: 12 of 12), so only an explicit
    `Page`/`PageProfile` overrides the tab — which keeps a page out of
    `search_people` without filtering the whole `search_pages` result to empty
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
  - **the third fallback is unavailable to a Page**: Facebook does not render the
    profile `Message` button for a Page actor (a Page cannot start a conversation
    with a stranger). Detection looks for an `i_user` cookie in the exported
    cookie, preferring the live `Network.getCookies` response: when it is
    non-empty the browser is authoritative (so a stale snapshot cannot
    misjudge), and only an empty or unreachable live jar falls back to the
    exported snapshot (`SessionState.acting_as_page`). When the thread row
    fails there, the client raises `UnsupportedError` with the reason instead of
    polling 35s for a button that will never appear. Existing threads still work
    (levels 1-2).
  - composer detection requires an `aria-label` matching `^Write to .+` (a
    non-empty name), which excludes the thread list's `Write to ` new-message
    search box. Only composers inside the main pane qualify, so a docked chat
    popup can never supply the box
  - **the recipient is verified before typing**: the thread-list fallback matches
    the full thread id in the href (never a substring) and `_thread_id()` no
    longer falls back to the uid after a redirect. Once a conversation is open
    the client requires "thread id == uid" or "`Write to <name>` matches the
    recipient's display name", otherwise it refuses to send (`nothing was
    sent`). The label is read once more before clicking send, in case a
    re-render swapped the box
  - `_compose_and_send()`: focus → `Input.insertText` → verify the composer
    actually received the text → click `Press enter to send` (fallback: Enter)
    → poll until the composer clears
  - **`SendResult.delivery` distinguishes four states**: `not_sent` (nothing was
    submitted, safe to retry), `submitted` (handed to Messenger, but the web UI
    exposes no server acknowledgement, so a retry may duplicate), `rejected`
    (Messenger marked it unsent — `SendRejectedError`, safe to retry) and
    `unknown` (send clicked but the outcome is unobservable: composer vanished, a
    reply was lost, the stream dropped, or the UI spotted a possible failure
    marker — `SendUncertainError`).
    **Only `submitted` is `ok=True`**; the other three are `ok=False`, and what
    differs is whether a retry is safe. `MessageService` maps
    `SendUncertainError` → `unknown`, `SendRejectedError` → `rejected`, and every
    other `FacebookError` → `not_sent` — but the client only lets *pre-click*
    failures escape as plain `FacebookError`, so an error after the click is
    always `unknown`.
  - **rejection detection is best-effort**: the "Couldn't send" marker is UI text
    (en/zh/es are recognised). Only *leaf* nodes whose whole trimmed text equals a
    marker are counted, and they must sit in the bubble that holds the text we
    just sent. Both sides are whitespace-collapsed before comparing (`innerText`
    keeps line breaks while the needle is a single-line prefix, so a multi-line
    message would otherwise be missed). An earlier version searched article bodies
    for the substring, so a normal message like "fixed the Couldn't send issue"
    was reported rejected.
    Failed bubbles are counted before and after, and only a *new* one counts, so
    an older failure cannot taint a later send. The same bubble's accessibility
    label still says "Message sent", which is why the text cannot be used to
    confirm delivery.
  - **protocol first**: `send()` tries `LightspeedSender` (WebSocket, no browser)
    and only falls back to the UI flow above when the channel is unusable
    (`FB_MESSAGE_TRANSPORT=auto`). The protocol path returns the server's
    `message_id`; the UI path has no acknowledgement to read
  - `send_via_graphql()`: no page-level GraphQL mutation exists, so this stays an
    explicit dead end

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

`FB_COOKIE_FILE` and `FB_COOKIE` are the *global* credentials (row 2 is already
per-account), and they belong to the `FB_ACCOUNT` identity: `build_settings(account="work")`
drops them, so `work` can only use its own account store. Otherwise a cookie file
outranks the store and a cross-account call silently borrows the other identity.

With no source at all the server runs anonymously (public pages only).

**Per-account overrides supported by `account.json`**:

| Key | Effect |
|---|---|
| `c_user` / `authenticated` / `updated_at` | metadata written on export, used by `--list` |
| `cdp_url` | binds this account to **its own** Chrome instance (separate profile + port) |
| `doc_ids` | `{"<route.op>": "<doc_id>"}`: refreshes the rotating GraphQL `doc_id` without a source change (§5.4) |
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
tools/launch_chrome_debug.sh --account work --dry-run   # print the plan, launch nothing
```

| Behaviour | Detail |
|---|---|
| profile location | `~/.facebook-mcp/accounts/<account>/chrome` (0700) |
| port | first free port that no other account has recorded and nothing is listening on; the account's own recorded port is reused when still free. Written back to `cdp_url` |
| main Chrome | **never touched**; the user's everyday browser does not need to quit |
| `--seed` | copies only `Cookies` + `Local State` (~3.6 MB) instead of the whole 13 GB profile |
| `--stop` | exact `--user-data-dir` match (regex metacharacters escaped + argument boundary), so other accounts are unaffected |
| `--dry-run` | prints account/port/cdp_url and launches nothing (useful to verify allocation) |

> Ports are deliberately **not** derived from account order: inserting an account
> makes positional allocation collide, and a live DevTools listener says nothing
> about which account owns it. The launcher therefore scans for a free port and
> reuses a recorded one only while that account's Chrome is actually running.

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

### 6.3 Send a message (protocol first, UI fallback)

```
server.send_message(recipient="exampleuser", text="...")
  → MessageService.send_message(...)
    → MessageClient.send(recipient, text)
      → resolve_recipient(): username → uid / thread key
      → protocol path (FB_MESSAGE_TRANSPORT=lightspeed|auto)
          → LightspeedSender.send(): require_auth → shared limiter → connect
          → task 46 → read the verdict
              replaceOptimsiticMessage + mid → submitted + message_id
              markOptimisticMessageFailed   → rejected; in auto mode the UI probes once
              no reply / dropped stream     → unknown (no automatic resend)
      → fallback path (FB_MESSAGE_TRANSPORT=ui, or channel unusable)
          → CDP: open thread → verify recipient → type → click → classify (no `rejected`)
  → SendResult(ok, delivery, thread_id, message_id, error)
```

> The UI path stays because only it can **start** a conversation (the socket only
> posts to a known thread key), and because it is what distinguishes "this thread
> needs the UI" from "this recipient has messaging turned off".
>
> **Rejection policy**: an explicit gateway refusal means the server said it was
> *not* sent, so `auto` mode probes once through the UI (nothing can be duplicated)
> while `lightspeed` mode fails outright. A lost reply or a dropped stream is
> `unknown`, and **no mode** retries that automatically.

---

## 7. Exception hierarchy

```
FacebookError                    base class, defined in core/exceptions.py
                                 (every class below lives there except CdpError)
├── AuthError                    session missing/expired (fb_dtsg invalid, login demanded)
├── RateLimitError               rate limited (may carry retry_after)
├── ChallengeError               captcha / risk check — defined, never raised today
├── NotFoundError                user/page/thread does not exist (HTTP 404)
├── TransportError               transport failure
│   ├── HttpStatusError          non-2xx (4xx/5xx, excluding 404/403/401/429)
│   ├── ParseError               response shape changed, GraphQL errors, stale template
│   └── CdpError                 **defined in transport/cdp.py** (the only one
│                                 outside core) — a DevTools protocol call failed
├── UnsupportedError             transport/account lacks it (no browser, Page chat)
├── SendRejectedError            Messenger marked it unsent ("Couldn't send") — retry is safe
└── SendUncertainError           send clicked, outcome unknown — do not blind-retry

TemplateError (routes/templates) — RuntimeError: missing/corrupt/unsupported
```

**Translation rules**

| Source | Condition | Result |
|---|---|---|
| HTTP | `404` | `NotFoundError` |
| HTTP | `401` / `403` (authenticated request) | `AuthError` (and `SessionManager.invalidate()`) |
| HTTP | `429` | `RateLimitError` |
| HTTP | any other `>= 400` | `HttpStatusError` |
| Relay `errors` | contains `missing_required_variable` | `ParseError` (asks you to re-capture the template) |
| Relay `errors` | `rate limit` while **not** authenticated (code `1675004`) | `AuthError` |
| Relay `errors` | contains `log in` / `login` / `permission` / `checkpoint` / … | `AuthError` (and `SessionManager.invalidate()`) |
| Relay `errors` | anything else | `ParseError` |
| Body | not JSON (e.g. HTML after the `for (;;);` guard) | `ParseError` hinting the `doc_id` may have rotated |
| Legacy envelope | `{"error": <code>, "errorSummary": ...}` (no `errors`) | `ParseError`; `1357031` (content no longer available) names the rotated `doc_id` |

**How it surfaces through MCP** (deliberately not uniform):

| Tool | On failure |
|---|---|
| `get_user_latest_post_time` | does **not** raise; returns `PostTimeResult(ok=False, error=...)` |
| `get_user_recent_posts` | raises → the SDK reports `isError` |
| `search_people` / `search_pages` | raises → `isError` |
| `send_message` | caught by `MessageService` → `SendResult(ok=False, delivery="not_sent"|"rejected"|"unknown", error=...)` |

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

**Being logged in changes the HTML variant** (same URL, same moment, 2026-10):

| Request | Size | `userVanity` | `timeline_list_feed_units` | Result |
|---|---|---|---|---|
| anonymous | 961 KB | 2 | 4 | complete embedded ServerJS payload |
| with `c_user`/`xs` | 1 181 KB | **0** | **0** | logged-in SPA shell; data comes from GraphQL |

The profile page therefore **has to be fetched anonymously**, which is why
`Route.anonymous` exists. An earlier implementation sent the cookies along and
broke "latest post time" precisely for accounts that had search credentials
configured.

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
| legacy REST endpoint | `POST /ajax/mercury/send_messages.php` | ❌ 404 |
| send is issued by a service worker | subscribed per session, decoded every frame | ❌ **earlier reading was wrong**: the frame belongs to the **page** session |
| send travels over the Lightspeed WebSocket | `Network.webSocketFrameSent` + base64 decode | ✅ confirmed |

**Corrected finding**: the message leaves as a **JSON task** on
`wss://gateway.facebook.com/ws/lightspeed` (a WebSocket owned by the page), wrapped
in a 16-byte binary header. The earlier "binary frames carry no body" conclusion
came from decoding only one base64 layer and from not collecting frames across
sessions; the text actually sits in `payload.tasks[0].payload.text`.

Frame layout (verified byte-for-byte against a live frame; codec in
`transport/lightspeed.py`):

```
0f | req_id(u16) | 02 00 00 | "{}" | 0d | req_id(u16) | u32(len(body)+2) | 80 | body
body = {"app_id":"2220391788200892","payload":"…","request_id":N,"type":3}
  payload = {"epoch_id":…,"version_id":"…","data_trace_id":"…","tasks":[…]}
    task 46 = send message (thread_id/otid/text/…), task 21 = mark read
```

**Working, no browser required**: task 46 goes out directly and the server
answers `replaceOptimsiticMessage` with a real `mid.$…`. **No warm-up is
required** (measured: the same thread accepts a bare send).

> ⚠️ **Self-correction**: an earlier version of this document claimed the
> thread-context fetches were a prerequisite, based on one thread that rejected a
> bare send and accepted one after them. A control experiment disproved it —
> another public page conversation accepted a bare send immediately. The real
> difference is **thread-specific**: some threads (a page's own "Get started"
> conversation, for instance) reject a bare send and need the context queries,
> while an ordinary public page thread does not. Warm-up is therefore the
> `warmup=True` option, not a general precondition.

**`Couldn't send` is an ambiguous verdict.** At least three causes produce it:

1. the recipient (a public page) has **messaging turned off**;
2. the **thread type** does not accept a protocol send (it needs the UI path);
3. thread context is missing (probe with `warmup=True`).

The client does not guess: a refusal is reported verbatim as `SendRejectedError`.
In `auto` mode it then tries the UI once — the server already stated the message
was *not* sent, so nothing can be duplicated, and the UI distinguishes "restricted
thread" from "closed inbox". In `lightspeed` mode the refusal is final.

Implementation: `clients/lightspeed_client.py` (connect → warm-up → send → parse the
verdict), with the codec in `transport/lightspeed.py`. `send_message` is now
**protocol-first**: with `FB_MESSAGE_TRANSPORT=auto` it tries the WebSocket and
only falls back to the CDP UI path when the channel is unusable; `ui` forces the
old path and `lightspeed` forbids the fallback. The protocol path also returns the
server's `message_id`, which the UI path cannot obtain.

**The actor type changes the UI**: while acting as a Page, Facebook opens the page
inbox instead of personal Messenger and does **not** render the `Message` button
on someone else's profile (a Page cannot start a conversation with a stranger).
The third fallback therefore only works for personal accounts; see
`acting_as_page` in §5.5.

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

1. Find the failing tool and read the `ParseError`:
   - `doc_id ... was rejected` / `Facebook error 1357031` → **`doc_id` rotated**
     (the common case);
   - `...template are stale` → the `variables` template expired;
   - `composer did not appear` and similar → DOM layout changed.
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
4. That is normally the whole job — `routes/graphql.py` needs no edit.
5. **Restart the MCP server** so the new `doc_ids` are picked up (§5.4).
6. **The default may be refreshed, but only as a deliberate "release refresh"
   commit** that says it is the bootstrap value for fresh installs. Fixing your
   own machine always goes through the account store, never through the source.
7. Interface parameters / `doc_id` / `variables` templates only touch `routes/`
   (or the account store). **DOM-driven paths (`send_message`) are the
   exception**: Messenger selectors live in the client, so a layout change
   requires a client change — which is why §11 lists that path as brittle.

---

## 9. Key design decisions

### 9.1 The transport abstraction comes first

There are two contracts, not one:

- `Transport` (`transport/base.py`) — HTTP-shaped requests. `CurlCffiTransport`,
  `CdpTransport` and `HybridTransport` implement it, and business clients have no
  idea how a request physically travels.
- `BrowserTransport` (`transport/browsing.py`) — the capability to drive a real
  UI (`evaluate` / `navigate` / `send` / `cookies`). Paths that must operate a
  browser (e.g. `send_message`) depend only on this protocol, obtained through
  `cdp_from()`, and import **no concrete transport class** (they catch
  `TransportError` rather than `CdpError`).

### 9.2 Routes are separate from clients

Interface definitions (`doc_id`, endpoints, `variables` templates) are **data**
that changes with every Facebook release; clients are **logic**. Changes to
**interface parameters and templates** only touch `routes/`, and the
`account.json -> doc_ids` override in §5.4 goes one step further: when only the
`doc_id` rotates, not even `routes/` has to change. The **exception** is the
DOM-driven `send_message`: its selectors live in the client, so a Messenger
layout change requires a client change (§11 already lists it as brittle). See
§8.6.

### 9.3 Protocol-first transport selection

`BaseClient` decides `transport_hint`:

| Condition | Channel |
|---|---|
| public read (`requires_auth=False`) | curl_cffi |
| login required **and** the session already holds cookies (store, config, or just bootstrapped) | **curl_cffi (pure protocol)** |
| login required **and** no cookies at all | CDP **once** to bootstrap them; that same request then uses curl_cffi |
| sending a message | protocol first (Lightspeed WS); the CDP UI is only a fallback (`FB_MESSAGE_TRANSPORT`) |

### 9.4 Session lifecycle is its own layer

Cookie/token loading, browser bootstrapping and post-rejection refresh live in
`SessionManager`, so no client has to deal with login state. Two boundaries worth
stating:

- **validation is passive**: `is_authenticated` only checks that `c_user` / `xs`
  exist and cannot tell whether they expired. The real signal comes from the
  server (HTTP 401/403 or a Relay auth error), which the client reports to
  `invalidate()`; the next call then re-bootstraps or fails explicitly.
- **only refreshable things are refreshed**: `fb_dtsg` / `lsd` are re-learned
  from a page and browser cookies are re-read; a file/string jar cannot be
  refreshed in-process and must be re-exported with a restart.

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
| M6 | Tests (355), README, editable install, CI, MIT license | ✅; PyPI release not done |

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
  fallbacks), conversation verification, input verification, send, delivery state
- `services/message_service.py`: exceptions → `SendResult(ok=False, delivery=..., error=...)`
- Evidence and alternatives in §8.3; `send_via_graphql()` is a retained entry
  point that currently raises `UnsupportedError`

### M6 details

- Tests: 355 across 17 files (including a CDP integration test against a mock DevTools server)
- Layering check: `tools/check_layers.py` prints `upward deps: 0` / `import cycles: 0`
- Docs: `ARCHITECTURE.md` (zh) / `ARCHITECTURE.en.md` (en) / `README.md` / `CONTRIBUTING.md`
- CI: `.github/workflows/ci.yml` (lint / multi-version tests / packaging)
- `Makefile`: `make check` runs the main local checks (lint / fmt / layers / test /
  smoke); CI additionally runs the commit-message convention, the credential-leak
  guard and the wheel build/install; `make hooks` installs the commit-msg hook
- `CONTRIBUTING.md` + `.gitmessage` + `LICENSE` (MIT)
- Install: `pip install -e ".[dev]"` works; not published to PyPI yet

---

## 11. Risks and compliance

- **Anti-abuse controls**: Facebook enforces strict rate limits and behavioural
  detection on bulk search/messaging. Throttle (`min_request_interval`) and avoid
  hammering from brand-new accounts. The interval is enforced per account by
  `transport/throttle.py`, shared by HTTP, CDP and the UI steps of messaging, so
  switching channel cannot bypass it.
- **Frontend churn**: `doc_id`, `variables` templates and HTML structure all
  change. `doc_id` rotation is absorbed by the `account.json` override layer (no
  source change when only it moves); everything else iterates through
  `tools/sniff.py` + `routes/` (§8.6).
- **Fragile send path**: `send_message` depends on Messenger's DOM
  (`contenteditable`, `aria-label="Press enter to send"`, the `Write to <name>`
  label). Layout changes cause a `ParseError` and require updating the selectors
  in the client — which is why §8.6/§9.2 scope "only `routes/` changes" to
  interface parameters and templates. The recipient is verified before typing,
  and the client refuses to send when it cannot read the name.
- **Delivery is not provable**: the web UI exposes no server acknowledgement, so
  `delivery="submitted"` means handed over, not received; `unknown` must be
  checked in the conversation before retrying. Measured behaviour: Facebook
  clears the composer first and only then stamps the bubble "Couldn't send"
  (and that bubble's accessibility label still says "Message sent"), so
  `rejected` relies on matching UI text — best-effort, and it breaks with a
  language change or a redesign.
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
make check     # lint + fmt-check + layers + docs + test + smoke
```

**Code style**: `ruff` (line-length 100, target py311) with
`E/F/I/B/BLE/SIM/RUF/UP`; `tests/*` relaxes `BLE001` (tests intentionally catch
broad exceptions).

**Commit convention**: Conventional Commits, validated by
`tools/commit_msg.py` — installed as a `commit-msg` hook by `make hooks`, and run
in CI over `--range origin/<base>..HEAD` on pull requests. Types:
`feat fix docs test refactor perf build ci chore revert`; scopes are listed in
`CONTRIBUTING.md`.
