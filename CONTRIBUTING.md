# Contributing

Thanks for taking the time to contribute.

## Ground rules

1. **Never commit credentials.** Cookies live in `~/.facebook-mcp/accounts/`
   (mode `0600`), never in the repo. CI fails if `cookies.txt`,
   `account.json` or `.env` are tracked and `.gitignore` already excludes them.
2. **Keep the layering intact.** `core → transport → session/routes → clients →
   services → server`, imports only point downward.
   `make layers` enforces this (0 upward deps, 0 cycles).
3. **Tests must stay offline.** No test may touch Facebook or a real Chrome.
   Use the local `http.server` and the mock DevTools server from
   `tests/test_m4_cdp_integration.py`.
4. **Don't weaken anti-abuse behaviour.** Rate limiting, request throttling and
   the "authorized accounts only" scope are not negotiable.

## Setup

```bash
make install     # .venv (Python 3.11) + dev dependencies
make hooks       # installs the commit-msg hook and the commit template
make check       # lint + format + layers + tests + MCP smoke (what CI runs)
```

## Documentation privacy

Examples in docs, tests, fixtures and code comments must never contain real
people, pages or identifiers. Use the neutral placeholders:

| Placeholder | Stands for |
|---|---|
| `exampleuser` | a profile / page username |
| `Example Page` | a display name |
| `100000000000001` | a profile or page uid |
| `200000000000001` | a Messenger thread id |
| `pfbid0AAA` | a post permalink suffix |
| `scontent.example` | a CDN host |

When you verify something against the live site, report the **outcome**
("3 results, first was a pest-control company"), not the identifiers you saw.
Screenshots and captured HTML belong in `.cache/` (gitignored) and should be
deleted before you finish.

## Documentation

`ARCHITECTURE.md` (Chinese) and `ARCHITECTURE.en.md` (English) describe the same
system and must stay in sync: a change that alters architecture, measured
behaviour or tooling belongs in **both** files. They share the same section
numbering and heading structure — keep it that way so readers can switch
languages at the same place.

The code blocks, tables and paths inside them are generated from the real
implementation, so update them when the implementation changes rather than
rewriting prose around stale details.

## Deploy keys

CI does not need a deploy key for this repository — `actions/checkout` uses the
built-in `GITHUB_TOKEN`. A deploy key is only needed when a workflow must reach
**another** repository, or when an external service has to clone this one.

This project's key convention:

```
~/.ssh/github-action-key-facebook-mcp        # private, 0600, ed25519, no passphrase
~/.ssh/github-action-key-facebook-mcp.pub    # public, paste into GitHub
```

Generate one per machine:

```bash
ssh-keygen -t ed25519 -a 100 -C "github-action-key-facebook-mcp" \
    -f ~/.ssh/github-action-key-facebook-mcp -N ""
chmod 600 ~/.ssh/github-action-key-facebook-mcp
```

Add the public key under **Settings → Deploy keys** (tick *Allow write access*
only if the consumer must push). With the `gh` CLI:

```bash
gh repo deploy-key add ~/.ssh/github-action-key-facebook-mcp.pub \
    --title "github-action-key-facebook-mcp"
```

Store the private key as a repository secret without echoing it:

```bash
gh secret set SSH_DEPLOY_KEY < ~/.ssh/github-action-key-facebook-mcp
```

Rules:

- always **ed25519**, never RSA;
- no passphrase (a CI runner cannot type one) — so treat the private key as a
  credential: `0600`, never in the repo, never in a log or screenshot;
- one key per machine, so a compromised runner can be revoked on its own;
- a deploy key is bound to a single repository; needing several means a GitHub
  App or a separate key per repo.

## Commit messages

The project uses [Conventional Commits](https://www.conventionalcommits.org/).
`tools/commit_msg.py` validates the format; `make hooks` wires it into
`commit-msg`, so a bad message is rejected locally instead of in CI.

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Types

| Type | Use for |
|---|---|
| `feat` | a new capability (tool, transport, client) |
| `fix` | a bug fix |
| `docs` | documentation only |
| `test` | tests only |
| `refactor` | behaviour-preserving restructuring |
| `perf` | performance work |
| `build` | packaging, dependencies, tooling |
| `ci` | CI configuration |
| `chore` | anything else that is not product code |
| `revert` | reverting a previous commit |

### Scopes

`core` `transport` `session` `routes` `clients` `services` `server`
`accounts` `cdp` `http` `proxy` `profile` `post` `search` `message`
`tools` `tests` `ci` `docs` `deps` `release`

A scope is optional; prefer the most specific one.

### Subject rules

- imperative mood: "add", not "added"/"adds"
- lowercase first ASCII letter (CJK subjects are fine as-is)
- no trailing period
- header at most **72** characters, subject at most **60**
- describe the effect, not the files touched

### Examples

```
feat(search): paginate results with page_info.end_cursor

SearchClient already parsed page_info; wire it into a public next_cursor()
so callers can request the following page without re-querying page 0.

Refs: #12
```

```
fix(accounts): keep cookies out of the repository

Cookies were written to .cache/ inside the working tree, which made it easy
to commit them by accident. Move the credential store to
~/.facebook-mcp/accounts/<name>/ with 0700/0600 permissions.

BREAKING CHANGE: FB_COOKIE_FILE now defaults to the account store; set it
explicitly to keep a custom path.
```

```
docs: explain why send_message needs a browser
```

Accepted as-is: `Merge ...`, `Revert ...`, `fixup! ...`, `squash! ...`.

### Validation

```bash
# one message
.venv/bin/python tools/commit_msg.py -m "feat(core): add account paths"

# a range (used by CI on pull requests)
.venv/bin/python tools/commit_msg.py --range origin/main..HEAD
```

## Pull requests

- One logical change per PR; keep unrelated refactors out.
- Update `CHANGELOG.md` under `## [Unreleased]` for user-visible changes.
- `make check` must pass.
- If you touched Facebook-facing behaviour, say **how you verified it**
  (e.g. "ran `tools/probe.py search pages 'pest control'`, got 3 results").
  Do not paste cookies, tokens or `c_user` values.

## Changing Facebook-facing code

Facebook changes its frontend regularly. When a query breaks:

1. Re-capture with `tools/sniff.py --navigate <url> --save-templates`.
2. That writes the fresh `doc_id`s into the account store and promotes
   `routes/templates/search_results.json` when the `variables` shape changed.
   Do **not** hardcode a rotated `doc_id` in `routes/graphql.py`: the registry
   value is only a bootstrap default (`BaseClient._doc_id()` prefers the
   account store), so refreshing it must not produce a commit.
3. Keep clients and services untouched — that separation is the point.

See `ARCHITECTURE.md` §8.6.

## License

By contributing you agree that your work is licensed under the [MIT License](./LICENSE).
