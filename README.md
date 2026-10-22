# cross-agent MCP

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

**Let your Claude Code, Codex, Grok Build and Copilot sessions talk to each other, live, without losing their existing context.**

Copilot support targets **existing VS Code Agent Host sessions**, including sessions using custom
models. It connects to VS Code's authenticated local Agent Host Protocol (AHP) endpoint; the
model, tools and approvals remain managed by the existing host. Copilot does not use the CLI
fallback described below and requires an exact session name, UUID or Copilot session URI.

cross-agent MCP is a relay MCP server for the coding agents you already have running —
**Claude Code**, **Codex**, **Grok Build** and **Copilot**. Any one of them can hand a message to
another through `send_to_codex` / `send_to_claude` / `send_to_grok` / `send_to_copilot`, keeping
both sides' existing context. Claude, Codex and Grok work in VS Code or a plain terminal:
the bridge finds their **currently active session** from the transcript on disk and
**resumes it**. Their optional panel shims make the exchange render live in the real chat
panel, as covered in [section 3](#3-registration). Copilot requires VS Code and uses its
existing Agent Host session directly.

Use `send_to_copilot(session_id="copilot-test", message="...")` for an existing Copilot
conversation. `list_agent_sessions(agent="copilot")` includes its editor UUID, host URI,
working directory and selected model. This integration has been verified with VS Code
**1.141.0 / AHP 0.10.0** on macOS. Other AHP versions are refused until supported explicitly.

That's the difference from just running a second CLI by hand: neither side has to re-explain
the task, and neither one loses the conversation it was already having. A few things this is
useful for:

- **Get a second opinion without leaving your conversation.** Ask Codex to review or double-check
  Claude's plan, or the other way around, and keep working while it thinks.
- **Hand off a long task and keep going.** `send_to_*` is asynchronous — it queues the message
  and returns immediately. Whenever the peer's answer is ready, it arrives back as a new message
  in your own session — provided that session is open in a panel, or it is the Copilot caller's
  verified SDK session on its existing host. Otherwise the answer waits on the delivery record
  for `bridge_status`, and the receipt says so up front.
- **Watch it happen, not just read a log.** With the panel shims from
  [section 3](#3-registration) installed, Claude, Codex and Grok exchanges render in VS Code's
  real chat panels. Copilot uses the host protocol directly and needs no shim.
- **Long active turns keep running.** Headless turns use an inactivity timeout, reset by CLI
  output or target transcript writes. Accepted panel turns are watched until they finish.

### A real example

One user runs a single Claude session as a **master** that directs about a dozen role-based
sub-sessions — a Claude or Codex session per role (implementation/tooling, image generation,
client, server, copy, and a few more). The master only delegates work whose output would be too
large for its own context; it keeps verification for itself, re-checking a peer's claims against
the actual files or source lines rather than taking a self-report as done. Every delegation names
an explicit `session_id`, since a dozen sessions sharing one working directory makes
auto-selection unreliable, and handoffs between two Claude sessions use `allow_same_agent: true`.

In one run, the image-generation role (Codex) produced a request that the tooling role (Claude,
same-agent) refused to act on — a field its contract required was missing. The master traced the
fault to source, had the image role rewrite the request, verified the fix independently, and
re-dispatched — three hops between two roles, with the master gating each one before it reached
the human.

## Contents

- [1. Requirements](#1-requirements)
- [2. Installation](#2-installation)
- [3. Registration](#3-registration)
- [4. Tools](#4-tools)
- [5. Session resolution rules](#5-session-resolution-rules)
- [6. Preventing infinite calls](#6-preventing-infinite-calls)
- [7. Environment variables](#7-environment-variables)
- [8. Verification](#8-verification)
- [9. Known limitations](#9-known-limitations)
- [License](#license)

```
          VS Code or Terminal                 VS Code Agent Host
                   │                                  │
        ┌──────────┼──────────┐                       │
        │          │          │                       │
      Claude     Codex       Grok                   Copilot
     session A  thread B   session C                chat D
        │          │          │                       │
        └───────────── cross-agent MCP ────────────────┘
                              │
                      session registry
               (~/.cross-agent/registry.json)
```

Any of these can reach any other through the same hub — across products (Claude ↔ Codex ↔
Grok ↔ Copilot), or between sessions within the same product (see the
same-agent rows in the table below, and [section 6](#6-preventing-infinite-calls) for how
that's gated).

| Direction | Tool | Live editor transport | Without it (fallback) |
|---|---|---|---|
| Claude → Codex | `send_to_codex` | Inject `turn/start` into the panel's app-server | `codex exec resume <thread-id> --json` |
| Codex → Claude | `send_to_claude` | Inject a stream-json user message into the panel process | `claude -p --resume <session-id> --output-format json` |
| Claude → Grok, Codex → Grok | `send_to_grok` | Inject an ACP `session/prompt` into the panel's `grok agent stdio` | `grok --single=<message> --resume <session-id> --output-format streaming-json` |
| Grok → Claude | `send_to_claude` | Inject a stream-json user message into the panel process | `claude -p --resume <session-id> --output-format json` |
| Grok → Codex | `send_to_codex` | Inject `turn/start` into the panel's app-server | `codex exec resume <thread-id> --json` |
| Claude → Claude¹ | `send_to_claude` | Inject a stream-json user message into the panel process | `claude -p --resume <session-id> --output-format json` |
| Codex → Codex¹ | `send_to_codex` | Inject `turn/start` into the panel's app-server | `codex exec resume <thread-id> --json` |
| Grok → Grok¹ | `send_to_grok` | Inject an ACP `session/prompt` into the panel's `grok agent stdio` | `grok --single=<message> --resume <session-id> --output-format streaming-json` |
| Claude, Codex, Grok → Copilot | `send_to_copilot` | Authenticated AHP `chat/turnStarted` on an existing default chat; no shim | Refused; no CLI fallback |
| Copilot → Claude, Codex, Grok | Matching `send_to_claude` / `send_to_codex` / `send_to_grok` | The target's panel shim, as above | The target's CLI resume, as above |
| Copilot → Copilot¹ | `send_to_copilot` | Same existing-host AHP transport | Refused; no CLI fallback |

¹ Same-agent rows need an explicit target — `allow_same_agent=true` or a `session_id` for
Claude and Grok, a `session_id` for Codex. Copilot always needs `session_id` or a sticky pin;
using that pin for Copilot → Copilot also needs `allow_same_agent=true` — see
[section 6](#6-preventing-infinite-calls).

With the shim attached, the exchange **renders directly in the real VS Code panel** (see section 3).

The exchange is **asynchronous**. `send_to_*` queues the message and returns immediately — it
does not carry the peer's reply. A background worker runs the peer's turn, and once a reply
exists, it is delivered as a new message through the sender's live return route (see
[Return traffic needs a panel](#return-traffic-needs-a-panel), including Copilot's verified
SDK route). Because nothing blocks, neither session is locked while the peer's turn runs,
and a turn that takes several minutes won't be lost to a timeout.

```
send_to_codex ──▶ [outbox queue] ──▶ Codex turn (minutes)
     │                                   │
returns immediately                answer generated
  (delivery_id)                         │
                                        ▼
                  delivered to the Claude session as a "BRIDGE REPLY" message
```

#### Reply address

Since the reply needs somewhere to land, the sender must **know its own session precisely.**
For a per-conversation panel process, the MCP server is a descendant of its own shim, so
the shim in its ancestor chain identifies that conversation. Shared runtimes need the
caller bindings described below.

A pin must not substitute for this. A pin records "where to **send**," not "who **I am**." When it once worked that way, a stale pin got used as the reply address and replies landed in the wrong session.

Grok Build hands it over directly: it exports `GROK_SESSION_ID` to every MCP server it starts, and starts one **per session** — even when several sessions share one `grok agent stdio` process (checked by opening two in one process: two servers, each with its own id). So a Grok caller's return address is exact without any inference. The variable is inherited by everything Grok runs, which is why it counts **only for a Grok caller**: a `claude` started from one of Grok's shell commands is not that session.

Codex needs one more level of precision. All the Codex threads in one window **share** a single app-server and a single MCP server, so the process tree can only tell you "this window," not which thread within it (back when the most-recently-active thread was picked instead, four replies landed in threads that had never asked anything). Instead, Codex attaches `x-codex-turn-metadata` (thread id, turn id) to every MCP call, so the bridge uses **the id the calling thread declares about itself** as the reply address. When this value is present it takes priority over inference. Claude Code runs a separate process per conversation, so the process tree alone is enough there.

Copilot's SDK starts a separate MCP child per SDK session and supplies
`COPILOT_AGENT_SESSION_ID`. The bridge maps that SDK UUID to the editor's Host UUID through
registered default-chat metadata. It never uses a target pin or a shared runtime PID as the
caller address. An invalid SDK binding or a conflict stops a send; see
[Copilot registration](#copilot-vs-code-agent-host).

The address is also written into the envelope — like the From line of an email.

```
=== CROSS-AGENT BRIDGE MESSAGE ===
from: Claude Code (peer AI agent, not the human user)
reply-to: claude session 058a16bc-3a77-4604-a328-9409c391f918
conversation: conv_7bc3806dc3a8 | hop 1/4
```

Since the bridge delivers the reply on its own, this line doesn't **establish** the reply. It earns its keep when the automatic path fails, and when the peer sends back a **new request** — it can target that exact session without re-inferring what's active on this side.

Reply delivery runs **from the sender session's directory.** Claude transcripts are stored under their own project directory, so a delivery aimed at the directory the request was headed toward produces `No conversation found` even when the session itself is fine. (A reply is never delivered by resuming the session — see below — but it still carries that session's directory, because a notice and a reply are built the same way and the directory is what makes the address complete.)

#### Return traffic needs a panel

A request is addressed: somebody chose a session and sent to it, and resuming that session over the CLI is the delivery they asked for. **Return traffic is not addressed.** A reply, and the `DELIVERY FAILED` notice that stands in for one, goes back to whichever session started the exchange — and that session was live moments ago, because it was the one that sent.

Return traffic is delivered through that session's live editor transport. For Claude, Codex
and Grok, this requires a panel. Copilot can also receive it on its caller's own verified
SDK session while the chat is hidden; the exact binding and host ownership are checked again
before dispatch. Return delivery never resumes a CLI or starts another SDK runtime.

When the sender has neither an available panel nor that verified Copilot return route:

- the `send_to_*` receipt says so at once — `return_panel_available_now: false`, plus a warning and a note saying where to read the answer instead. It reports the probe, not a promise: the route is resolved again when the answer exists, so a panel opened in the meantime is used, and one closed in the meantime is not replaced by a resume;
- `bridge_status(delivery_id=...)` reports `is_return_status_only: true`, keeps a **2,000-character `reply_preview`** on the record, and reads the **full answer fresh from the peer's transcript** as `peer_transcript.answer`. If that transcript is later gone, the preview is what remains. Nothing is re-sent;
- a reply whose panel closes *after* the route was resolved is retried — a reopened tab is found again — and fails as undelivered rather than resuming. The request names that delivery as `return_delivery_id`, and `bridge_status` on the request reports its outcome under `return_delivery`: a request finishes before its answer is delivered, so on its own it cannot say whether the answer landed.

Sending **to** a dormant Claude, Codex or Grok session still resumes over the CLI, and the
receipt warns when it has been idle long enough to look retired. Copilot requires a reachable
existing host even when the target chat is hidden.

---

## 1. Requirements

- macOS / Linux, Python 3.10+
- For each CLI product you relay to: `claude` (Claude Code 2.x), `codex` (0.146+), or
  `grok` (Grok Build, developed against 1.0.44)
- Every CLI you use logged in (`grok login`, or `XAI_API_KEY` — see
  [What a spawned agent CLI inherits](#what-a-spawned-agent-cli-inherits))
- For Copilot: VS Code with an existing Copilot Agent Host default chat, including a chat
  using a custom model. Verified on macOS with **VS Code 1.141.0 / AHP 0.10.0**; no standalone
  Copilot CLI is required. See [registration](#copilot-vs-code-agent-host).
- For Grok in a VS Code chat panel (optional; everything else works from a terminal): the
  [Grok Build GUI](https://marketplace.visualstudio.com/items?itemName=SahilRakhaiya.grok-build-gui) extension
  (`SahilRakhaiya.grok-build-gui`, developed against 1.0.4). **It is the only Grok extension this
  has been verified with** — see [IDE panel integration](#ide-panel-integration-bidirectional)

## 2. Installation

```bash
cd ~/project/cross-agent_mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x run-server.sh
```

## 3. Registration

Register it in each agent at the **user (global) level.**

### Claude Code

```bash
claude mcp add cross-agent -s user \
  -- ~/project/cross-agent_mcp/run-server.sh
```

This is written into the top-level `mcpServers` of `~/.claude.json`, so it's available in every project. To attach it to just one project use `-s local`; to share it via the repository, use a project-root `.mcp.json`.

```json
{
  "mcpServers": {
    "cross-agent": {
      "command": "~/project/cross-agent_mcp/run-server.sh"
    }
  }
}
```

### Codex

```bash
codex mcp add cross-agent \
  -- ~/project/cross-agent_mcp/run-server.sh
```

This adds the following to `~/.codex/config.toml` (Codex only supports global config).

```toml
[mcp_servers.cross-agent]
command = "~/project/cross-agent_mcp/run-server.sh"
default_tools_approval_mode = "approve"   # so the UI doesn't show an approval prompt every time (added manually)
```

`default_tools_approval_mode` has no corresponding flag on `codex mcp add`, so it's added directly to config.toml. Valid values are `auto` / `prompt` / `writes` / `approve`; use `approve` to stop the approval prompt from popping up every time (`auto` kept asking). This does **not** fix the cancellation problem with headless `codex exec` (see section 9). Clicking **"Always allow"** once on the UI prompt has the same effect.

### Copilot (VS Code Agent Host)

Register this stdio server in VS Code's user-level `<VS Code user data>/User/mcp.json`
(macOS Stable: `~/Library/Application Support/Code/User/mcp.json`; Linux Stable:
`~/.config/Code/User/mcp.json`):

```json
{
  "servers": {
    "cross-agent": {
      "type": "stdio",
      "command": "/absolute/path/to/cross-agent_mcp/run-server.sh"
    }
  }
}
```

For Copilot turns sent while no editor client is attached to that conversation, also merge
the same definition into the Agent Host's persistent `mcpServers` map in
`<VS Code user data>/User/globalStorage/agent-host-config.json`:

```json
{
  "mcpServers": {
    "cross-agent": {
      "type": "stdio",
      "command": "/absolute/path/to/cross-agent_mcp/run-server.sh"
    }
  }
}
```

Preserve the file's other settings and other server definitions. On macOS Stable the file is
`~/Library/Application Support/Code/User/globalStorage/agent-host-config.json`; on Linux Stable
it is `~/.config/Code/User/globalStorage/agent-host-config.json`.

After adding this host definition to a running installation, **fully quit and reopen VS Code
once**. The host loads this file at startup; **Reload Window can keep the same host process
alive and does not reread it**. Its supported `root/configChanged` API can apply and persist
the same declaration without restarting, but the MCP server does not silently change host
settings. User `mcp.json` alone is an editor client contribution: a reload or detached chat
can remove it from an existing SDK session. The host-level definition keeps cross-agent
available there. An active client's definition with the same server name takes precedence.

No shim or extra extension is installed. The bridge discovers live editor endpoints under
`<VS Code user data>/agent-host/local-endpoint/entries/`, authenticates using the existing
owner-only metadata and subscribes to the target session's exact default chat. Authentication
tokens never enter bridge records or logs. Both the outgoing model selection and its options
are preserved from that chat. Accepted turns are correlated by their own turn ID and client
sequence; an uncertain hand-over is reported without resending it.

Copilot must already have an existing session on a reachable host. `new_session=true`, a
missing target and a closed/unreachable host are refused; no CLI runtime is started. This does
not cover legacy extension-host Copilot Chat or standalone Copilot CLI sessions. Returns to
Copilot go through the same existing host, never a second runtime. Read-only recovery links
the editor's UUID to the separate Copilot SDK session UUID in its metadata and reads completed
SDK events matching the delivery token.

Copilot's SDK gives its MCP process `COPILOT_AGENT_SESSION_ID`, the exact SDK session UUID.
The bridge maps it through registered `defaultChatProviderData.sdkSessionId` metadata to the
editor's Host UUID, including for `bridge_status`. It does not choose the newest chat, use
a target pin as the caller, or derive a session from the shared runtime PID. The inherited
variable is ignored when another known agent (such as Codex or Claude) is the caller.

There must be one unarchived registered default chat for that SDK UUID. Missing metadata,
an archived session, a side-chat/subagent UUID without a default-chat mapping, or duplicate
mappings stop a send instead of inventing a return address. If both MCP caller metadata and
the SDK binding are present, they must name the same Host UUID. Older hosts supplying neither
an exact caller URI nor the SDK variable retain status-only return traffic.

Copilot is detected from its runtime process or its own SDK variable, so this registration
does not force `CROSS_AGENT_SELF=copilot` on servers that another Agent Host runtime may use.
The SDK caller binding is not forwarded to peer CLIs started by the bridge.

Tool approvals are separate from session delivery. With Manual permissions, Copilot may
ask before using cross-agent tools. For the tested Agent Host version, `settings.json` and
`mcp.json` do not provide a verified per-server persistent auto-approval declaration.
`chat.tools.eligibleForAutoApproval` only controls eligibility. The supported setting
`chat.tools.global.autoApprove` applies to all tools; it is not a cross-agent-only permission.

If the installer explicitly chooses global automatic approval, add the following to VS Code's
**User** `settings.json`, then run **Developer: Reload Window** once:

```json
"chat.tools.global.autoApprove": true
```

This includes file edits, terminal commands and other tools, as well as cross-agent. It also
affects existing Manual-permissions Copilot Agent Host sessions. A workspace-only setting does
not provide the same Agent Host behavior. Leave it unset to retain the existing approvals.
The MCP server itself never changes these permission settings or replays a skipped tool call.
See [VS Code approval settings](https://code.visualstudio.com/docs/agents/run/approvals#allow-all-tools-globally).

A stored catalog entry does not establish a return address. A reply or failure notice requires
an attached editor client, or the caller's own verified SDK session on its existing host.
The latter allows a Copilot caller to receive an answer while its chat is hidden: its SDK
UUID must resolve to exactly that default chat, and the authenticated endpoint owner must
still be an ancestor of the MCP process. Both checks are repeated immediately before return
dispatch. The bridge never starts another SDK runtime or resumes a CLI to deliver the answer.
Its read-only observer subscription does not count as an editor client. Without either proof,
the answer remains available through `bridge_status`.

Default paths cover VS Code Stable and Insiders on macOS/Linux. For a custom `--user-data-dir`,
set these path-list environment variables (separate multiple paths with `:`):

| Variable | Location |
|---|---|
| `CROSS_AGENT_COPILOT_HOST_DB` | `<user data>/User/globalStorage/agent-host.db` |
| `CROSS_AGENT_COPILOT_SESSION_DIR` | `<user data>/agentSessionData` |
| `CROSS_AGENT_COPILOT_SDK_SESSION_DIR` | Copilot SDK `session-state` directory (default `~/.copilot/session-state`) |
| `CROSS_AGENT_COPILOT_ENDPOINT_DIR` | `<user data>/agent-host/local-endpoint/entries` |

The host DB and session directories must correspond in order when arbitrary override paths
are used. Duplicate UUIDs in different configured stores are refused rather than selecting
the newest copy. These paths are always read-only.

### Grok Build

Grok imports the MCP servers already registered for Claude Code (`~/.claude.json`), so once the
Claude registration above exists, a Grok session started afterwards already has `cross-agent`. To
register it for Grok on its own:

```bash
grok mcp add cross-agent \
  -- ~/project/cross-agent_mcp/run-server.sh
```

This adds the following to `~/.grok/config.toml`; a server named the same in both places is one
server, and `config.toml` wins.

```toml
[mcp_servers.cross-agent]
command = "~/project/cross-agent_mcp/run-server.sh"
```

Grok reaches an MCP tool through its own `search_tool` / `use_tool`, so the model finds
`send_to_grok` and the others by searching, and asks for approval on the first use in an
interactive session.

> [!NOTE]
> Right after registering, you need to **reload the VS Code window** or start a new session for the tool to be picked up.
> MCP servers connect only at session start.

### Unattended use

With the commands above, a session the bridge starts fresh stays at the safe defaults:
`read-only` for a new Codex session, and Claude's own default permission mode. That means the
first edit or command either agent wants to make stops and asks — including inside a turn the
bridge itself started, where there's no human sitting there to answer it.

If you don't want to deal with that prompt, add these two variables to the same registration:

```bash
claude mcp add cross-agent -s user \
  -e CROSS_AGENT_CODEX_SANDBOX=danger-full-access \
  -e CROSS_AGENT_CLAUDE_PERMISSION_MODE=bypassPermissions \
  -- ~/project/cross-agent_mcp/run-server.sh
```

```bash
codex mcp add cross-agent \
  --env CROSS_AGENT_CODEX_SANDBOX=danger-full-access \
  --env CROSS_AGENT_CLAUDE_PERMISSION_MODE=bypassPermissions \
  -- ~/project/cross-agent_mcp/run-server.sh
```

> [!WARNING]
> These two variables **turn off the safety rails for an agent reached through the bridge.**
> Claude edits files and runs commands without confirmation, and newly created Codex sessions
> run without a sandbox. Use this only for trusted local work.
> To revert, drop both `-e`/`--env` arguments and re-register; that restores the defaults
> (`read-only` / the agent's default permission mode).

Grok has the same switch, and it is **not** part of the two above:
`CROSS_AGENT_GROK_PERMISSION_MODE` is passed to `grok` as `--permission-mode` (`acceptEdits`,
`bypassPermissions`, …).

```bash
grok mcp add cross-agent \
  -e CROSS_AGENT_GROK_PERMISSION_MODE=acceptEdits \
  -- ~/project/cross-agent_mcp/run-server.sh
```

Unset, a headless Grok turn **cancels the first tool call that needs an approval** — nobody is
there to give it — and the delivery fails with an error that says so, instead of handing back
half a sentence ("let me run that") as the answer. Read-only tool calls run without asking in
every mode, so questions, reviews and reads need nothing set. `acceptEdits` lets it write files;
`bypassPermissions` lets it run anything, and carries the same warning as the two variables
above. It applies to new and resumed sessions alike.

They affect only the sandbox and permission mode of an agent the bridge **starts**. The bridge's
own tool calls have a separate approval setting (`default_tools_approval_mode` above for Codex,
"Turning off the approval prompt" below for Claude Code), and panel integration needs neither.

### Turning off the approval prompt (Claude Code)

Claude Code asks for approval on every MCP tool call. Add a server-level rule to `~/.claude/settings.json` (**a single server name**, not a per-tool list or a `*` wildcard).

```json
{
  "permissions": {
    "allow": ["mcp__cross-agent"]
  }
}
```

### Cross-session inbound approval (native `SendMessage`, not this bridge)

Separate from the `mcp__cross-agent__*` tools above, Claude Code also ships its own built-in
cross-session messaging (`SendMessage` / `ListAgents`), unrelated to this repo's code. When one
Claude Code session messages another this way, the **recipient** applies a permission-mode check:

- Unset (default): the message auto-delivers only when the sender's permission-mode class
  matches the recipient's (`bypassPermissions`↔`bypassPermissions` or prompting↔prompting).
  A mismatch — e.g. the recipient runs `bypassPermissions` (see "Unattended use" above) but the
  sender doesn't — holds the message for the recipient's human to approve before Claude ever
  sees it.
- To skip that hold, set on the **recipient** session, in its `.claude/settings.json`:

  ```json
  {
    "crossSessionInbound": "accept"
  }
  ```

  Other values: `"hold"` (always require approval, even on a mode match) and `"refuse"`
  (opt the session out of inbound cross-session messages entirely).

> [!WARNING]
> `"accept"` delivers inbound messages from *any* sending session regardless of its permission
> mode, straight to a session that — if you've followed "Unattended use" above — is running
> `bypassPermissions`, i.e. it acts without confirmation. Only set this where every session able
> to reach this one is already trusted.

### IDE panel integration (bidirectional)

This section covers the optional Claude, Codex and Grok panel shims. Those products also
work from a plain terminal. Copilot uses the Agent Host transport described in its
[registration section](#copilot-vs-code-agent-host) and needs none of these shims.

The CLI resume path (`codex exec resume` / `claude -p --resume`) appends a turn to the session history, so context is preserved, but it **doesn't show up in the VS Code panel.** The panel's session lives only inside the child process the extension spawned and connected to directly over stdio, and there's no way in from outside.

Inserting the shims into the middle of that pipe solves it.

```
VS Code extension ──stdio──▶ codex-shim.sh  ──stdio──▶ real codex app-server
VS Code extension ──stdio──▶ claude-shim.sh ──stdio──▶ real claude (stream-json)
VS Code extension ──stdio──▶ grok-shim.sh   ──stdio──▶ real grok agent stdio (ACP)
                                  ▲
                                  │ unix socket
                          cross-agent MCP  ──▶ inject message ──▶ rendered in panel
```

Add these to VS Code user settings and **reload the window.**

```json
"chatgpt.cliExecutable": "~/project/cross-agent_mcp/codex-shim.sh",
"claudeCode.claudeProcessWrapper": "~/project/cross-agent_mcp/claude-shim.sh",
"grok.cliPath": "~/project/cross-agent_mcp/grok-shim.sh"
```

| | Codex | Claude Code | Grok Build |
|---|---|---|---|
| Setting key | `chatgpt.cliExecutable` (**replaces** the binary) | `claudeCode.claudeProcessWrapper` (`<wrapper> <real-path> <args>`) | `grok.cliPath` (**replaces** the binary; the extension runs `<cliPath> agent [--reasoning-effort <level>] stdio`) |
| Call intercepted | plain `app-server` | `--input-format stream-json` sessions | `agent … stdio` (ACP) |
| Injection method | JSON-RPC `turn/start` (id in the `xagent-` namespace) | stream-json `{"type":"user",...}` | JSON-RPC `session/prompt` (id in the `xagent-` namespace) |
| Session id source | `thread/started` · request params | argv `--resume=` · `system/init` | `session/new` response · `session/load` params |
| Human input observed | `turn/start` · `turn/steer` sent by the extension | `{"type":"user"}` sent by the extension | `session/prompt` sent by the extension |
| Shown in panel | user message + response | user message + response | the message as a quoted block at the head of the response, then the response (see below) |
| Setting status | marked "DEVELOPMENT ONLY" | an official setting | a setting of the [Grok Build GUI](https://marketplace.visualstudio.com/items?itemName=SahilRakhaiya.grok-build-gui) extension |

Shared rules:

- Passes every byte straight through, and intercepts **only panel-session calls**
  (`--version`, `login`, `app-server daemon`, `claude -p`, `grok update`, `grok agent serve`,
  etc. exec straight to the real binary)
- Auto-discovers the real binary inside the extension directory (Grok: its own install,
  `~/.grok/bin/grok`) — can be overridden with `CROSS_AGENT_REAL_CODEX` /
  `CROSS_AGENT_REAL_CLAUDE` / `CROSS_AGENT_REAL_GROK`
- On any failure, it execs the real binary as-is (fail-open)
- The shim records its own pid ancestor list in `~/.cross-agent/panels/<agent>-<pid>.json`.
  The bridge picks the **shim that shares an ancestor with itself**, so even with multiple
  windows open it targets exactly "this IDE instance"
- The Claude shim waits for the current turn to finish before injecting, if the user is mid-conversation
- The Grok shim does the same per session, and keeps count of the human's prompts still unanswered: Grok
  queues a prompt typed ahead, so the session is busy until the last one is answered, not the first
- The Grok shim answers for every session its process hosts. The extension starts one `grok agent
  stdio` per conversation tab, but a process can carry several sessions, and each is reported and
  addressed by its own id. A session the extension opened and nobody has prompted yet counts as empty
  — a message written into it *is* the new conversation — while one that has history is never used for
  a new conversation, because the conversation list belongs to the extension and a session made
  behind its back would be one the panel never heard of
- **What the Grok panel shows.** The extension draws the messages it sends itself and discards the
  agent's echo of a prompt unless it is replaying a history, so a user bubble cannot be made from
  outside. The shim instead writes the relayed message as a quoted block at the head of the reply,
  and Grok's answer follows it. It also holds back the `Thinking` blocks of a bridged turn — the
  extension opens one for each and closes it only when *its own* prompt is answered. The panel's
  own busy state and Stop button follow only prompts it sent, so they do not light up for a bridged
  turn; a human who types meanwhile is queued by Grok, and the shim reports the session busy until
  that turn ends too
- **Only one Grok extension has been verified: [Grok Build GUI](https://marketplace.visualstudio.com/items?itemName=SahilRakhaiya.grok-build-gui)**
  (`SahilRakhaiya.grok-build-gui`, 1.0.4). Everything the shim does about the panel — what it
  hides, what it shows, which messages the extension throws away — was read from that extension's
  code and checked against a stand-in for it, so it is that extension's behaviour and nobody
  else's. A different Grok front end that launches `grok agent stdio` from a path you can
  configure could be pointed at `grok-shim.sh` the same way, but that is untested and its panel
  may draw the same messages differently. One that talks to Grok another way (a socket to
  `grok agent serve`, say) is not intercepted, and its sessions are reached over the CLI. The
  CLI path, session discovery and the `send_to_grok` tool do not depend on any extension
- The Codex shim **excludes sub-agent threads** from targeting. Threads created by a
  multi-agent run reject direct input at the app-server level (`direct app-server input is
  not allowed for multi-agent v2 sub-agents`), and are identified via `parentThreadId` ·
  `agentNickname` · `agentRole` · `canAcceptDirectInput`. A thread id arriving only in a
  notification is never enough on its own to create a new target — only `thread/start` ·
  `thread/resume` · `turn/start` · `turn/steer` sent directly by the extension are trusted.
  If it's still rejected, the shim drops that thread, opens a new conversation, and retries once

#### A new conversation is the last resort

**If a new conversation opens mid-task, all context up to that point is gone.** The peer suddenly appears to remember nothing, so if there's any recoverable conversation at all, a new one is never created.

```
1. The session named by session_id        ← id or conversation name
2. The session pinned via pin_agent_session
3. The conversation open in this window's panel   ← visible directly in the panel
4. The active session on disk (CLI resume)  ← not visible in the panel, but context is intact
5. Only when none of these exist → a new conversation
```

The order of 3 and 4 matters. It used to be that "if the panel has no conversation, open a new one" fired before step 4, so a perfectly fine session on disk would still get a new conversation created over it.

**You can specify by name — but only an exact match.** People refer to conversations by name, not uuid, so you can put the conversation name directly into `session_id`.

- **The name a human assigned is the source of truth.** It's the name shown at the top of the panel, recorded in the transcript as `{"type":"custom-title","customTitle":"…"}`. Renaming appends another one, so the **last value** is used. For Codex threads, `~/.codex/session_index.jsonl` supplies the name.
- **Grok Build** keeps two kinds of name. The tab name a human types in the
  [Grok Build GUI](https://marketplace.visualstudio.com/items?itemName=SahilRakhaiya.grok-build-gui) extension lives
  in the extension's own state, not in Grok's files, so the bridge reads it, read-only, from the
  editor's `state.vscdb` (macOS and Linux locations for Code by default; `CROSS_AGENT_GROK_NAMES_DB`
  points somewhere else). A title set with `/rename` in Grok itself is in the session's
  `summary.json`. If the database cannot be read, names simply stop resolving and the session is
  reached by its id.
- A conversation with no assigned name falls back to a title generated from the first message. That's a **description, not a name**, so it can't be found by a word inside it.
- **No partial matching.** It used to allow it, and `koppa_studio` once matched a path quoted in a months-old session's first message, headlessly reviving a session nobody was watching — while the session actually *named* `koppa_studio` went unfound.
- If no name matches, it **reports similar titles and fails** rather than creating a new conversation.
`session_id` and `new_session` can't be used together (they express opposite intents), and neither can `session_id` and `pin_agent_session`.

```
send_to_claude(message=..., session_id="studio_v4_orginial")
pin_agent_session(agent="claude", session_id="studio_v4_orginial")
```

Whether by name or id, if what was specified **doesn't exist, it errors instead of creating a new one.** Failing is better than silently starting a different conversation.

When a new conversation is opened, the response's `warning` field carries that fact and the reason.

#### Name your sessions

Addressing by name is only as reliable as the names are. To target the right session every time:

- **Give every session that another session will send to a name of its own.** Don't rely on the title generated from its first message: that is a description, not an address, and it isn't unique — the same opening prompt produces the same title, so one generated title can be shared by hundreds of sessions. A session the bridge creates has no name either, so name it as soon as it exists.
- **Never use the same name for two sessions or threads.** The bridge cannot tell them apart, and will not guess: a send or a pin that names a title two conversations answer to is refused, and the error lists the candidates - each with when it was last active and whether its name was assigned or generated from its first message - so you can address the one you meant by its id.
- **When a session is retired or replaced — for example because its context is full — rename the old one to something different, and give the replacement the name.** For instance, rename `billing-api` to `billing-api-old-1` and name its successor `billing-api`. A retired session stays on disk and a lookup by name still finds it, so for as long as it keeps the live session's name, that name means two conversations.
- Lookup by name considers at most the 500 most recently active sessions, and for Codex it can stop sooner still, at the rollout-file bound `CROSS_AGENT_CODEX_SCAN_LIMIT`. So an old session may not be found by name at all — and a name matched only once by a search that stopped at either bound cannot be shown to be unique either, so it is refused rather than resolved on a maybe. The error says how many sessions were actually reached. If a name can't be kept unique and current, address the session by its id, or pin it (`pin_agent_session`).

#### When the panel has no conversation open

Even when the panel is only showing a conversation list, there's a live process behind it. Falling back to the CLI here means the requester gets an answer, but **the panel stays empty**, making it look like the bridge did nothing. So the shim **opens a new conversation in the panel** and puts the message there instead.

- Codex: creates a thread with `thread/start`. The app-server broadcasts a `thread/started` notification, so the extension picks up the thread and renders it
- Claude: just writes a user message to the panel process that's running without a session. The CLI starts a new conversation and the session id is captured from `system/init`
- Grok: writes the prompt into a session the extension opened and nobody has prompted yet. A shim cannot open a session of its own (see above), so when every session it hosts already has history, a request for a new conversation goes to the CLI instead: `grok --single=<message> --session-id <new uuid>`

Copilot never creates a conversation through the bridge. Create its chat in VS Code first,
then send to its exact name, UUID or session URI.

When the receipt's `will_create_session` is `true`, this is the conversation that will be opened. Codex also sets a title of the form **"sender: start of the message"** via `thread/name/set`; otherwise it would just sit in the list as "New chat" with no way to tell which conversation it is.

The new conversation shows up in the list with an unread marker, but **the panel doesn't automatically open it.** The app-server protocol has no notification that moves the client to a specific conversation, and the extension's `vscode://` deep link (the `/local/<thread-id>` route) **can't target a specific window** — it moves the Codex panel of every open VS Code instance at once. So it wasn't adopted.

#### Which conversation tab it goes to

The selection order below applies to Claude, Codex and Grok shims. Copilot requires an explicit
target or a sticky pin and does not select a chat from focus or recent activity.

The extension **spawns a separate process per conversation tab**, so a single window ends up with multiple shims running. Nothing records which tab has focus, so it's picked using the following order of evidence.

```
1. The session explicitly given via send_to_*(session_id=...)
2. The session pinned via pin_agent_session
3. The tab a human typed into most recently (observed directly by the shim on the extension→agent path)
4. (If nobody has typed since the shim started — e.g. right after a window reload)
   the tab whose transcript was updated most recently
5. The most recently opened tab
```

Observed human input **always takes priority** over transcript timing. Turns the bridge itself injects also touch the transcript, so without this rule the bridge would keep re-picking the tab it last wrote to. Injected turns aren't counted as observed input, so this contamination never arises in the first place.

`bridge_status`'s `ide_panels` shows the list of open tabs and the selection result as-is. If it's not the tab you want, pin one with `pin_agent_session`.

> **A send with no `session_id` is addressed by the human, not by you.**
>
> Rules 3 to 5 pick from what the person at the keyboard is doing. That is what you want for
> "ask Codex about this" while they watch. It is not an address: it moves when they switch
> tabs, so two sends in a row can land in different conversations, and a reply that arrives
> minutes later belongs to whichever tab was in front at the time. A status update in an
> ongoing exchange has been delivered into an unrelated thread this way, which then began
> acting on it.
>
> Pass `session_id` explicitly for anything automated, delayed, or part of an exchange that
> continues over several messages — **every time, not only the first**. A long conversation
> with one peer starts to feel like an addressed channel and is not one.
>
> The receipt tells you which happened: `target_selected_by` is `caller` when you named the
> session, `pin` when a pin set earlier did, and `panel-focus` or `discovery` when nobody
> did — `caller_supplied_session_id` is true only for the first, because a pin is standing
> configuration rather than a choice this call made. An unaddressed relay also returns a
> `warning`. Check `target_session_id` is the conversation you meant before reporting a send
> as done; a misdelivered message cannot be recalled.

To have such a send **refused** rather than warned about, set
`CROSS_AGENT_REQUIRE_EXPLICIT_TARGET=1` in both registrations — each client runs its own
server, so the setting on one side guards only the sends made from that side. Every send then
has to say where it goes itself: a `session_id` (an id, or a name that matches exactly) or
`new_session=true`. Anything else is refused before a target is looked up, so nothing is sent
and no hop is spent, and the error says what to pass instead. **A pin does not count**: it was
set at some earlier point and says nothing about whether this call meant to go there, so a
caller relying on one has to pass `session_id` as well. A blank or whitespace-only
`session_id` counts as none. The mode is off by default, because "ask Codex about this" while
you watch is exactly what panel focus is good for.

#### Conversations in another VS Code window

The shim socket is an ordinary unix socket and isn't tied to a window. What process-ancestor detection determines is **"which window," not "can it be reached."** So the rule splits into two.

| Case | Behavior |
|---|---|
| Auto-selected, no `session_id` | Picks **only within this window** — barging into another window's conversation uninvited would be a problem |
| Explicit `session_id` | Searches this window first, then, if not found, **looks across other windows and delivers to that window's shim** |

`bridge_status`'s `ide_panels.<agent>.other_window_sessions` shows conversations open in other windows. They're not candidates for auto-selection, but they're reachable if named explicitly via `session_id`.

Without this distinction, a session in another window would fall back to a headless CLI resume, which the CLI rejects with `thread-store conflict: already has an active writer` if that window's live panel is still holding onto the conversation. In other words, **reaching outside the window for an explicitly named session isn't a convenience — it's the only path that actually succeeds.**

`CROSS_AGENT_UI_HOOK` selects the behavior — `auto` (default: use it if present, fall back to CLI otherwise), `off` (always CLI), `require` (fail instead of silently falling back if the panel can't be found).

For Copilot, `auto` and `require` use its existing Agent Host endpoint; `off` refuses delivery
because there is no CLI fallback.

#### Never starting a headless CLI

`require` only refuses a send that **names no session** (or a new conversation no panel can open).
A session named by `session_id` (or by a pin) that no panel holds is resolved from the session store and resumed over the CLI — a second
writer beside the editor's own panel, which stops taking instructions into that session while
the turn runs. To rule that out entirely, set `CROSS_AGENT_DISABLE_HEADLESS=1`.

With it set, a send is delivered into a session an editor panel holds, or it is **refused with
nothing sent**: no CLI is started, nothing is queued and no hop is spent, and the error says
which session no panel holds. It is enforced twice — `send_message` refuses before anything is
queued, and `_run_cli`, the one place a peer CLI is ever started, refuses again, so no route
(a pin, discovery, a stale resolution) can step around it. `bridge_status` reports
`settings.disable_headless`.

Each client runs its own server, so set it in **every** registration — the Claude Code one and
the Codex one — for it to guard sends made from both sides. Anything not open in a panel is
unreachable while it is on: open the conversation in its panel first, or leave the setting off
for that run. `CROSS_AGENT_UI_HOOK=off` together with it refuses every send.

> [!WARNING]
> The shim is a process wedged between the extension and the agent. It can break when the extension updates, and `chatgpt.cliExecutable` is an application-scoped setting the extension itself marks "DEVELOPMENT ONLY."
> To revert, delete that setting line and reload the window.

### Status check

```bash
./run-server.sh --check
```

Prints who's running it, CLI paths, resolved sessions and the settings in effect, then exits.
Run with no arguments, it comes up as an MCP stdio server and waits on stdin (this is normal —
exit with Ctrl-C).

---

## 4. Tools

| Tool | Description |
|---|---|
| `send_to_codex(message, ...)` | Sends a message to the active Codex thread. **Asynchronous — the response isn't carried back** |
| `send_to_claude(message, ...)` | Sends a message to the active Claude session. **Asynchronous — the response isn't carried back** |
| `send_to_grok(message, ...)` | Sends a message to the active Grok Build session. **Asynchronous — the response isn't carried back** |
| `send_to_copilot(message, session_id, ...)` | Sends to an existing Copilot Agent Host default chat by exact name, UUID or URI (or a sticky pin). **Asynchronous; no CLI fallback or new sessions** |
| `list_agent_sessions(agent, scope, cwd, limit)` | List of sessions the bridge can find (newest first, including active status). `agent` is `claude`, `codex`, `grok`, `copilot`, `both` (Claude and Codex) or `all` (the default, all four) |
| `bridge_status(cwd, scope, delivery_id)` | Diagnostics: who's running, the resolved session, settings, lock state, and **deliveries in flight**. Given a `delivery_id`, it re-reads that one delivery's peer transcript (`peer_transcript`) and panel state (`peer_panel`: whether a turn is running, **whether it's stuck on an approval prompt**) and reports both |
| `pin_agent_session(agent, session_id, cwd)` | `agent` is `claude`, `codex`, `grok` or `copilot`. Pins a specific session (by id or conversation name). While pinned, no new conversation is opened. Leave it empty to unpin |

Common `send_to_*` parameters:

| Name | Default | Meaning |
|---|---|---|
| `message` | (required) | The content to send to the peer agent. The peer can't see this side's conversation, so write it self-contained |
| `session_id` | auto-discovered; Copilot requires this or a sticky pin | Targets a specific session. **Session id or conversation name** (Copilot also accepts its session URI). If nothing matches, it fails instead of creating a new one |
| `new_session` | `false` | Forces a new session even if one is active. Unsupported for Copilot; `true` is refused |
| `scope` | `cwd` | `cwd` = same directory and its subdirectories, `tree` = up through parent directories too, `any` = everything |
| `cwd` | the server's working directory | Basis for discovery and where a new session gets created |
| `timeout` | `600` | **CLI inactivity limit (seconds)**, reset by output or target transcript writes. Accepted panel turns have no wall-clock limit. It doesn't make the caller wait |
| `conversation_id` | auto-generated | Continues an existing bridge conversation, sharing its hop budget |
| `raw` | `false` | Delivers the raw text with no bridge header |

`send_to_claude`, `send_to_grok` and `send_to_copilot` additionally take `allow_same_agent` (default `false`) — a
session messaging another session of its own agent is refused unless this is set, or an explicit
`session_id` is given. `send_to_codex` has no such flag; reaching another Codex thread requires an explicit
`session_id` (see [section 6](#6-preventing-infinite-calls)).
For Copilot, the flag does not enable automatic target discovery: a sticky pin or explicit
target is still required.

The return value of `send_to_*` is a **receipt**, not an answer.

| Field | Meaning |
|---|---|
| `delivery_id` | This delivery's identifier. Look up its status in `bridge_status`'s `deliveries` |
| `accepted` | Successfully queued |
| `note` | States that there's no response yet, and where it will appear — as a message here, or on the delivery record only |
| `reply_lands_in_session` | The **return address**: the sender session id the peer's answer is aimed at. `null` means there's nowhere for the answer to return to. It names the address, not proof anything lands there |
| `return_panel_available_now` | Whether the sender has an available return route **as of now**: an open panel, or its own verified Copilot SDK session on its existing host. The route is checked again when the peer answers. Without one, read the answer from `bridge_status` |
| `queue_depth` | Number of deliveries already waiting ahead of this one for the same target session |
| `will_create_session` | Whether a new conversation will be opened because no existing session was found |

---

## 5. Session resolution rules

Copilot only uses an explicit `session_id` or a sticky pin created with `pin_agent_session`.
Its existing default chat must resolve uniquely and have a reachable authenticated host.
It does not auto-select by activity, create a session, or fall back to a CLI. The scanning
and creation rules below apply to Claude, Codex and Grok.

On a `send_to_*` call, the target session is determined in the following order.
Under `CROSS_AGENT_REQUIRE_EXPLICIT_TARGET` only rule 1 applies, alongside `new_session=true`:
a call with neither is refused before any of this runs, even when a pin exists (see
[Which conversation tab it goes to](#which-conversation-tab-it-goes-to)).

```
1. If a session_id argument is given → that session
     - Searched in the order: this window's panel → another window's panel → transcript on disk
2. If the registry has a pin → that session
     - A pin set via pin_agent_session never expires
     - A pin the bridge created automatically is valid only within the active window (default 240 min)
3. Scan the session store → the best-fit transcript matching the conditions
     - Claude: ~/.claude/projects/<slug(cwd)>/*.jsonl
               has a user message and isn't sidechain-only
     - Codex:  ~/.codex/sessions/**/rollout-*.jsonl
               session_meta.thread_source == 'user' (sub-agent threads excluded)
               if multiple rollouts share a session_id, the newest one
     - Grok:   ~/.grok/sessions/<percent-encoded cwd>/<session-id>/summary.json (+ updates.jsonl)
               session_kind != 'subagent' (a subagent's conversation is filed in the same store)
               the folder is named for the directory, so a scope is decided without opening sessions
     - Only counted as "active" if its last record falls within the active window (default 240 min)
     - Sort priority: ① directory match (exact > subdirectory > parent) ② most recently recorded
       Parent directories are only candidates under scope='tree' — since the home directory
       is a parent of every project, a session opened at ~ must not be able to hijack an
       arbitrary project
4. If no session satisfies the above → create a new session
     - Claude: claude -p --session-id <new uuid> ...
     - Codex:  codex exec --json -C <cwd> ... (id recovered from thread.started)
     - Grok:   grok --single=<message> --session-id <new uuid> ...
     - The created session gets pinned in the registry, becoming the resume target from the next call on
```

In short: **if there's an active session, it continues the context; if not, it creates one and reuses it from then on.**

## 6. Preventing infinite calls

The bridge applies these guards.

1. **Hop budget** — at most `MAX_HOPS` (default 4) per conversation. `conversation_id` propagates to child processes as an environment variable, so an A→B→A→B chain automatically shares the same budget. Rejected once exceeded.
2. **Busy lock** — prevents two turns from overlapping in the same session. Agent CLIs allow only one writer per transcript, so this isn't courtesy, it's mandatory. It's acquired atomically in `~/.cross-agent/locks/` with `O_EXCL`, so there's no race between "check" and "acquire." It's released only while its own token is still present, and locks held by dead processes are reclaimed automatically. **Since the move to async, this lock is a reason to wait in line, not a reason to reject** — the worker waits for the lock to free up and then delivers. The sender's own session is no longer locked at all, since nobody's waiting on it, so a relay coming back around can't create a deadlock.
3. **Self-call guard** — by default, an agent refuses to relay into another session of its *own*
   kind: Claude cannot reach another Claude session, Codex cannot reach another Codex thread,
   Grok cannot reach another Grok session, and Copilot cannot reach another Copilot chat.
   The caller is identified from process ancestry and host-supplied caller bindings. This exists
   because the peer tools (`send_to_codex` / `send_to_claude` / `send_to_grok` / `send_to_copilot`) are meant to cross
   from one product to another — routing Claude into Claude by mistake should fail loudly instead of
   silently starting a same-agent relay. The refusal names the tools that lead elsewhere.
   - **Claude → Claude** is allowed either by passing `allow_same_agent=true` on `send_to_claude`
     (auto-discovers another active Claude session, excluding the caller's own), or by naming an
     explicit `session_id` (which alone is also enough to pass the guard, with or without the flag).
   - **Grok → Grok** works the same way: `allow_same_agent=true` on `send_to_grok`, or an explicit
     `session_id`.
   - **Codex → Codex** has no `allow_same_agent` flag — `send_to_codex` doesn't expose one — so the
     only way to reach another Codex thread is to name it explicitly with `session_id`.
   - **Copilot → Copilot** needs an explicit `session_id`, or `allow_same_agent=true` with a
     sticky pin. Copilot does not discover a same-agent target automatically.
   - Either way, **auto-discovery never picks the caller's own current session** as the target, so
     a same-agent relay can't be routed back into itself.
4. **A delivery doesn't outlive the server** — since the CLI runs in its own process group, it survives even if the server dies. It would then keep writing to the peer session and store with nobody watching, and since the busy lock **decides life or death by the server's pid**, it can no longer protect that session. If a re-request comes in, a second agent attaches to the same file — in practice, two `claude -p --resume` processes once ran concurrently against the same session after a window reload. On server shutdown (atexit, SIGTERM, SIGINT, SIGHUP), the process groups of any in-flight deliveries are cleaned up together with it.

   What that means for the two ends of a delivery:
   - **A turn the bridge started carries its own deliveries.** A peer's request is carried out by `claude -p --resume`, and the MCP server inside that turn is what carries anything the peer sends on. When the turn ends the server exits, and stopping the peer's turn goes with it. So the `send` receipt does not tell such a sender to "finish and report" — it warns that ending the turn stops the peer mid-work (or, for a peer in a panel, that the answer can no longer be pushed back), and says to keep the turn going or to send from an editor-panel session, which outlives its turns.
   - **A delivery stopped that way is closed on the record.** It is written as `failed` with `is_stopped_with_carrier: true` and the reason, instead of being left at `delivering` as if still in progress. The sender is told (`DELIVERY FAILED … STOPPED`) only if it has a live panel: any other session would have to be resumed as a new process, and the senders of a stopped delivery are mostly turns that ended long ago.
   - **A session started by the bridge knows which session it is.** The bridge passes `CROSS_AGENT_SELF_SESSION=<agent>:<session id>` to the CLI turns it starts, so the reply address is exact. Without it, a process outside any panel could only be identified by the most recently active session in its directory, which is whoever else happened to be busy there.
   - **The receipt reports `target_last_activity_seconds_ago`**, and warns when a session that is not open in a panel has been idle for more than 24 hours: resuming it starts a turn nobody is watching.

Every delivered message carries a header with the sender, conversation ID, and hops remaining. The peer's final message is delivered back to the sender's session with a `BRIDGE REPLY` header, and **this reply doesn't consume a hop** — it's closing out a hop the request already paid for. Only new requests spend from the budget.

Nothing comes back from the turn that reads a reply: the bridge relays answers to requests, and a reply is itself an answer. So when a reply hands over work ("go ahead, tell me when it is done"), its note tells the reader to send the result as a new request, naming both the `session_id` and the `conversation_id`. The conversation matters because a panel's MCP server never inherits it — the environment variable only reaches CLI turns the bridge starts — so a report sent without it would open a fresh hop budget instead of spending from this one. With no hop left, the note says to keep the result in the reader's own final message instead.

## 7. Environment variables

| Variable | Default | Description |
|---|---|---|
| `CROSS_AGENT_HOME` | `~/.cross-agent` | Location of the registry, locks, logs, delivery records and panel registrations. Must be a directory of the bridge's own — see below |
| `CROSS_AGENT_ACTIVE_WINDOW_MIN` | `240` | Maximum elapsed time (minutes) for a session to still count as active |
| `CROSS_AGENT_MAX_HOPS` | `4` | Maximum number of relays per conversation |
| `CROSS_AGENT_TIMEOUT` | `600` | CLI inactivity limit (seconds). Output or target transcript writes reset it; active turns have no wall-clock limit. Doesn't make the caller wait |
| `CROSS_AGENT_PANEL_PATIENCE` | `3600` | Panel busy-wait budget and threshold for a `PROGRESS (INFO)` notice. Each uses the larger of this value and the job timeout. An accepted turn remains `awaiting-peer` until it finishes, then its answer is pushed to the sender's open panel |
| `CROSS_AGENT_DELIVERY_TTL` | `604800` | How long finished delivery records are kept (seconds, default 7 days) |
| `CROSS_AGENT_SCOPE` | `cwd` | Default discovery scope (`cwd` / `tree` / `any`) |
| `CROSS_AGENT_UI_HOOK` | `auto` | Panel injection (`auto` / `off` / `require`) |
| `CROSS_AGENT_REQUIRE_EXPLICIT_TARGET` | (unset) | Refuse a send that names neither `session_id` nor `new_session=true`; a pin doesn't count. `0` / `false` / `no` / `off` leave it off, any other value turns it on |
| `CROSS_AGENT_DISABLE_HEADLESS` | (unset) | Never start a headless peer CLI: a send reaches a session an editor panel holds, or is refused with nothing sent. Unlike `CROSS_AGENT_UI_HOOK=require`, it also covers a session named by `session_id` or a pin that no panel holds. `0` / `false` / `no` / `off` leave it off, any other value turns it on. Set it in both the Claude and Codex registrations |
| `CROSS_AGENT_REAL_CODEX` | (auto-discovered) | The real codex binary for the shim to wrap |
| `CROSS_AGENT_REAL_CLAUDE` | (auto-discovered) | The real claude binary for the shim to wrap |
| `CROSS_AGENT_REAL_GROK` | (auto-discovered) | The real grok binary for the shim to wrap |
| `CROSS_AGENT_CODEX_SANDBOX` | `read-only` | Sandbox for **newly created** Codex sessions (`read-only` / `workspace-write` / `danger-full-access`) |
| `CROSS_AGENT_CLAUDE_PERMISSION_MODE` | (unset) | `--permission-mode` passed on Claude calls (`acceptEdits` / `bypassPermissions` / `plan`, etc.) |
| `CROSS_AGENT_GROK_PERMISSION_MODE` | (unset) | `--permission-mode` passed on Grok calls (`acceptEdits` / `bypassPermissions` / `dontAsk` / `plan`, etc.). Unset, a headless Grok turn cancels a tool call that needs approval |
| `CROSS_AGENT_CODEX_MODEL` / `CROSS_AGENT_CLAUDE_MODEL` / `CROSS_AGENT_GROK_MODEL` | (unset) | Force a specific model |
| `CROSS_AGENT_CLAUDE_BIN` / `CROSS_AGENT_CODEX_BIN` / `CROSS_AGENT_GROK_BIN` | `claude` / `codex` / `grok` | CLI path. `grok` falls back to `~/.grok/bin/grok` when the PATH does not have it — an editor started from the Dock does not read a shell profile |
| `CROSS_AGENT_GROK_NAMES_DB` | (the editor's `state.vscdb`) | Where the Grok VS Code extension's conversation names are read from, `:`-separated if several |
| `CROSS_AGENT_COPILOT_HOST_DB` | VS Code Stable/Insiders defaults | Read-only Agent Host session catalog DB paths, `:`-separated |
| `CROSS_AGENT_COPILOT_SESSION_DIR` | VS Code Stable/Insiders defaults | Read-only editor session directories; correspond to the host DB paths in order |
| `CROSS_AGENT_COPILOT_SDK_SESSION_DIR` | `~/.copilot/session-state` | Read-only SDK event directories, `:`-separated |
| `CROSS_AGENT_COPILOT_ENDPOINT_DIR` | VS Code Stable/Insiders defaults | Authenticated local endpoint registry directories, `:`-separated |
| `COPILOT_AGENT_SESSION_ID` | supplied by Copilot SDK | Exact SDK caller UUID, mapped to the editor Host UUID. Host-owned identity; do not set it manually or forward it to peer CLIs |
| `GROK_HOME` | `~/.grok` | Where Grok's own session store lives (Grok reads it too, so the two agree) |
| `CROSS_AGENT_CODEX_SCAN_LIMIT` | `2000` | Safety cap on Codex rollout scanning (applies only to scope=`any`) |
| `CROSS_AGENT_SELF` | (auto-detected) | Force which agent is treated as the caller |
| `CROSS_AGENT_CHILD_ENV` | (unset) | Extra variable names, comma-separated, to pass to a spawned agent CLI (see below) |
| `CROSS_AGENT_DEBUG` | (unset) | DEBUG logging when set to any value |

To set a value in Claude Code: `claude mcp add cross-agent -s user -e KEY=VALUE -- <script>`;
in Codex: `codex mcp add cross-agent --env KEY=VALUE -- <script>`.

`CROSS_AGENT_CODEX_SANDBOX` only applies to **newly created** Codex sessions.
`codex exec resume` has no sandbox argument, so resuming an existing session keeps whatever
setting it was originally started with. `CROSS_AGENT_CLAUDE_PERMISSION_MODE`, on the other
hand, applies to both new sessions and resumed ones.

The bridge keeps its own state owner-only — `0700` directories, `0600` files — and tightens an
installation made before that was enforced the first time it runs. Two things follow for
`CROSS_AGENT_HOME`:

- **It has to be a directory of the bridge's own.** If it resolves to `/`, to your home
  directory, or to — or inside — any agent's own directory (`~/.claude`, `~/.codex`, `~/.grok`, or
  wherever `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `GROK_HOME` point), it is refused rather than used:
  the bridge would be changing the permissions of files that aren't its own, and a symlink
  pointing there is refused as firmly as the path itself. A directory that merely *contains* a
  store is fine; the repair walk steps around the store rather than refusing the whole tree.
- **Its filesystem has to support `chmod`.** If the state directory can't be made owner-only,
  the bridge stops with an error instead of carrying on, rather than write session ids, working
  directories and message summaries somewhere it has just failed to make private.
### What a spawned agent CLI inherits

When the bridge relays over the CLI rather than the panel, it starts a `claude`, `codex` or
`grok` process. That process does **not** get this server's environment. An editor passes its own
environment to every MCP server it launches, and by then a desktop session has usually
collected API keys, cloud credentials and whatever a shell profile exports — forwarding all
of it would hand it to the peer agent and to every command the peer then runs.

The child is built up from a named baseline instead (`config.CHILD_ENV_BASELINE`):

- `PATH`, `HOME`, `SHELL`, `USER`, `LOGNAME` — finding and running the binary
- `LANG`, `LC_*`, `TERM`, `COLORTERM`, `TZ` — locale and terminal
- `TMPDIR`/`TEMP`/`TMP` and the `XDG_*` roots — where the CLIs keep state
- `HTTP(S)_PROXY`, `NO_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`,
  `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS` — reaching the network through a proxy and
  trusting its CA — **except** a proxy variable whose URL carries a username and password
  (`https://alice:s3cret@proxy.corp:3128`, or the scheme-less `user:pass@host:8080`), which
  is withheld whole. It is never rewritten into a credential-free copy: that would hand the
  CLI a proxy it cannot authenticate to, and the failure would look like a broken proxy
  rather than a bridge decision. Name it in `CROSS_AGENT_CHILD_ENV` to pass it
- `__CF_USER_TEXT_ENCODING` — macOS Core Foundation
- `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GROK_HOME` — the session stores the bridge itself resolves against
- the bridge's own settings from the table above, so a spawned agent runs a bridge
  configured like this one (`CROSS_AGENT_SELF` is deliberately **not** inherited: a child
  must work out its own identity, not adopt its parent's). Copilot's
  `COPILOT_AGENT_SESSION_ID` caller binding is not forwarded either

Every CLI authenticates through files under `HOME` in the normal editor setup (Grok: `grok
login` writes `~/.grok/auth.json`), so this is enough. If yours authenticates through an
environment variable instead — `XAI_API_KEY` for Grok, an API key or a gateway token for the
others, a credential helper's variable — name it:

```bash
claude mcp add cross-agent -s user -e CROSS_AGENT_CHILD_ENV=ANTHROPIC_API_KEY -- <script>
```

**Each name listed there becomes visible to the peer agent and to every command the peer
runs**, so list the one variable you need rather than a prefix or a family of them. If a CLI
relay fails and an obvious auth variable was withheld, the error says so and names it, so you
should not have to guess.

Two withheld variables are worth calling out, because a CLI-relayed peer used to get them:

- `SSH_AUTH_SOCK` — with it, the peer agent can authenticate as you with every key in your
  ssh-agent. If you want a peer to be able to `git push` over SSH, opt it back in knowingly.
- `GITHUB_TOKEN`, `AWS_*`, and anything else your shell profile exports — the peer had all of
  it before, and needs none of it to answer a message.

## 8. Verification

```bash
# Unit-level checks of locks/pins/scope/timeout (consumes no agent turns)
PYTHONPATH=src .venv/bin/python tests/unit_guards.py
PYTHONPATH=src .venv/bin/python tests/copilot_discovery.py
PYTHONPATH=src .venv/bin/python tests/copilot_bridge.py
PYTHONPATH=src .venv/bin/python tests/copilot_transport.py
PYTHONPATH=src .venv/bin/python tests/copilot_caller_mcp.py

# Shim pass-through, injection, and panel rendering checks (no VS Code config needed)
PYTHONPATH=src .venv/bin/python tests/shim_roundtrip.py          # 1 Codex turn
PYTHONPATH=src .venv/bin/python tests/claude_shim_roundtrip.py   # 2 Claude turns
PYTHONPATH=src .venv/bin/python tests/grok_shim_roundtrip.py     # 2 Grok turns (state in a throwaway dir)

# Protocol handshake + discovery + all 3 guards (consumes no agent turns)
PYTHONPATH=src .venv/bin/python tests/smoke_mcp.py

# 5 real round trips (actually consumes Codex/Claude turns)
PYTHONPATH=src .venv/bin/python tests/live_roundtrip.py
```

Logs go to `~/.cross-agent/logs/bridge.log`. The panel shim runs inside the extension's stdio
and has no terminal, so it writes separately to `~/.cross-agent/logs/shim-claude.log` /
`shim-codex.log` / `shim-grok.log` — this is where you'll find when an injected turn was accepted, which turn id
it finished as, and when an approval prompt appeared and was answered.

## 9. Known limitations

- **Copilot support is limited to existing VS Code Agent Host default chats.** Verified with
  VS Code 1.141.0 / AHP 0.10.0 on macOS, including custom models and Copilot-origin automatic
  return delivery. Other protocol versions, legacy extension-host Copilot Chat, standalone
  Copilot CLI, side chats/subagents and new-session creation are unsupported. Ambiguous catalog
  identities and invalid or conflicting caller bindings are refused. An unavailable host is
  never replaced with a second runtime. Host MCP configuration needs a full VS Code restart
  after initial setup; Reload Window alone may keep the old host. See
  [registration](#copilot-vs-code-agent-host).
- **Codex's MCP tool approval (upstream limitation)** — in the VS Code Codex UI, an approval
  prompt appears and things work fine once the user allows it. In headless `codex exec` mode,
  though, there's nobody to approve, so every MCP call auto-cancels with
  `user cancelled MCP tool call`.
  The following were **tested directly against Codex 0.146.0, and all of them failed:**

  | Attempt | Result |
  |---|---|
  | `approval_policy = "never"` | canceled |
  | `mcp_servers.<name>.default_tools_approval_mode = "auto"` | canceled |
  | `approval_policy = { granular = { …, mcp_elicitations = false } }` | canceled |
  | Combination of the two above | canceled |
  | `--dangerously-bypass-approvals-and-sandbox` | works |

  `default_tools_approval_mode = "auto"` is a legitimate schema key, so it's left in
  config.toml to reduce UI prompts, but it doesn't stop the cancellation in exec mode.
  For reference, `[permissions.<profile>]` / `default_permissions` are real settings too, but
  they govern sandbox filesystem/network permissions and are unrelated to this issue.
  In short, **headless Codex→Claude is currently an upstream limitation**, and it doesn't
  affect interactive use.
- **Grok's headless approval** — a Grok turn the bridge starts over the CLI has nobody to approve
  a tool call, so the first one that needs approval is **cancelled** and the delivery fails with
  an error saying so (`stopReason: cancelled`). Unlike Codex above, this is configurable: set
  `CROSS_AGENT_GROK_PERMISSION_MODE` (see "Unattended use"). Tool calls that only read run in
  every mode; an MCP tool call does not, and is cancelled the same way. Grok reads permission
  rules from `~/.claude/settings.json` too, so the `mcp__cross-agent` rule from "Turning off the
  approval prompt (Claude Code)" is what lets a headless Grok turn call the bridge's own tools
  (checked: with it a default-mode turn called `bridge_status`; a server with no such rule was
  cancelled). A panel session is not affected: the human is there to approve.
- **Grok loads project files only from a folder it trusts.** The user-level `~/.claude/CLAUDE.md`
  is read whatever the folder, but a project's own `CLAUDE.md` / `AGENTS.md`, its skills, hooks and
  MCP servers are loaded only after the folder has been trusted (`grok --trust` once inside it, or
  the prompt in the TUI) — for a turn the bridge starts as much as for a session you open. The
  bridge never trusts a folder on your behalf.
- **A Grok answer is what it said after its last tool call.** Grok narrates between tool calls,
  and the plain `--output-format json` glues all of it together; the bridge reads `streaming-json`
  and the transcript's chunks and cuts at the last tool call. A turn that never spoke after its
  tools returns everything it said. A Grok transcript writes every streamed chunk as a line, so
  the bridge reads the last 8 MB of it rather than 2 MB when it looks for an answer.
- **Not verified: resuming a Grok session that a live panel process holds.** With the shim the
  panel path is used and the question does not arise. Without it (`CROSS_AGENT_UI_HOOK=off`, or the
  setting not made), the bridge resumes the session with a second `grok` process while the
  extension's own is still holding it. Grok keeps lock files in a session folder, which suggests
  the writes are serialized, but this was tested only against a session no live process held.
- **When the UI reflects changes** — without the shim attached, the bridge only appends a
  turn to a Claude, Codex or Grok session transcript, so the VS Code chat window doesn't update
  live (it shows up the next time that session is reopened). Setting up the shims from section
  3 renders those exchanges in the panel. Copilot uses its host protocol directly.
- **Chain-state propagation through the shim** — panel injection doesn't spawn a new child
  process, so environment variables like `CROSS_AGENT_CONVERSATION_ID` aren't passed to the
  peer. The envelope header carries the conversation id instead, so the peer can still
  continue the same conversation.
- **Concurrent writes** — the busy lock atomically serializes deliveries the bridge sends
  against each other, but it can't protect **a session a human is typing into directly in the
  VS Code chat window** (that side doesn't know about the lock). It's safer not to target a
  session the peer is actively typing in right now.
- **Delivery queue execution lives only inside the server process** — this is intentional.
  Reviving the queue from disk and resending would make a restart mean **resend, not resume**
  (a worker's delivery is a blocking child process, so the moment the process dies, the
  peer-turn's result is lost). That would make the peer redo the same work — worse than losing
  it outright. So **if the server shuts down, a delivery that hasn't gone out yet is not
  resumed** — an MCP server comes up separately per session (a Claude conversation, a Codex
  window, the app that launched the server), so before restarting, check `bridge_status` in
  that session to confirm `deliveries.pending` is empty. What follows instead eliminates the
  "there's an answer but it can't be read" case.
  - **A delivery record hits disk the moment it's accepted** (`~/.cross-agent/deliveries/`). An
    in-flight delivery lives at `in-flight/<delivery_id>.json`; a finished one, at
    `<delivery_id>.json` as before. Every state change (queued → delivering → awaiting-peer →
    delivered/failed) is written atomically via temp-file-then-rename. So even if the server
    holding a delivery shuts down along with its app, **any other server** can look that
    delivery up with `bridge_status(delivery_id)` and read the answer from the peer transcript
    the same way as before. A delivery whose server disappeared is only marked
    `is_orphaned: true` — it is **never resent.** A delivery orphaned while still queued was
    never received by the peer, so no answer is looked up from the transcript for it. A
    finished delivery can also be read via `reply_preview` in `bridge_status`'s
    `deliveries.earlier`, and a delivery another server has recorded as in progress shows up
    in `deliveries.in_flight_elsewhere`. A record carrying `is_return_status_only: true` is one
    whose answer — or failure notice — was never put into the sender's session, because that
    session had no available live return route. `reply_preview` holds the first 2,000 characters
    of the answer if there was one; the whole answer is read fresh from the peer's own transcript
    as `peer_transcript.answer`, so the record is a summary of it, not a copy. The payload and child environment variables are
    **never stored** — env holds every variable of this process.
    - Why the in-flight record lives in a subfolder: a server running a version prior to this
      feature treats any `deliveries/*.json` record without a `finished_at` as expired and
      deletes it. It never looks inside a subfolder.
    - Expiry: past `expires_at` — a delivery lease renewed by the running worker, together
      with its session busy lock — an in-flight record is treated as orphaned even if its pid is still
      alive (to guard against pid reuse). The file itself is deleted only after the TTL
      (default 7 days) has also passed from there — deleting it right at expiry would make the
      app go back to "I don't know" if it asks the next day. A finished record still follows
      `finished_at` + TTL as before.
  - **The answer is recovered from the peer's transcript.** Every agent writes each turn to
    JSONL, so even if the transport broke or the process died before the answer could be
    received, the answer itself is still on disk. If a delivery ends without an answer, the
    peer session's last assistant message is read back (`is_reply_recovered`). Since this
    isn't a resend, the peer is never made to redo the same work.
  - **If the peer is busy, it waits — it doesn't reject.** The agents already handle
    concurrent input — Claude waits for the current turn to finish before injecting, Codex
    and Grok queue it. But there's a limit, and past it the shim answers with
    `busy with another turn` / `already in flight`. That's the shim's own deadline, not ours,
    so instead of closing the delivery as failed, it's **retried after an interval** — for
    as long as the job timeout, or `CROSS_AGENT_PANEL_PATIENCE` on the panel path if that is
    longer. Any other error is treated as an outright failure.
  - **The recipient isn't told a message is waiting for it.** While the recipient is mid-turn,
    nothing appears in its session and the bridge keeps retrying the delivery; `bridge_status`
    lists deliveries by the server carrying them, not by the session they are for. If a retry
    succeeds after the turn ends, within the retry window above, the message arrives. If the
    window closes first, the request fails as **not delivered** (`is_undelivered`) — the
    recipient never received it, so there is no transcript to watch for an answer — and the
    bridge attempts a `DELIVERY FAILED` notice in the sender's panel saying so. Sending again
    is safe. The failure stays readable with `bridge_status(delivery_id=...)`. The failed
    request is not replayed.
  - **A long panel turn stays in progress.** Passing `CROSS_AGENT_PANEL_PATIENCE` sends one
    `PROGRESS (INFO)` notice to the sender's idle open panel, records `progress_note`, and keeps
    the request `awaiting-peer`. The final answer follows the usual reply delivery path when
    the turn ends. The progress notice has its own `progress_delivery_id`, separate from the
    final answer's `return_delivery_id`. No failure notice is sent just because time passed.
    INFO does not wait for a busy sender and is skipped if the final answer is already ready;
    the progress note remains readable on the request record.
  - **Even after giving up on the transport, the peer keeps working.** On the panel path, the
    peer is a session we neither spawned nor can stop, so a socket timing out doesn't mean the
    turn is over. When the transport actually breaks, it isn't closed immediately — instead,
    **the peer transcript is periodically re-checked** (every 15 seconds by default, up to 15
    minutes). It's picked up the moment the peer writes its answer. On the CLI path, that turn
    was our own child process. Only inactivity exceeding `timeout` kills its process group,
    recorded as `is_killed_by_timeout: true` and `killed by bridge timeout`; it closes without
    another wait. `peer_transcript.is_working` is false for that stopped request even if the
    killed process left an open turn in its transcript. Legacy wall-clock timeout records are
    reported the same way. The headless receipt states the idle policy and duration explicitly.
  - **Even if the shim misses the turn, the answer is picked up within 30 seconds.** The shim
    reports a turn finished when the app-server's completion event matches the turn id it
    itself started — but sometimes that id never matches (a turn queued behind another turn,
    or a case where the `turn/start` response's id differs from the id of the actual run).
    When that happens, the shim keeps answering "still running" for a full hour even though
    the answer is already complete on disk, and every subsequent delivery to that same session
    piles up behind the lock — five of them actually backed up that way once. So the worker
    breaks its socket wait into 30-second chunks and **in between each one, reads the peer
    transcript; if it finds a finished turn that echoed the request token back, it settles that
    right there as the answer** (`is_reply_confirmed_by_transcript`). A finished turn with no
    token isn't accepted, since it could belong to someone else's turn. The shim itself, too,
    treats the end of a turn whose message echoes its token as the end of its own turn,
    regardless of turn id.
  - **A turn stuck on an approval prompt is invisible in the transcript.** A turn waiting on a
    human click writes nothing, so from the record alone it's indistinguishable from a turn
    that's still working. Both the approval request and its answer pass through the shim
    (Claude: `control_request`/`control_response`; Codex: the `item/*/requestApproval` family),
    so the shim tracks them and reports via `bridge_status(delivery_id=...)`'s
    `peer_panel.awaiting_approval` exactly which prompt it's stuck on and for how many seconds.
  - **Every request carries a unique token, and a matching one coming back is how it's paired
    up.** The envelope carries `request: req_<epoch ms>_<6 random chars>`, and the reply
    instructions ask that it be echoed back verbatim on the last line. A matching token means
    it's that request's answer **regardless of timing**; a different token means it's the
    answer to a different question and is rejected. Because of the millisecond prefix, the
    token alone tells you when the request was sent. It's fine if the peer doesn't echo it —
    it just falls back to the timing rule below. **Cooperation is a bonus, not a requirement.**
  - **Only text written after the request is accepted as the answer.** Recovery pulls the
    "last utterance," so if the peer hasn't even seen the request yet, **text written before
    the question** comes back as the answer. In practice, a single paragraph written nine
    minutes earlier was once delivered as the answer to two different questions. An utterance
    that predates the delivery time isn't an answer, and in that case nothing is recovered at
    all.
  - **A recovered answer is labeled as such in the envelope.** Recovery pulls "the last
    utterance at that moment," and even a peer that's still working has a last utterance — a
    line noting what it's about to do next. Delivered without a label, that's indistinguishable
    from a finished report, and leads to acting on a report that was never actually made. This
    actually happened, which is why a recovered reply carries a `RECOVERED, NOT RECEIVED`
    warning and a `| recovered from transcript` marker in the header. **It also states why the
    transport failed** (a `transport failure:` line) — a panel rejecting the message and a
    socket exceeding its patience window call for different next steps, and a warning with no
    reason couldn't tell the two apart.

  The one remaining limitation is **the peer never producing an answer at all**, which no
  design can recover from.
- **A session in another VS Code window is only reachable if it's named explicitly** —
  specifying it via `session_id` delivers to that window's shim, but auto-selection only ever
  happens within this window (see section 3). A Claude, Codex or Grok session with no panel
  open in any window still goes through headless CLI resume, and gets rejected with
  `thread-store conflict` if some window's panel is holding onto that conversation. Under
  `CROSS_AGENT_DISABLE_HEADLESS=1` that send is refused instead.
- **A reply requires a live return route** — an available sender panel, or the Copilot caller's
  own verified SDK session on its existing host. Without one, read the answer from
  `bridge_status`; the bridge never starts another runtime to deliver it.
- **Automatic CLI session discovery is mtime-based.** If several sessions are open at once in
  the same directory, pinning the target with `pin_agent_session` is the more reliable choice.
  Copilot always requires an explicit target or a sticky pin.

## License

[MIT](LICENSE)
