# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
(see [CONTRIBUTING.md](./CONTRIBUTING.md)).

## [Unreleased]

### Added

- `ARCHITECTURE.en.md`: full English translation of the architecture document,
  keeping the same section numbering as the Chinese original.
- `CONTRIBUTING.md` and `CHANGELOG.md` now document the documentation-sync and
  documentation-privacy rules.
- GraphQL `doc_id` overrides: the registry in `routes/graphql.py` is now only a
  bootstrap default, and `BaseClient._doc_id()` prefers the `doc_ids` map in
  `~/.facebook-mcp/accounts/<name>/account.json`. `doc_id` rotates with every
  Facebook frontend build, so refreshing it no longer needs a source change.

### Changed

- Docs now state that a refreshed `account.json` `doc_id` is read when the server
  starts, so the MCP server has to be restarted; §8.6 distinguishes a rotated
  `doc_id` (legacy envelope) from an expired `variables` template.
- `tools/sniff.py --save-templates` now records captured `doc_id`s in the
  account store and only promotes the search `variables` template into the
  package; other captures stay in the gitignored `.cache/routes.json` instead of
  accumulating dead templates (and their captured ids) inside the wheel.
- Template promotion is atomic, so an interrupted capture can no longer leave a
  truncated `search_results.json` behind.
- Documentation audited line by line against the code: `Settings` and
  `account.json` tables gained `route_doc_ids` / `doc_ids`, `SessionState`
  documents the actor probe, the exception tree reflects the widened
  `UnsupportedError`, and the stale test counts (146 / 177) and the CN/EN
  divergences in §6.3 and §7 were corrected. `.env.example` no longer repeats the
  CDP block and documents `FB_ROUTE_DOC_IDS`.

### Fixed

- **`--account` selected only the storage folder, not the browser.**
  `tools/export_cookies.py` and `tools/sniff.py` used `get_settings()`, so they
  read `FB_CDP_URL` and could export account A's cookie jar into account B's
  directory. Both now build settings through `build_settings(account)`, which
  applies that account's `cdp_url`. `export_cookies --list` shows the binding.
- **The launcher allocated colliding ports and could kill the wrong browser.**
  Ports were derived from the account's position in a sorted list, so inserting
  an account renumbered the others, and a live DevTools listener on the computed
  port was treated as success without checking ownership. Allocation now picks a
  port no other account recorded and nothing listens on, reusing the account's
  own recorded port only while its Chrome runs. `--stop` matched the profile path
  with `pgrep -f` as an unescaped regex, so account `a.b` also matched `axb`;
  the pattern is now escaped and anchored to an argument boundary. `--dry-run`
  was added so allocation can be verified without launching a browser.
- **`send_message` concatenated an existing draft.** `insertText` appends at the
  caret, so an unsent draft became `old draft: hello`. The client now refuses
  before typing when the composer is non-empty, and nothing is sent.
- **A vanished composer counted as a successful send.** `_composer_text()`
  returned `""` both for "empty" and "not found", so a re-render or navigation
  was reported as delivery. It now returns `None` for a missing composer, and the
  delivery check raises instead of assuming success.
- **curl_cffi's own cookie jar defeated `anonymous` routes too.** Skipping the
  `Cookie` header was not enough: a GraphQL search response sets `c_user`/`xs`
  on the transport's jar, and the next "anonymous" profile fetch then carried
  them and received the logged-in SPA shell. `credentials="omit"` now routes
  through a dedicated session whose jar is cleared before every request.
- **CDP requests ignored `anonymous` routes.** Even when the client omitted the
  `Cookie` header, the in-page `fetch()` still used `credentials: "include"`, so
  a logged-in browser sent the session to the profile page — defeating
  `PROFILE_PAGE.anonymous`. `HttpRequest.credentials` now carries that intent and
  the CDP transport forwards it.
- **`tools/sniff.py` read the raw WebSocket and lost buffered events.** Requests
  fired during `Page.navigate()` are buffered by the CDP client while it waits
  for the command reply; the capture loop skipped that buffer, so a capture could
  report zero routes even though the requests had arrived. It now drains through
  `CdpTransport.drain_events()`.
- **`#HttpOnly_` cookies were dropped.** curl writes HttpOnly entries with that
  prefix; the parser treated every `#` line as a comment and silently produced an
  unauthenticated jar.
- **Retries bypassed the request throttle.** `_throttle()` ran once before the
  retry loop, so a retry could fire immediately after the previous attempt.
- **The layering checker could not fail.** Relative imports were resolved to the
  package only and absolute `facebook_mcp.*` imports were dropped, so injected
  upward imports produced "0 violations". Both forms are now resolved to a full
  module path, `from pkg import mod` is handled, results are de-duplicated, and
  `tests/test_check_layers.py` proves the checker catches injected violations.
- **`get_user_latest_post_time` failed whenever cookies were configured.** The
  profile page is served as a logged-in SPA shell when a session cookie is sent
  (no `userVanity`, no `timeline_list_feed_units`), so the parser reported
  "page shape changed?" for exactly the setup the docs recommend. `Route` gained
  an `anonymous` flag and `PROFILE_PAGE` now fetches without credentials;
  search still uses the session. Reported as a regression by an end-to-end check.
- **A rotated `doc_id` surfaced as a raw `JSONDecodeError`.** Facebook answers an
  unknown query id with HTTP 200 and a legacy envelope guarded by its
  anti-JSON-hijacking prefix (`for (;;);{"error":1357031,...}`).
  `HttpResponse.json()` now strips the guard, and `BaseClient` turns a
  still-undecodable body or a legacy `{"error": ...}` envelope into a
  `ParseError` that names the rejected `doc_id` and points at
  `tools/sniff.py --save-templates`. Previously the error bypassed the exception
  hierarchy entirely and hid the real cause.
