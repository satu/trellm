# Browser stack notes & disambiguation alternatives

This document captures the investigation behind the trellm browser stack and
proposes two alternative ways to unblock multi-Chrome scenarios on the same
host.

## How the current stack works

trellm's `scripts/start-browser.sh` is a near-clone of humphrey's. It runs four
processes on the trellm host:

1. **Xvfb** on display `:99` (host-only abstract socket `@/tmp/.X11-unix/X99`).
2. **Chrome** as user `dariofreni`, with `--remote-debugging-port=9222` and
   `--user-data-dir=$HOME/.chrome-trellm`.
3. **x11vnc** exposing `:99` on TCP `5900`.
4. **noVNC** (`websockify`) bridging `5900 → 6080`.

The Claude CLI is wired up to the running Chrome via the Web-Store
**`claude-in-chrome` extension** (ID `fcoeoabgfenejglbffodgkkbkcdhcgfn`,
hard-coded by the extension's manifest `key`). Each `claude --chrome`
invocation:

- Reads `~/.config/google-chrome/NativeMessagingHosts/com.anthropic.claude_code_browser_extension.json`
- That JSON points to a wrapper script that execs `claude --chrome-native-host`
- The native host launched by Chrome's extension talks back to `claude --chrome`
  over the `bridge.claudeusercontent.com` WebSocket the extension declares in
  its CSP `connect-src`.

The bridge is per-Anthropic-account: every Chrome where the extension is signed
into the same Claude account becomes a candidate "browser" the CLI can drive.

## Why M5 hit "Multiple Chrome extensions connected"

Three Chromes on this host were signed into the same account at the same time:

| Chrome | User | Profile dir |
| --- | --- | --- |
| trellm host Chrome | `dariofreni` | `~/.chrome-trellm` |
| `humphrey` container Chrome | `humphrey` (UID inside container) | `/home/humphrey/.chrome-humphrey` |
| `nostalgic_goodall` container Chrome | (container UID) | (container path) |

Containers don't share the host's NativeMessagingHosts registration (each has
its own `~/.config/google-chrome/NativeMessagingHosts/`), so the conflict
isn't local-stdio. It's the **cloud bridge**: all three extensions register
with the same Anthropic identity, so `claude --chrome` running on any of them
sees three candidate browsers and refuses to dispatch.

The current MCP tool (`mcp__claude-in-chrome__switch_browser`) only broadcasts
a connection request — disambiguation is a manual "Connect" click in the
desired Chrome via VNC.

## The extension already supports naming

Decompiling `assets/PairingPrompt-Bqsp4vIU.js` from the installed extension
shows a literal prompt: *"Name this browser so you can identify it later."*
The pairing flow's URL params include `current_name`, and on confirm the
extension fires `{type: "pairing_confirmed", request_id, name}` back to the
service worker. Suggested defaults from the UI: "Personal Chrome", "Work
laptop", "Claude Code", "Claude Desktop".

So **the bridge already knows each browser by a user-set name.** What's
missing — at the trellm/Claude-Code MCP layer — is a way to *select* by name.
`switch_browser` takes no parameters and is broadcast-only. Until that gains a
`name`/`browserId` filter, naming alone doesn't auto-unblock multi-Chrome
selection without a human click.

## Alternative A — Account isolation (recommended)

Run the trellm Chrome under a **dedicated Anthropic account** that no other
Chrome on the box is signed into.

1. Provision a second Anthropic account (e.g. `trellm-bot@…`) on a Max/Pro
   seat.
2. Sign into the trellm-side Chrome (`~/.chrome-trellm`) only with that
   account. Sign humphrey/other containers into the original account.
3. trellm's `claude --chrome` resolves to the trellm-bot account → bridge
   shows exactly one candidate browser → no disambiguation prompt.

**Pros**

- Zero code change in trellm.
- Per-Chrome name in the extension is preserved and visible in VNC.
- humphrey keeps full browser functionality on the original account.
- Works today with the official Web-Store extension.

**Cons**

- Requires a second seat (or shared org).
- Extra login flow when first wiring up — VNC into trellm Chrome, sign in
  manually.
- Account drift: if someone signs the same account into another Chrome later
  (e.g. on a laptop) the conflict re-appears.

## Alternative B — Side-loaded fork with a distinct extension ID

Build a fork of `claude-in-chrome` packaged with:

- A regenerated `key` (new extension ID) installed only into
  `~/.chrome-trellm` via the existing symlink trick in `start-browser.sh`.
- A distinct `nativeMessagingHosts` host name (e.g.
  `com.trellm.claude_browser_extension`) registered only under
  `dariofreni`.
- Either bypass the cloud bridge (use a local Unix-socket transport between
  fork-extension and a custom `trellm-chrome-host` daemon) or accept the
  bridge but pin the fork to a separate account.

**Pros**

- Hard isolation — no chance of cross-Chrome bleed regardless of how many
  other Chromes are running.
- Doesn't depend on Anthropic shipping a `--browser-name` selector.

**Cons**

- Maintenance: forking the extension means tracking upstream `claude-in-chrome`
  updates manually; auto-updates from the Web Store don't apply.
- Custom native-host code to write and harden.
- The Claude Code CLI's `--chrome` flag expects the Web Store extension ID;
  the fork would need a Claude Code feature to take a custom extension ID, or
  trellm would have to ship its own `claude --chrome` shim. Both are
  non-trivial.

## Recommendation

**Alternative A** is the cheapest unblock and doesn't depend on changes
outside trellm. Document that the trellm Chrome must be signed into a
dedicated Anthropic account, fail loudly in `start-browser.sh` if more than
one Chrome on the host is reachable on the same account, and revisit
Alternative B only if Anthropic doesn't add named-browser selection to the
MCP within a reasonable horizon.
