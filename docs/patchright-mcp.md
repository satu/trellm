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

## 6. Smoke test

Mirror the M5 milestone from the (now-archived) claude-in-chrome
re-attempt card.

**File a follow-up card** with:

> *trellm patchright-mcp M-smoke fetch example.com title via patchright
> from a trellm-spawned Claude*
>
> ## Pre-req
> - `scripts/start-browser.sh` resurrected (sans extension symlink) and
>   `--chrome-trellm` profile in place.
> - `claude.projects.trellm.browser.enabled: true` in `~/.trellm/config.yaml`.
> - patchright-mcp-lite built (`npm install && npm run build` in
>   `~/src/patchright-mcp-lite`) and CDP-reachable on `localhost:9222`.
>
> ## Card body
> Browse to `https://example.com` using the patchright MCP, return the
> page title.
>
> ## Pass criteria
> - The Claude subprocess is invoked with `--mcp-config` referencing
>   patchright (verify in the live-output stream that the MCP is
>   listed).
> - Claude calls `mcp__patchright__browse` (verify in the stream).
> - The card comment contains the literal string `Example Domain`.
> - No claude-in-chrome bridge errors in the stream (we never go near
>   the bridge).
>
> ## Stop gate
> If any of the pre-reqs is wrong or the smoke test fails, **stop and
> re-investigate** — do not promote browser-enabled to other projects.

This card slots into READY TO TRY immediately after the wiring card it
depends on. Suggested split:

1. **M1 — browser stack**: resurrect `scripts/start-browser.sh` /
   `setup-browser.sh` minus the extension symlink; add static tests.
2. **M2 — MCP wiring**: add `BrowserConfig` + `Config.is_browser_enabled`
   + `--mcp-config` plumbing in `ClaudeRunner.run` /
   `_run_compact` / `_run_cost` / `maintenance.run`, with unit tests.
3. **M3 — auto-start**: have `start-trellm.sh` invoke
   `scripts/start-browser.sh start` when `claude.browser.enabled` (global
   or any project) is true.
4. **M4 — smoke test card** (the one above).

## Out of scope

- Migrating non-browser projects to require patchright. Browser is
  opt-in per project — only projects that explicitly need it should
  toggle it on.
- Replacing humphrey's claude-in-chrome stack. humphrey lives inside its
  container with its own Chrome, and the cloud bridge works fine when
  it's the only client.
- Any second-Anthropic-account work or a forked claude-in-chrome —
  patchright-mcp obsoletes both.