- `_acting_as_page()` checked the exported cookie snapshot first and returned
  early, contradicting its own docstring: a jar exported while acting as a Page
  kept refusing the profile fallback after the browser had been switched back to
  the personal identity. The live `Network.getCookies` response is now
  authoritative when non-empty; the snapshot is only a fallback.
- `tools/smoke_mcp.py` now drains the child's stderr on a background thread. A
  chatty or crashing server could fill the pipe buffer and block before
  replying, which the smoke test reported as a bogus handshake timeout.
- `tools/sniff.py` promoted any capture into `search_results.json`; the allowed
  set is now an explicit `{friendly_name: filename}` mapping, so adding a second
  promoted query can no longer overwrite the search template. Its route-table
  printout no longer tells operators to hardcode a rotated `doc_id` in
  `routes/graphql.py`, which CONTRIBUTING.md forbids.
- Removed dead code: `transport/cdp.py::ensure_supported()` (never called) and
  the unused `Recipient` model.
- `send_message` against a session acting as a Page wasted ~35s polling for the
  profile "Message" button, which Facebook never renders for a Page (a Page
  cannot start a conversation with a stranger), and then blamed "the page layout
  changed". The client now detects the Page actor from the `i_user` cookie —
  in the exported snapshot (`SessionState.acting_as_page`) and in the live
  `Network.getCookies` response, so a switch made in the browser is seen too —
  and fails immediately with the real reason.
  Existing threads and the thread-list fallback are unaffected.
- `tools/smoke_mcp.py` closed stdin immediately after writing the handshake
  (`subprocess.run(input=...)`). The SDK cancels in-flight handlers when the read
  stream ends, so `tools/list` — which has to build one JSON schema per tool —
  was dropped on slower machines and CI failed with `no tools/list reply` while
  `initialize` had answered. The client now keeps the session open until the
  replies arrive (as a real MCP client does) and prints the server's stderr on
  every failure, not just when `initialize` goes missing.

### Planned

- Capture the Messenger send mutation (service-worker / Lightspeed) and replace
  the browser-driven `send_message` with a protocol implementation.
- Pagination for `search_*` (the `end_cursor` is already parsed by
  `SearchClient.next_cursor`).
- Publish to PyPI.

## [0.1.0] - 2026-10-20

First working release: all five MCP tools verified against the live site.

### Added

- **MCP server** (`mcp<2` official SDK, `mcp.server.fastmcp.FastMCP`) exposing
  `get_user_latest_post_time`, `get_user_recent_posts`, `search_people`,
  `search_pages`, `send_message`.
- **Layered architecture** (`core → transport → session/routes → clients →
  services → server`) with a machine-checked dependency rule
  (`tools/check_layers.py`, 0 upward deps, 0 cycles).
- **Transport layer**: `curl_cffi` (browser TLS/JA3 impersonation, retry with
  backoff, request throttling, loopback bypass) and Chrome DevTools over
  WebSocket, plus `HybridTransport` routing per request.
- **Profile scraping** without any credentials: parses the embedded ServerJS
  `timeline_list_feed_units` payload.
- **Search** via Relay GraphQL: captured `doc_id` plus a `variables` template
  stored as data in `routes/templates/search_results.json`; `jazoest` derived
  from `fb_dtsg`.
- **Messenger send** driven through the real UI via CDP, with three fallbacks
  for opening a conversation (direct thread URL → thread-list row → profile
  "Message" button).
- **Account isolation**: credentials live in `~/.facebook-mcp/accounts/<name>/`
  (mode `0700`/`0600`), never in the repository. Per-call `account` argument on
  every tool, plus `FB_ACCOUNT` for process-level selection.
- **Proxy support**: explicit `FB_PROXY` → standard env vars → macOS system
  proxy (`scutil --proxy`).
- **Tooling**: `probe.py`, `sniff.py` (capture `doc_id`/templates via CDP),
  `export_cookies.py`, `import_cookies.py` (decrypt Chrome `v10` cookies via
  Keychain), `check_layers.py`, `smoke_mcp.py`, and a per-account
  `launch_chrome_debug.sh`.
- **CI**: ruff lint + format, layered-import check, secret-leak guard, pytest on
  Python 3.11/3.12/3.13 (Ubuntu) and 3.11 (macOS), MCP stdio smoke test, wheel
  build with package-data assertions.
- **Tests**: 146 offline tests (local `http.server` + a mock DevTools server).

### Fixed

- Chrome >= 136 refuses `--remote-debugging-port` with the default profile;
  use a dedicated `--user-data-dir`.
- DevTools WebSocket handshakes are rejected with HTTP 403 when an `Origin`
  header is present; suppressed instead of weakening Chrome flags.
- Profile pages silently lose their timeline when the `Accept: text/html`
  header is missing; the parser now flags the reduced payload
  (`Timeline.payload_complete`) and the error message says so.
- Anonymous GraphQL calls are answered with `Rate limit exceeded`
  (code `1675004`) rather than an auth error; mapped to `AuthError` with an
  actionable message.
- The Messenger thread list's "Write to " search box was mistaken for a
  conversation composer, sending text into the wrong element.

[Unreleased]: https://github.com/YoTro/facebook-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/YoTro/facebook-mcp/releases/tag/v0.1.0
