# patchright-mcp as the trellm browser interface — investigation

This is the answer to the 6 scope questions on the
[patchright-mcp consider card](https://trello.com/c/n94Csprd). It is a
decision document, not an implementation plan — implementation will come in
follow-up cards (see "Smoke test" below for the first one).

## TL;DR

- **Use it.** patchright-mcp-lite cleanly replaces the reverted
  `claude-in-chrome` route. There is no cloud bridge, so the multi-Chrome
  account-multiplexing failure that killed the Web-Store extension cannot
  recur.
- **Lifecycle is split**: one long-lived headed Chrome on the trellm host,
  one stdio MCP server *per Claude subprocess*. Both are cheap.
- **Wiring is trellm-managed `--mcp-config`** — opt-in per project via
  `claude.projects.<name>.browser: true`, mirroring the reverted
  `browser_enabled` plumbing in [c09e9c7](https://github.com/satu/trellm/commit/c09e9c7).
- **Headed Chrome under Xvfb** (resurrect the reverted browser-stack scripts
  but drop the `claude-in-chrome` extension symlink — patchright-mcp-lite
  doesn't need the extension).
- **Profile dir persists cookies**. Sites that need login are handled by a
  one-time human-in-the-loop VNC sign-in.
- **Keep patchright** (vs vanilla Playwright) — the stealth patches are a
  free win.

## Architectural correction to the card description

The card states:

> Each MCP server instance owns its own Chromium process, so trellm and
> humphrey browse in lanes that physically can't collide.

**This is not how the local fork works.** `~/src/patchright-mcp-lite/src/index.ts`
does `chromium.connectOverCDP("http://localhost:9222")` — it attaches to a
pre-existing headed Chrome that we bring ourselves. It does not launch
Chromium.

This doesn't change the recommendation, but it does change the isolation
story:

- humphrey runs its own Chrome inside its container (separate UID,
  separate filesystem, container network). Cannot collide.
- trellm runs its own Chrome on the host (separate user-data-dir, separate
  CDP port if humphrey ever moved back to host). Cannot collide.
- The "physical lanes" guarantee comes from **Chrome instance separation**,
  not from MCP-spawn-per-Chromium.

This is good — it means we can keep the `~/.chrome-trellm` profile dir
pattern from the reverted browser-stack and get cookie persistence for
free.

## 1. Lifecycle

| Component       | Lifetime                              | Why                                                                                                                                                                 |
|-----------------|---------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Xvfb + Chrome   | Long-lived (started by start-trellm.sh) | Browser cold-start is 2–3 s — too slow per-card. Chrome is also where cookies live; killing it loses login state.                                                  |
| x11vnc + noVNC  | Long-lived                            | Observability for the human; cheap.                                                                                                                                 |
| patchright MCP server | **Ephemeral, per Claude subprocess** | claude spawns its MCP servers when it starts and reaps them on exit. Node startup + CDP attach is ~300 ms; not worth pooling. Matches the standard MCP stdio model. |

Memory cost: one Chrome (~250 MB idle) + one Node MCP process (~50 MB)
*per concurrently-running browser-using card*. trellm currently runs cards
sequentially, so practically there is at most one MCP process at a time.

If parallel cards both use the browser, they share the *same* Chrome via
CDP. patchright-mcp-lite's connection manager has a shared-context pattern
(`browser.contexts()[0]`) — pages from different MCP processes will live
in the same browser context and *can see each other's cookies*. This is
fine for trellm-managed projects (same trust boundary). It would be a
problem if we ever ran a hostile site, but that's not the use case.

## 2. Wiring

Three options:

| Option                                | Pros                                                                 | Cons                                                                                                                                |
|---------------------------------------|----------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------|
| (a) trellm-managed `--mcp-config`     | Opt-in per project; no managed-repo pollution; trellm controls toggle | trellm has to write/maintain the JSON                                                                                                |
| (b) Per-repo `.mcp.json`              | Standard Claude Code pattern; works without trellm                   | Every browser-using repo carries machine-specific absolute paths in the JSON; no central toggle                                      |
| (c) User-level `~/.claude.json`       | Dead simple, write once                                              | Leaks into humphrey, the user's own interactive sessions, every other repo                                                          |

**Recommendation: (a)**. It mirrors the existing
`Config.is_browser_enabled(project)` / `BrowserConfig` pattern from the
reverted [c09e9c7](https://github.com/satu/trellm/commit/c09e9c7), so we
re-use a known shape:

```yaml
# ~/.trellm/config.yaml
claude:
  browser:
    enabled: false  # global default
  projects:
    mbspending:
      working_dir: "~/src/mbspending"
      browser:
        enabled: true  # opt-in
```

Implementation sketch (for the follow-up card, not this one):

```python
# trellm/claude.py
async def _run_once(..., browser_enabled: bool):
    cmd = [self.binary, "-p", prompt, "--output-format", ...]
    if browser_enabled:
        cmd.extend(["--mcp-config", _patchright_mcp_config_json()])
    ...

def _patchright_mcp_config_json() -> str:
    # Inline JSON; --mcp-config accepts JSON strings as well as file paths.
    return json.dumps({
        "mcpServers": {
            "patchright": {
                "command": "node",
                "args": [str(Path("~/src/patchright-mcp-lite/dist/index.js").expanduser())],
                "env": {
                    "CDP_ENDPOINT": "http://localhost:9222",
                    "BROWSER_RESTART_CMD": str(Path(__file__).parent.parent / "scripts" / "start-browser.sh") + " start",
                }
            }
        }
    })
```

The path to `patchright-mcp-lite` is absolute and machine-specific, so it
should be configurable (e.g.
`claude.browser.patchright_path: "~/src/patchright-mcp-lite/dist/index.js"`).
This is fine — trellm config is per-host already.

## 3. Headless vs headed

The local fork is locked into CDP-attach mode against an external Chrome,
so the choice is really "headed Chrome under Xvfb" vs "headless Chrome".
The fork's `--remote-debugging-port` Chrome runs in whatever mode
`start-browser.sh` launches it in. Both are technically supported by
patchright.

**Recommendation: headed Chrome under Xvfb**, re-using
[484e6c3](https://github.com/satu/trellm/commit/484e6c3)'s
`scripts/start-browser.sh` and `scripts/setup-browser.sh` *minus* the
claude-in-chrome extension symlink (we don't need it any more).

Reasons:

- VNC observability is the difference between "browser hung, retry?" and
  "the site is showing a CAPTCHA". The reverted setup had this for free
  via `:6080/vnc.html`.
- Cookie persistence in `~/.chrome-trellm` works in both modes, but headed
  is what we know works (humphrey runs this way).
- Anti-bot detection of headless Chrome is more aggressive than of headed
  Chrome. patchright's stealth patches help, but headed-under-Xvfb is
  still the safer default.

The trade-off is ~250 MB of resident memory. Acceptable for trellm.

What changes vs the reverted `start-browser.sh`:

- Drop the `EXTENSION_ID` constant and the
  `~/.config/google-chrome/Default/Extensions/$EXTENSION_ID` symlink. Not
  needed any more.
- Optional: drop `--no-sandbox` if we're not running under root (we
  aren't; `dariofreni` user). Was needed because Chrome+Xvfb sometimes
  bails on sandbox under headless display init; verify in the M1
  follow-up.

## 4. Auth / cookies

`patchright-mcp-lite` reuses Chrome's default browser context
(`browser.contexts()[0]`), which means every page launched by the MCP
shares the cookies of `~/.chrome-trellm`'s default profile.

**Recommendation: persistent profile dir + one-time VNC sign-in.**

Workflow:

1. Once, after setup: VNC into the trellm Chrome, sign into whichever
   site (Trello, GitHub, GitHub gists, …) the agent will need.
2. Cookies persist in `~/.chrome-trellm/Default/Cookies`. Survives
   browser-stack restarts.
3. If a site logs the agent out, the next browse fails visibly (auth
   redirect); a human re-signs via VNC.

We do **not** want to script credentials through the MCP — `interact`'s
`fill` action would put passwords through the Claude conversation,
violating the chat-side privacy rules. Manual VNC login is the right
boundary.

For sites that need an OTP / WebAuthn / passwordless flow, the same
manual VNC interaction handles it.

## 5. Stealth value

patchright is a maintained drop-in for Playwright with patches against
common anti-bot fingerprints (Runtime.enable leak, console.enable leak,
command-line flag leaks, closed shadow-root interaction). Because our
fork uses `chromium.connectOverCDP`, **the patches apply** to the
attached browser session.

**Recommendation: keep patchright** (vs swapping to vanilla Playwright).
There is no extra cost — we already have it cloned, it's a drop-in API,
and the upstream Patchright SDK is actively maintained
(`patchright@^1.52.1` in `package.json`). Monitor upstream for breakage
on Chrome auto-updates; if Patchright stops keeping up, revisit.

## 6. Milestone breakdown

Each milestone is a separate follow-up ticket, sequenced strictly. Each
milestone has a hard stop gate — if its verification fails, the next
milestone does not begin until the failure is understood and the plan
is corrected.

A general rule across all milestones: **one milestone = one ticket =
one commit (or a tight sequence on a focused branch)**. Don't bundle
scope across milestones. If you discover work that belongs to a later
milestone while in an earlier one, write it down and defer.

### M1 — Browser stack (host-side Chrome + Xvfb + VNC)

> *trellm patchright-mcp M1 resurrect browser-stack scripts (sans
> claude-in-chrome extension)*

**Scope.** Files that may change:

- `scripts/start-browser.sh` (resurrected from
  [484e6c3](https://github.com/satu/trellm/commit/484e6c3))
- `scripts/setup-browser.sh` (resurrected from the same commit)
- `tests/test_browser_scripts.py` (new, or a shell-level smoke check;
  see Verification below)

Nothing in `trellm/` changes in M1. No Python code. No `--mcp-config`
plumbing yet. **Do not** install or symlink any browser extension —
the whole reason patchright-mcp exists is to avoid that path.

**Implementation steps.**

1. `git show 484e6c3:scripts/start-browser.sh > scripts/start-browser.sh`,
   `git show 484e6c3:scripts/setup-browser.sh > scripts/setup-browser.sh`,
   `chmod +x` both.
2. Delete the `EXTENSION_ID` constant and the
   `~/.config/google-chrome/Default/Extensions/$EXTENSION_ID` symlink
   block from `start-browser.sh`.
3. Delete any extension-install bits from `setup-browser.sh`.
4. Verify whether `--no-sandbox` is still needed. We run as
   `dariofreni`, not root, so it likely isn't — drop it and let
   Verification step 4 confirm. If Chrome refuses to start, restore the
   flag and document why in a one-line comment.
5. Add a tiny test that asserts the scripts are syntactically valid
   (`bash -n`) and that `start-browser.sh` defines the expected
   functions/commands (`start`, `stop`, `status`). This catches
   regressions if the script is edited later.

**Verification steps.**

1. `bash -n scripts/start-browser.sh && bash -n scripts/setup-browser.sh`
   exits 0 (syntax).
2. The test from step 5 above passes under `pytest`.
3. `scripts/setup-browser.sh` runs on a fresh shell without errors
   (idempotent: running it twice in a row works).
4. `scripts/start-browser.sh start` returns 0 and Chrome binds CDP on
   9222: `curl -s http://localhost:9222/json/version` returns JSON with
   a `Browser` field.
5. `scripts/start-browser.sh status` reports a running PID.
6. `scripts/start-browser.sh stop` releases 9222: `curl
   http://localhost:9222/json/version` fails with connection refused.
7. Idempotency: `start ; start` second invocation says "already
   running" and exits 0 (does not double-launch Chrome).
8. Profile dir `~/.chrome-trellm/Default/` exists after first start; a
   manually-written cookie in `Default/Cookies` survives `stop` →
   `start`.
9. VNC reachable: `curl -s -o /dev/null -w "%{http_code}"
   http://localhost:6080/vnc.html` returns `200`.
10. **Negative check**: no `claude-in-chrome` extension anywhere under
    `~/.config/google-chrome/Default/Extensions/` (`ls` shows nothing
    matching `fcoeoabgfenejglbffodgkkbkcdhcgfn`).

**Stop gate.** Any verification step above fails → fix in M1, do not
start M2. If steps 4–7 fail in a way that suggests Chrome-version drift
(profile incompatibility, Xvfb quirks, etc.), capture the symptom in a
card comment before retrying.

**Out of scope for M1.** Any change to `trellm/` Python. Any
`--mcp-config` work. `start-trellm.sh` auto-start (that's M3).
patchright-mcp-lite build/install (assume it's already built; if not,
note as a one-line pre-req in the card, not a step).

---

### M2 — MCP wiring (`--mcp-config` plumbed through `ClaudeRunner`)

> *trellm patchright-mcp M2 add BrowserConfig + Config.is_browser_enabled
> + --mcp-config plumbing*

**Scope.** Files that may change:

- `trellm/config.py` (add `BrowserConfig` dataclass, the
  `is_browser_enabled` accessor, and the JSON-config helper)
- `trellm/claude.py` (add `browser_enabled` param to `run`,
  `_run_once`, `_run_compact`, `_run_cost`; append `--mcp-config`
  when true)
- `trellm/maintenance.py` (thread the same flag for the maintenance
  invocation)
- `trellm/__main__.py` and `trellm/web/server.py` (compute
  `browser_enabled = config.is_browser_enabled(project)` at call sites
  and pass it down)
- `tests/test_config.py`, `tests/test_claude.py`,
  `tests/test_maintenance.py` (red-then-green coverage for every
  branch)

Mirrors the reverted
[c09e9c7](https://github.com/satu/trellm/commit/c09e9c7) — but plumbs
`--mcp-config <json>` instead of the old `--chrome` flag.

**Implementation steps (in TDD order).**

1. Write failing `test_config.py`: YAML with
   `claude.projects.foo.browser.enabled: true` →
   `Config.is_browser_enabled("foo")` returns `True`; YAML without →
   `False`. Add the `BrowserConfig` dataclass and accessor; tests go
   green.
2. Write failing `test_config.py`: `Config.patchright_mcp_config_json()`
   returns a JSON string parseable by `json.loads`, with
   `mcpServers.patchright.command == "node"` and the args path
   resolving under `~/src/patchright-mcp-lite/dist/index.js` (or a
   configurable override). Implement; tests go green.
3. Write failing `test_claude.py`: stub `asyncio.create_subprocess_exec`
   and assert that `ClaudeRunner.run(..., browser_enabled=True)`
   includes `--mcp-config` in the command; `browser_enabled=False` does
   not. Implement; tests go green.
4. Same pattern for `_run_compact` and `_run_cost`.
5. Same pattern in `test_maintenance.py`: maintenance honours the
   project's `browser.enabled` flag.
6. Wire call sites in `__main__.py` and `web/server.py` to compute the
   flag from `Config.is_browser_enabled(project)` and pass it down.
   Add an integration-level test that asserts a card processed for a
   browser-enabled project results in a `--mcp-config`-bearing command
   (subprocess mocked).

**Verification steps.**

1. `pytest` is fully green. No skipped tests in the new files.
2. `pytest --cov=trellm tests/test_config.py tests/test_claude.py
   tests/test_maintenance.py` shows the new branches (`browser_enabled`
   true/false, JSON helper) are exercised — eyeball coverage on the
   added lines.
3. Manual smoke (does not require Chrome running): run `trellm --once`
   against a project with `browser.enabled: true` on a trivial card
   while subprocess execution is logged at DEBUG; the logged command
   contains `--mcp-config`. Same project with `enabled: false` →
   command does not.
4. **Negative check**: grep the repo for any leftover `--chrome` flag
   from the reverted experiment; none should exist. Grep for
   `claude-in-chrome`, `bridge.claudeusercontent.com`,
   `fcoeoabgfenejglbffodgkkbkcdhcgfn` — all should return zero hits in
   `trellm/` and `scripts/`.

**Stop gate.** Any failing test, or `--mcp-config` showing up when
`browser.enabled` is `false`, or any of the negative greps above
returning a hit → fix in M2, do not start M3.

**Out of scope for M2.** Starting Chrome (M3). Running the MCP server
end-to-end (M4). Editing `start-browser.sh` again. Adding browser
support to projects other than `trellm` (`mbspending` etc. opt in
later, individually).

---

### M3 — Auto-start the browser stack from `start-trellm.sh`

> *trellm patchright-mcp M3 auto-start browser stack from
> start-trellm.sh when any project has browser.enabled*

**Scope.** Files that may change:

- `start-trellm.sh` (the Docker entrypoint / direct launcher introduced
  in [4275aae](https://github.com/satu/trellm/commit/4275aae))
- A test (shell-level or Python) verifying the auto-start decision
  logic, isolated from actually starting Chrome.

Nothing in `trellm/` Python.

**Implementation steps.**

1. In `start-trellm.sh`, after the config sanity check, decide whether
   the browser stack is needed: parse `~/.trellm/config.yaml` and
   return true iff `claude.browser.enabled: true` globally **or** any
   `claude.projects.<name>.browser.enabled: true`. Use `yq` if already
   available, else a Python one-liner via the same venv trellm uses.
2. If needed, invoke `scripts/start-browser.sh start` (which is
   idempotent per M1). Fail the script with a clear error if Chrome
   doesn't come up on 9222 within 10 seconds — do not let trellm run
   browser-enabled cards against a dead browser.
3. **Do not** stop the browser on trellm exit. The browser is a
   long-lived dependency owned by the host (VNC users may still want
   it; subsequent trellm restarts should not pay the cold-start cost).
   Document this in a one-line comment.
4. Add a small test fixture that drives the decision logic with three
   YAML inputs (no browser, global on, one project on) and asserts the
   correct branch is taken — without actually invoking
   `scripts/start-browser.sh`.

**Verification steps.**

1. `bash -n start-trellm.sh` exits 0.
2. The decision-logic test passes.
3. With no project having `browser.enabled: true` and no global flag:
   `start-trellm.sh` runs without launching Chrome. `pgrep -f
   "remote-debugging-port=9222"` returns empty; trellm still polls
   normally.
4. With one project (e.g. `trellm`) flipped on: `start-trellm.sh`
   starts the browser stack first; `curl -s -o /dev/null -w "%{http_code}"
   http://localhost:9222/json/version` returns `200` within 10s; trellm
   starts after.
5. Idempotent: invoke `start-trellm.sh` a second time while Chrome is
   already up → no error, Chrome PID is unchanged.
6. Lifetime: stop trellm (Ctrl-C); Chrome stays alive; VNC still
   reachable; restart trellm and it reuses the running Chrome.
7. **Negative check**: a config with `enabled: true` *and* `setup-
   browser.sh` not yet run → `start-trellm.sh` fails loudly with a
   pointer to the setup command rather than starting trellm in a
   half-broken state.

**Stop gate.** Any of the above fails → fix in M3, do not start M4.

**Out of scope for M3.** Stopping Chrome on exit. Cookie management
(handled by the profile dir from M1). Anything in `trellm/`.

---

### M4 — End-to-end smoke test card

> *trellm patchright-mcp M4 smoke: fetch example.com title via
> patchright from a trellm-spawned Claude*

**Scope.** No code change in this milestone — it's a Trello card whose
*processing* is the test. The deliverable is a green run that proves
M1+M2+M3 are correctly composed.

**Pre-flight checklist (run before filing the card).**

- [ ] M1 verification 1–10 all green within the last week.
- [ ] M2 `pytest` fully green within the last week.
- [ ] M3 verification 3–7 all green within the last week.
- [ ] `claude.projects.trellm.browser.enabled: true` is set in
      `~/.trellm/config.yaml`.
- [ ] `npm install && npm run build` succeeded in
      `~/src/patchright-mcp-lite` and `dist/index.js` exists.
- [ ] `curl -s http://localhost:9222/json/version` returns 200 with a
      `Browser` field.

**Card body** (this is the literal body of the M4 follow-up card —
copy verbatim when filing):

> Browse to `https://example.com` using the patchright MCP and return
> the page title in a Trello comment on this card.

**Pass criteria.**

1. The Claude subprocess command (visible in the trellm live-output
   stream and DEBUG logs) contains `--mcp-config` referencing
   patchright-mcp-lite.
2. The MCP server initialisation line `patchright-lite` appears in the
   stream (i.e. claude actually loaded the server).
3. Claude calls `mcp__patchright__browse` (visible in the stream as a
   tool call).
4. The "Claude:" comment posted to the card contains the literal
   string `Example Domain`.
5. No mention of `bridge.claudeusercontent.com`,
   `claude-in-chrome`, or the reverted `--chrome` flag anywhere in the
   stream.

**Verification steps.**

1. All five pass criteria observed on a single run.
2. Re-run the card a second time (move it back to TODO); same result.
   Idempotency proves the long-lived Chrome and the per-subprocess MCP
   are both working as designed.
3. Spot-check `~/.chrome-trellm/Default/Cookies` exists and is
   non-empty after the run (proves the shared profile is in use, not a
   throwaway one).

**Stop gate.** Any pass criterion fails → **do not promote
`browser.enabled` to any other project**. File a follow-up
investigation card with the stream excerpt and the failing criterion.

**Out of scope for M4.** Promoting browser support to `mbspending`,
`smugcoin`, or any other project — those are their own opt-in cards,
filed only after M4 is green.

---

### Sequencing summary

```
M1 (browser stack)  ──►  M2 (MCP wiring)  ──►  M3 (auto-start)  ──►  M4 (smoke)
   stop gate            stop gate              stop gate             stop gate
```

Strictly sequential. Each gate must close before the next opens. M0
(the investigation in this doc) is already complete.

## Out of scope

- Migrating non-browser projects to require patchright. Browser is
  opt-in per project — only projects that explicitly need it should
  toggle it on.
- Replacing humphrey's claude-in-chrome stack. humphrey lives inside its
  container with its own Chrome, and the cloud bridge works fine when
  it's the only client.
- Any second-Anthropic-account work or a forked claude-in-chrome —
  patchright-mcp obsoletes both.
