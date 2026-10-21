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

- `send_message` against a session acting as a Page wasted ~35s polling for the
  profile "Message" button, which Facebook never renders for a Page (a Page
  cannot start a conversation with a stranger), and then blamed "the page layout
  changed". The client now detects the Page actor from the `i_user` cookie —
  in the exported snapshot (`SessionState.acting_as_page`) and in the live
  `Network.getCookies` response, so a switch made in the browser is seen too —
  and fails immediately with the real reason.
  Existing threads and the thread-list fallback are unaffected.

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
