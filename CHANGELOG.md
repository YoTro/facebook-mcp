# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
(see [CONTRIBUTING.md](./CONTRIBUTING.md)).

## [Unreleased]

### Added

- **`send_message` works without a browser.** Messenger's send is reproducible
  over `wss://gateway.facebook.com/ws/lightspeed`: task 46 alone makes the server
  answer `replaceOptimsiticMessage` with a real `mid.$…`. (Warm-up tasks are
  available as an option, but a control experiment showed they are not required —
  see the corrections below.) `clients/lightspeed_client.py`
  implements the sequence; `send_message` is protocol-first with a UI fallback,
  selectable via `FB_MESSAGE_TRANSPORT`.
- `transport/lightspeed.py`: encoder/decoder for Messenger's sync frames. The
  wire format was recovered from a live send and is verified byte-for-byte by
  `tests/test_lightspeed.py` (16-byte header + JSON task body).
- `tools/lightspeed_probe.py`: talks to the gateway with nothing but the
  account's cookies, for further protocol work (`--dry-run` prints the frame).
- `tools/sniff.py --websocket`: records WebSocket URLs and binary frames, because
  Messenger sends messages over a WS that never appears as an HTTP request.
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

### Changed

- **Search no longer needs a browser after the first bootstrap.** The CDP hint
  was keyed on where the cookies came from (`from_config`), so a session
  bootstrapped from Chrome sent every authenticated search back through CDP. The
  rule is now "does the session hold cookies": if it does, curl_cffi serves the
  request, and CDP is only used when there are none.
- **Bootstrapped cookies are persisted.** They are written to the account store
  (0600, `source: cdp-bootstrap`), so a single attached Chrome is enough for good;
  later runs are pure HTTP (`FB_PERSIST_BROWSER_COOKIES` opts out). When Facebook
  rejects such a jar, `invalidate()` deletes it so the next call re-bootstraps
  instead of reloading a dead cookie at the next start. A user-managed
  `FB_COOKIE`/`FB_COOKIE_FILE` is never touched.

### Fixed

- **A wrong conclusion about the protocol was corrected.** This changelog and
  both architecture documents claimed a send must be preceded by thread-context
  fetches, based on a single thread where a bare send was refused and a warm-up
  made it work. A control experiment (same thread type, no warm-up) was accepted,
  so the requirement is thread-specific, not general. Warm-up is now opt-in and
  the documentation states the ambiguity of `Couldn't send`, which is also what a
  page with messaging disabled returns.
- **`Couldn't send` is now treated as ambiguous rather than diagnostic.** The
  message and the docs no longer assert that a refusal means missing warm-up; they
  list the three known causes and let `auto` mode use the UI path to tell them
  apart (safe: the server already said nothing was sent).
- **A replayed send could have been reported as `not_sent` after delivery.** The
  gateway nests its verdict in a JSON string, so the acceptance marker arrives
  with escaped quotes; matching raw text missed it while the message had in fact
  been sent. `parse_ack` normalises the escapes first.
- **The architecture notes had the send path wrong.** They claimed the send is
  issued by a service worker and that Lightspeed frames carry no readable body.
  Per-session capture shows the frame on the **page** session and the text in
  `payload.tasks[0].payload.text`; both documents now carry the corrected,
  byte-verified frame layout.
- **Multi-line messages could hide a failed send.** The rejection needle
  collapsed whitespace while the bubble's `innerText` kept its line breaks, so
  sending "Hello\nExample User" never matched its own bubble and a "Couldn't
  send" label was reported as `submitted`. Both sides are collapsed now, and the
  same normalisation is used for the "did the composer accept the text" check.
- **An unreadable composer passed recipient verification.** `_assert_composer_target`
  only failed on a *mismatch*, so a label that could no longer be read counted as
  verified. With a known recipient it now refuses. The Enter fallback also checks
  that the composer still exists first — previously a vanished box let three key
  events land on whatever had focus.
- **`search_pages` returned nothing.** Facebook types page results as
  `__typename="User"` (measured live: every result of `search/pages` is a
  `User` with no category fields), so the "never report a User as a Page"
  heuristic classified them as people and the page search filtered them all out.
  The tab is the authority again; only an explicit `Page`/`PageProfile` overrides
  it, which still keeps a page out of a people search.
- **A message sent after the click could be reported `not_sent`.** Any
  `FacebookError` raised after the send was clicked (a lost CDP reply, for
  instance) was mapped to `not_sent` — "safe to retry" — which invites a
  duplicate. Post-click failures are now `SendUncertainError` → `unknown`.
- **Ordinary message text could trigger "rejected".** Failure detection searched
  article bodies for "Couldn't send", so sending "fixed the Couldn't send issue"
  counted as a failed send (and `rejected` claims a retry is safe — a duplicate
  risk in the other direction). Only leaf nodes whose whole text equals a marker,
  inside the bubble holding our text, are counted now.
- **Recipient checks could still mis-deliver.** `_same_name` accepted any prefix,
  so "Example User" matched "Example User Two"; prefix matching now requires an
  explicit truncation marker. The composer is also re-verified immediately before
  the send click, not only before typing, so the pacing wait is no longer a window
  for a docked chat to steal the box.
- **A foreign account could borrow the default browser.** `build_settings()`
  dropped the global cookies for another account but kept the global `cdp_url`,
  so `auto` mode bootstrapped that browser's credentials for the wrong account.
  A foreign account now has no browser unless its own `account.json` binds one,
  and an unbound account fails with an actionable message.
- **CDP auth failures bypassed session invalidation.** The transport raises
  `AuthError` inside `request()`, so `BaseClient`'s post-return status check never
  ran and `session.rejected` stayed empty while every later call repeated the 403.
- **Anonymous retries could carry a cookie.** The anonymous jar was cleared once
  before the retry loop; a retryable response that set a cookie leaked it into the
  retry. The session is now resolved (and cleared) per attempt.
- **Packet capture could run forever.** `drain_events()` waited for an idle gap
  only, so a continuous event stream kept `sniff --seconds N` alive past its
  budget. It now honours an absolute deadline.
- **`export_cookies --home` chose the output directory but not the browser.** The
  account lookup used `FB_HOME`, so a custom root could export the wrong account's
  jar. `build_settings(account, home=...)` now carries the override (`sniff` got
  `--home` too).
- **The layering checker missed `__init__.py`.** Package modules were treated as
  ordinary modules when resolving relative imports, so an upward import injected
  into `core/__init__.py` still reported zero violations.
- **The document-sync checker ignored unnumbered headings.** It split on every
  heading and then dropped the unnumbered ones, so a whole section could move
  between parents (the real drift between §3 and §4) or disappear unnoticed. It
  now compares the full outline and counts unnumbered bodies with their parent.

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
- **The launcher required Chrome even for `--dry-run` and `--stop`.** The binary
  was looked up at a hardcoded macOS path before argument handling, so the CI
  runner (no Chrome) failed the dry-run tests. The lookup now happens only before
  an actual launch, honours `FB_CHROME`, and probes the usual Linux paths;
  `--dry-run`/`--stop` work without a browser. Regex escaping for `--stop` now
  uses `re.escape` instead of a sed character class, which is not portable across
  BSD and GNU sed.
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

- `account="work"` inherited the global `FB_COOKIE` / `FB_COOKIE_FILE` from
  `FB_ACCOUNT`, and a cookie file outranks the account store — a cross-account
  call could act as the other identity. Global credentials are now dropped for
  any other account, with a warning.
- `send_message` typed into "the first composer labelled `Write to …`", which is
  also what docked chat popups render, and matched thread rows with a substring
  id. It now ignores docked chats, requires the exact thread id, and refuses to
  compose unless the thread id equals the recipient uid or the composer's
  `Write to <name>` label matches the recipient's display name. The label is
  re-read before clicking send.
- `SendResult` claimed success when the composer merely cleared. It now carries
  `delivery`: `not_sent` (safe to retry), `submitted` (handed over; a retry may
  duplicate) or `unknown` (send clicked, outcome unobservable, raised as
  `SendUncertainError`).
- `search_pages` returned people: `_category_for()` fell back to the tab's
  category for `__typename="User"`, so a person surfaced in the pages tab was
  labelled PAGES. Users now count as pages only when they carry category
  metadata.
- A rejected session was never invalidated: an expired jar counted as logged in
  and every call replayed it. `SessionManager.invalidate()` drops the CSRF
  tokens and, for browser-backed sessions, the cookies so the next call
  re-bootstraps; an exported jar fails fast with "re-export and restart".
- `min_request_interval` only applied to curl_cffi, so CDP-executed GraphQL and
  the `send_message` UI steps bypassed it. A per-account `RateLimiter` is now
  shared by HTTP, CDP and UI operations.
- `get_user_latest_post_time` returned `ok=True` with a `null` time when no post
  carried a parsed `creation_time`; it now fails and says the payload shape
  probably changed.
- Documentation reconciled with the code: the doc_id "bootstrap default" rule is
  identical in CONTRIBUTING and §8.6, "only `routes/` changes" is scoped to
  interface parameters/templates (DOM selectors live in the client), §9.1
  documents the `BrowserTransport` capability protocol instead of claiming
  `Transport` is the only contract, and `make check` is described as the main
  local checks rather than "equivalent to CI".

- `ARCHITECTURE.md` / `ARCHITECTURE.en.md` audited again: the English §6.3 lost the
  conversation-verification and `delivery` steps when an earlier edit batch was
  applied to one side only, the exception tree now says `CdpError` is defined in
  `transport/cdp.py` (the only one outside `core`), "the first two are global
  credentials" no longer miscounts the per-account store, and only `unknown`
  (not `submitted`) is described as coming from `SendUncertainError`.
- The `get_user_latest_post_time` vs `get_user_recent_posts` asymmetry (structured
  `ok=False` vs raising) is now explained instead of only tabulated, and §3 has a
  dependency-injection diagram showing how `services/factory.py` wires the graph.
- `CONTRIBUTING.md` dropped the deploy-key tutorial: it is maintainer/machine
  setup, not a contributor rule; README keeps the one fact that matters (CI uses
  `GITHUB_TOKEN` and needs no deploy key).

- `tools/check_docs.py` (`make docs`, also in `make check` and CI) asserts that
  `ARCHITECTURE.md` and `ARCHITECTURE.en.md` keep the same section numbering,
  bullet count, table rows and fenced-code shape. It exists because a one-sided
  edit landed twice; `tests/test_check_docs.py` proves it fails on exactly that.

- `delivery="rejected"`: a live test against a real page showed Facebook clearing
  the composer and *then* stamping the bubble "Couldn't send" — so
  `send_message` reported `ok=True/submitted` for a message that never existed
  in the thread (the same bubble's accessibility label even says "Message
  sent"). Failed bubbles are now counted before and after the click and polled
  for up to 4s; only a *new* marker counts, so an older failure cannot taint a
  later send. `SendRejectedError` carries the thread id and maps to
  `delivery="rejected"` (`ok=False`) — safe to retry, though it will probably
  fail the same way. The marker is localised UI text (en/zh/es), so the check is
  best-effort and documented as such.

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
