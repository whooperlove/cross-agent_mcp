# cross-agent MCP

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

**Let your Claude Code session and your Codex thread talk to each other, live, without either one losing its memory.**

cross-agent MCP is a relay MCP server for two coding agents you already have running — an active
**Claude Code** session and an active **Codex** thread, in a plain terminal or in VS Code, it
doesn't matter which. Either one can hand a message to the other through `send_to_codex` /
`send_to_claude`, and the bridge finds each product's **currently active session** from the
transcript it leaves on disk and **resumes it** — instead of spawning a disposable new agent —
so both sides keep their full existing context. VS Code isn't required for any of this; it only
unlocks one extra feature, covered in [section 3](#3-registration): seeing the exchange render
live in the real chat panel instead of just landing in the transcript.

That's the difference from just running a second CLI by hand: neither side has to re-explain
the task, and neither one loses the conversation it was already having. A few things this is
useful for:

- **Get a second opinion without leaving your conversation.** Ask Codex to review or double-check
  Claude's plan, or the other way around, and keep working while it thinks.
- **Hand off a long task and keep going.** `send_to_*` is asynchronous — it queues the message
  and returns immediately. Whenever the peer's answer is ready, it arrives back as a new message
  in your own session.
- **Watch it happen, not just read a log.** With the two panel shims from
  [section 3](#3-registration) installed, both directions render in VS Code's real chat panel
  like any other message, instead of just appending a line to a transcript file.
- **Nothing is lost to a timeout.** Because nothing blocks, a peer turn that takes ten minutes is
  fine — the reply lands whenever it lands.

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
               Terminal or VS Code
                         │
     ┌────────────┬──────┴─────┬────────────┐
     │            │            │            │
  Claude       Claude        Codex        Codex
 session A    session B    thread C     thread D
     │            │            │            │
     └─────────── cross-agent MCP ──────────┘
                         │
                 session registry
          (~/.cross-agent/registry.json)
```

Any of these can reach any other through the same hub — Claude ↔ Codex across products, or
Claude ↔ Claude / Codex ↔ Codex within the same product (see the same-agent row in the table
below, and [section 6](#6-preventing-infinite-calls) for how that's gated).

| Direction | Tool | With panel shim | Without it (fallback) |
|---|---|---|---|
| Claude → Codex | `send_to_codex` | Inject `turn/start` into the panel's app-server | `codex exec resume <thread-id> --json` |
| Codex → Claude | `send_to_claude` | Inject a stream-json user message into the panel process | `claude -p --resume <session-id> --output-format json` |
| Claude → Claude¹ | `send_to_claude` | Inject a stream-json user message into the panel process | `claude -p --resume <session-id> --output-format json` |
| Codex → Codex¹ | `send_to_codex` | Inject `turn/start` into the panel's app-server | `codex exec resume <thread-id> --json` |

¹ Same-agent rows need an explicit target — `allow_same_agent=true` or a `session_id` for Claude,
a `session_id` for Codex — see [section 6](#6-preventing-infinite-calls).

With the shim attached, the exchange **renders directly in the real VS Code panel** (see section 3).

The exchange is **asynchronous**. `send_to_*` queues the message and returns immediately — it does not carry the peer's reply. A background worker runs the peer's turn, and once a reply exists, it's **delivered to the sender's session as a new message**. Because nothing blocks, neither session is locked while the peer's turn runs, and a turn that takes several minutes won't be lost to a timeout.

```
send_to_codex ──▶ [outbox queue] ──▶ Codex turn (minutes)
     │                                   │
returns immediately                answer generated
  (delivery_id)                         │
                                        ▼
                  delivered to the Claude session as a "BRIDGE REPLY" message
```

#### Reply address

Since the reply needs somewhere to land, the sender must **know its own session precisely.** This isn't inferred — because the shim sits between the extension and the agent, the MCP server is a descendant of its own shim, and **the shim whose pid appears in its own ancestor chain is exactly the conversation hosting it.** That's a certainty, not a guess.

A pin must not substitute for this. A pin records "where to **send**," not "who **I am**." When it once worked that way, a stale pin got used as the reply address and replies landed in the wrong session.

Codex needs one more level of precision. All the Codex threads in one window **share** a single app-server and a single MCP server, so the process tree can only tell you "this window," not which thread within it (back when the most-recently-active thread was picked instead, four replies landed in threads that had never asked anything). Instead, Codex attaches `x-codex-turn-metadata` (thread id, turn id) to every MCP call, so the bridge uses **the id the calling thread declares about itself** as the reply address. When this value is present it takes priority over inference. Claude Code runs a separate process per conversation, so the process tree alone is enough there.

The address is also written into the envelope — like the From line of an email.

```
=== CROSS-AGENT BRIDGE MESSAGE ===
from: Claude Code (peer AI agent, not the human user)
reply-to: claude session 058a16bc-3a77-4604-a328-9409c391f918
conversation: conv_7bc3806dc3a8 | hop 1/4
```

Since the bridge delivers the reply on its own, this line doesn't **establish** the reply. It earns its keep when the automatic path fails, and when the peer sends back a **new request** — it can target that exact session without re-inferring what's active on this side.

Reply delivery runs **from the sender session's directory.** Claude transcripts are stored under their own project directory, so resuming from the directory the request was headed toward produces `No conversation found` even when the session itself is fine.

---

## 1. Requirements

- macOS / Linux, Python 3.10+
- `claude` CLI (Claude Code 2.x), `codex` CLI (0.146+)
- Both CLIs logged in

## 2. Installation

```bash
cd ~/project/cross-agent_mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x run-server.sh
```

## 3. Registration

Register both at the **user (global) level.** Pass along the environment variables so that sessions the bridge newly creates run without sandbox/approval friction.

### Claude Code

```bash
claude mcp add cross-agent -s user \
  -e CROSS_AGENT_CODEX_SANDBOX=danger-full-access \
  -e CROSS_AGENT_CLAUDE_PERMISSION_MODE=bypassPermissions \
  -- ~/project/cross-agent_mcp/run-server.sh
```

This is written into the top-level `mcpServers` of `~/.claude.json`, so it's available in every project. To attach it to just one project use `-s local`; to share it via the repository, use a project-root `.mcp.json`.

```json
{
  "mcpServers": {
    "cross-agent": {
      "command": "~/project/cross-agent_mcp/run-server.sh",
      "env": {
        "CROSS_AGENT_CODEX_SANDBOX": "danger-full-access",
        "CROSS_AGENT_CLAUDE_PERMISSION_MODE": "bypassPermissions"
      }
    }
  }
}
```

### Codex

```bash
codex mcp add cross-agent \
  --env CROSS_AGENT_CODEX_SANDBOX=danger-full-access \
  --env CROSS_AGENT_CLAUDE_PERMISSION_MODE=bypassPermissions \
  -- ~/project/cross-agent_mcp/run-server.sh
```

This adds the following to `~/.codex/config.toml` (Codex only supports global config).

```toml
[mcp_servers.cross-agent]
command = "~/project/cross-agent_mcp/run-server.sh"
default_tools_approval_mode = "approve"   # so the UI doesn't show an approval prompt every time (added manually)

[mcp_servers.cross-agent.env]
CROSS_AGENT_CLAUDE_PERMISSION_MODE = "bypassPermissions"
CROSS_AGENT_CODEX_SANDBOX = "danger-full-access"
```

`default_tools_approval_mode` has no corresponding flag on `codex mcp add`, so it's added directly to config.toml. Valid values are `auto` / `prompt` / `writes` / `approve`; use `approve` to stop the approval prompt from popping up every time (`auto` kept asking). This does **not** fix the cancellation problem with headless `codex exec` (see section 9). Clicking **"Always allow"** once on the UI prompt has the same effect.

### Turning off the approval prompt (Claude Code)

Claude Code asks for approval on every MCP tool call. Add a server-level rule to `~/.claude/settings.json` (**a single server name**, not a per-tool list or a `*` wildcard).

```json
{
  "permissions": {
    "allow": ["mcp__cross-agent"]
  }
}
```

> [!WARNING]
> The two environment variables above **turn off the safety rails for an agent reached through the bridge.**
> Claude edits files and runs commands without confirmation, and newly created Codex sessions
> run without a sandbox. Use this only for trusted local work.
> To revert, drop both `-e`/`--env` arguments and re-register; that restores the defaults (`read-only` / the agent's default permissions).

> [!NOTE]
> Right after registering, you need to **reload the VS Code window** or start a new session for the tool to be picked up.
> MCP servers connect only at session start.

### IDE panel integration (bidirectional)

Everything above works the same from a plain terminal — this section is optional, and only
matters if you also use the VS Code extensions' chat panels.

The CLI resume path (`codex exec resume` / `claude -p --resume`) appends a turn to the session history, so context is preserved, but it **doesn't show up in the VS Code panel.** The panel's session lives only inside the child process the extension spawned and connected to directly over stdio, and there's no way in from outside.

Inserting the two shims into the middle of that pipe solves it.

```
VS Code extension ──stdio──▶ codex-shim.sh  ──stdio──▶ real codex app-server
VS Code extension ──stdio──▶ claude-shim.sh ──stdio──▶ real claude (stream-json)
                                  ▲
                                  │ unix socket
                          cross-agent MCP  ──▶ inject message ──▶ rendered in panel
```

Add these to VS Code user settings and **reload the window.**

```json
"chatgpt.cliExecutable": "~/project/cross-agent_mcp/codex-shim.sh",
"claudeCode.claudeProcessWrapper": "~/project/cross-agent_mcp/claude-shim.sh"
```

| | Codex | Claude Code |
|---|---|---|
| Setting key | `chatgpt.cliExecutable` (**replaces** the binary) | `claudeCode.claudeProcessWrapper` (`<wrapper> <real-path> <args>`) |
| Call intercepted | plain `app-server` | `--input-format stream-json` sessions |
| Injection method | JSON-RPC `turn/start` (id in the `xagent-` namespace) | stream-json `{"type":"user",...}` |
| Session id source | `thread/started` · request params | argv `--resume=` · `system/init` |
| Human input observed | `turn/start` · `turn/steer` sent by the extension | `{"type":"user"}` sent by the extension |
| Shown in panel | user message + response | user message + response |
| Setting status | marked "DEVELOPMENT ONLY" | an official setting |

Shared rules:

- Passes every byte straight through, and intercepts **only panel-session calls**
  (`--version`, `login`, `app-server daemon`, `claude -p`, etc. exec straight to the real binary)
- Auto-discovers the real binary inside the extension directory
  (can be overridden with `CROSS_AGENT_REAL_CODEX` / `CROSS_AGENT_REAL_CLAUDE`)
- On any failure, it execs the real binary as-is (fail-open)
- The shim records its own pid ancestor list in `~/.cross-agent/panels/<agent>-<pid>.json`.
  The bridge picks the **shim that shares an ancestor with itself**, so even with multiple
  windows open it targets exactly "this IDE instance"
- The Claude shim waits for the current turn to finish before injecting, if the user is mid-conversation
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

#### When the panel has no conversation open

Even when the panel is only showing a conversation list, there's a live process behind it. Falling back to the CLI here means the requester gets an answer, but **the panel stays empty**, making it look like the bridge did nothing. So the shim **opens a new conversation in the panel** and puts the message there instead.

- Codex: creates a thread with `thread/start`. The app-server broadcasts a `thread/started` notification, so the extension picks up the thread and renders it
- Claude: just writes a user message to the panel process that's running without a session. The CLI starts a new conversation and the session id is captured from `system/init`

When the receipt's `will_create_session` is `true`, this is the conversation that will be opened. Codex also sets a title of the form **"sender: start of the message"** via `thread/name/set`; otherwise it would just sit in the list as "New chat" with no way to tell which conversation it is.

The new conversation shows up in the list with an unread marker, but **the panel doesn't automatically open it.** The app-server protocol has no notification that moves the client to a specific conversation, and the extension's `vscode://` deep link (the `/local/<thread-id>` route) **can't target a specific window** — it moves the Codex panel of every open VS Code instance at once. So it wasn't adopted.

#### Which conversation tab it goes to

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

#### Conversations in another VS Code window

The shim socket is an ordinary unix socket and isn't tied to a window. What process-ancestor detection determines is **"which window," not "can it be reached."** So the rule splits into two.

| Case | Behavior |
|---|---|
| Auto-selected, no `session_id` | Picks **only within this window** — barging into another window's conversation uninvited would be a problem |
| Explicit `session_id` | Searches this window first, then, if not found, **looks across other windows and delivers to that window's shim** |

`bridge_status`'s `ide_panels.<agent>.other_window_sessions` shows conversations open in other windows. They're not candidates for auto-selection, but they're reachable if named explicitly via `session_id`.

Without this distinction, a session in another window would fall back to a headless CLI resume, which the CLI rejects with `thread-store conflict: already has an active writer` if that window's live panel is still holding onto the conversation. In other words, **reaching outside the window for an explicitly named session isn't a convenience — it's the only path that actually succeeds.**

`CROSS_AGENT_UI_HOOK` selects the behavior — `auto` (default: use it if present, fall back to CLI otherwise), `off` (always CLI), `require` (fail instead of silently falling back if the panel can't be found).

> [!WARNING]
> The shim is a process wedged between the extension and the agent. It can break when the extension updates, and `chatgpt.cliExecutable` is an application-scoped setting the extension itself marks "DEVELOPMENT ONLY."
> To revert, delete that setting line and reload the window.

### Status check

```bash
./run-server.sh --check
```

Prints who's running it, both CLI paths, the currently resolved active session, and the settings in effect, then exits. Run with no arguments, it comes up as an MCP stdio server and waits on stdin (this is normal — exit with Ctrl-C).

---

## 4. Tools

| Tool | Description |
|---|---|
| `send_to_codex(message, ...)` | Sends a message to the active Codex thread. **Asynchronous — the response isn't carried back** |
| `send_to_claude(message, ...)` | Sends a message to the active Claude session. **Asynchronous — the response isn't carried back** |
| `list_agent_sessions(agent, scope, cwd, limit)` | List of sessions the bridge can find (newest first, including active status) |
| `bridge_status(cwd, scope, delivery_id)` | Diagnostics: who's running, the resolved session, settings, lock state, and **deliveries in flight**. Given a `delivery_id`, it re-reads that one delivery's peer transcript (`peer_transcript`) and panel state (`peer_panel`: whether a turn is running, **whether it's stuck on an approval prompt**) and reports both |
| `pin_agent_session(agent, session_id, cwd)` | Pins a specific session (by id or conversation name). While pinned, no new conversation is opened. Leave it empty to unpin |

Common `send_to_*` parameters:

| Name | Default | Meaning |
|---|---|---|
| `message` | (required) | The content to send to the peer agent. The peer can't see this side's conversation, so write it self-contained |
| `session_id` | auto-discovered | Targets a specific session. **Session id or conversation name.** If nothing matches, it fails instead of creating a new one |
| `new_session` | `false` | Forces a new session even if one is active |
| `scope` | `cwd` | `cwd` = same directory and its subdirectories, `tree` = up through parent directories too, `any` = everything |
| `cwd` | the server's working directory | Basis for discovery and where a new session gets created |
| `timeout` | `600` | **Budget (seconds) for the peer's turn itself.** Enforced by the worker; it doesn't make the caller wait |
| `conversation_id` | auto-generated | Continues an existing bridge conversation, sharing its hop budget |
| `raw` | `false` | Delivers the raw text with no bridge header |

`send_to_claude` additionally takes `allow_same_agent` (default `false`) — a Claude session
messaging another Claude session is refused unless this is set, or an explicit `session_id` is
given. `send_to_codex` has no such flag; reaching another Codex thread requires an explicit
`session_id` (see [section 6](#6-preventing-infinite-calls)).

The return value of `send_to_*` is a **receipt**, not an answer.

| Field | Meaning |
|---|---|
| `delivery_id` | This delivery's identifier. Look up its status in `bridge_status`'s `deliveries` |
| `accepted` | Successfully queued |
| `note` | States that there's no response yet, and that it'll arrive later as a separate message |
| `reply_lands_in_session` | The sender session id the peer's answer will be delivered to. `null` means there's nowhere for the answer to return to |
| `queue_depth` | Number of deliveries already waiting ahead of this one for the same target session |
| `will_create_session` | Whether a new conversation will be opened because no existing session was found |

---

## 5. Session resolution rules

On a `send_to_*` call, the target session is determined in the following order.

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
     - Only counted as "active" if its last record falls within the active window (default 240 min)
     - Sort priority: ① directory match (exact > subdirectory > parent) ② most recently recorded
       Parent directories are only candidates under scope='tree' — since the home directory
       is a parent of every project, a session opened at ~ must not be able to hijack an
       arbitrary project
4. If no session satisfies the above → create a new session
     - Claude: claude -p --session-id <new uuid> ...
     - Codex:  codex exec --json -C <cwd> ... (id recovered from thread.started)
     - The created session gets pinned in the registry, becoming the resume target from the next call on
```

In short: **if there's an active session, it continues the context; if not, it creates one and reuses it from then on.**

## 6. Preventing infinite calls

It's blocked in three layers.

1. **Hop budget** — at most `MAX_HOPS` (default 4) per conversation. `conversation_id` propagates to child processes as an environment variable, so an A→B→A→B chain automatically shares the same budget. Rejected once exceeded.
2. **Busy lock** — prevents two turns from overlapping in the same session. Agent CLIs allow only one writer per transcript, so this isn't courtesy, it's mandatory. It's acquired atomically in `~/.cross-agent/locks/` with `O_EXCL`, so there's no race between "check" and "acquire." It's released only while its own token is still present, and locks held by dead processes are reclaimed automatically. **Since the move to async, this lock is a reason to wait in line, not a reason to reject** — the worker waits for the lock to free up and then delivers. The sender's own session is no longer locked at all, since nobody's waiting on it, so a relay coming back around can't create a deadlock.
3. **Self-call guard** — by default, an agent refuses to relay into another session of its *own*
   kind: Claude cannot reach another Claude session, and Codex cannot reach another Codex thread.
   The caller is identified from the parent process chain, not a self-reported label. This exists
   because the two peer tools (`send_to_codex` / `send_to_claude`) are meant to cross from one
   product to the other — routing Claude into Claude by mistake should fail loudly instead of
   silently starting a same-agent relay.
   - **Claude → Claude** is allowed either by passing `allow_same_agent=true` on `send_to_claude`
     (auto-discovers another active Claude session, excluding the caller's own), or by naming an
     explicit `session_id` (which alone is also enough to pass the guard, with or without the flag).
   - **Codex → Codex** has no `allow_same_agent` flag — `send_to_codex` doesn't expose one — so the
     only way to reach another Codex thread is to name it explicitly with `session_id`.
   - Either way, **auto-discovery never picks the caller's own current session** as the target, so
     a same-agent relay can't be routed back into itself.
4. **A delivery doesn't outlive the server** — since the CLI runs in its own process group, it survives even if the server dies. It would then keep writing to the peer session and store with nobody watching, and since the busy lock **decides life or death by the server's pid**, it can no longer protect that session. If a re-request comes in, a second agent attaches to the same file — in practice, two `claude -p --resume` processes once ran concurrently against the same session after a window reload. On server shutdown (atexit, SIGTERM, SIGINT, SIGHUP), the process groups of any in-flight deliveries are cleaned up together with it.

Every delivered message carries a header with the sender, conversation ID, and hops remaining. The peer's final message is delivered back to the sender's session with a `BRIDGE REPLY` header, and **this reply doesn't consume a hop** — it's closing out a hop the request already paid for. Only new requests spend from the budget.

## 7. Environment variables

| Variable | Default | Description |
|---|---|---|
| `CROSS_AGENT_HOME` | `~/.cross-agent` | Location of the registry, locks, and logs |
| `CROSS_AGENT_ACTIVE_WINDOW_MIN` | `240` | Maximum elapsed time (minutes) for a session to still count as active |
| `CROSS_AGENT_MAX_HOPS` | `4` | Maximum number of relays per conversation |
| `CROSS_AGENT_TIMEOUT` | `600` | Budget (seconds) for the peer's turn itself. Doesn't make the caller wait |
| `CROSS_AGENT_DELIVERY_TTL` | `604800` | How long finished delivery records are kept (seconds, default 7 days) |
| `CROSS_AGENT_SCOPE` | `cwd` | Default discovery scope (`cwd` / `tree` / `any`) |
| `CROSS_AGENT_UI_HOOK` | `auto` | Codex panel injection (`auto` / `off` / `require`) |
| `CROSS_AGENT_REAL_CODEX` | (auto-discovered) | The real codex binary for the shim to wrap |
| `CROSS_AGENT_REAL_CLAUDE` | (auto-discovered) | The real claude binary for the shim to wrap |
| `CROSS_AGENT_CODEX_SANDBOX` | `read-only` | Sandbox for **newly created** Codex sessions (`read-only` / `workspace-write` / `danger-full-access`) |
| `CROSS_AGENT_CLAUDE_PERMISSION_MODE` | (unset) | `--permission-mode` passed on Claude calls (`acceptEdits` / `bypassPermissions` / `plan`, etc.) |
| `CROSS_AGENT_CODEX_MODEL` / `CROSS_AGENT_CLAUDE_MODEL` | (unset) | Force a specific model |
| `CROSS_AGENT_CLAUDE_BIN` / `CROSS_AGENT_CODEX_BIN` | `claude` / `codex` | CLI path |
| `CROSS_AGENT_CODEX_SCAN_LIMIT` | `2000` | Safety cap on Codex rollout scanning (applies only to scope=`any`) |
| `CROSS_AGENT_SELF` | (auto-detected) | Force which agent is treated as the caller |
| `CROSS_AGENT_DEBUG` | (unset) | DEBUG logging when set to any value |

To set a value in Claude Code: `claude mcp add cross-agent -s user -e KEY=VALUE -- <script>`;
in Codex: `codex mcp add cross-agent --env KEY=VALUE -- <script>`.

`CROSS_AGENT_CODEX_SANDBOX` only applies to **newly created** Codex sessions.
`codex exec resume` has no sandbox argument, so resuming an existing session keeps whatever
setting it was originally started with. `CROSS_AGENT_CLAUDE_PERMISSION_MODE`, on the other
hand, applies to both new sessions and resumed ones.

## 8. Verification

```bash
# Unit-level checks of locks/pins/scope/timeout (consumes no agent turns)
PYTHONPATH=src .venv/bin/python tests/unit_guards.py

# Shim pass-through, injection, and panel rendering checks (no VS Code config needed)
PYTHONPATH=src .venv/bin/python tests/shim_roundtrip.py          # 1 Codex turn
PYTHONPATH=src .venv/bin/python tests/claude_shim_roundtrip.py   # 2 Claude turns

# Protocol handshake + discovery + all 3 guards (consumes no agent turns)
PYTHONPATH=src .venv/bin/python tests/smoke_mcp.py

# 5 real round trips (actually consumes Codex/Claude turns)
PYTHONPATH=src .venv/bin/python tests/live_roundtrip.py
```

Logs go to `~/.cross-agent/logs/bridge.log`. The panel shim runs inside the extension's stdio
and has no terminal, so it writes separately to `~/.cross-agent/logs/shim-claude.log` /
`shim-codex.log` — this is where you'll find when an injected turn was accepted, which turn id
it finished as, and when an approval prompt appeared and was answered.

## 9. Known limitations

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
- **When the UI reflects changes** — without the shim attached, the bridge only appends a
  turn to the session transcript, so the VS Code chat window doesn't update live (it shows up
  the next time that session is reopened). Setting up both shims from section 3 renders both
  directions in the panel.
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
    in `deliveries.in_flight_elsewhere`. The payload and child environment variables are
    **never stored** — env holds every variable of this process.
    - Why the in-flight record lives in a subfolder: a server running a version prior to this
      feature treats any `deliveries/*.json` record without a `finished_at` as expired and
      deletes it. It never looks inside a subfolder.
    - Expiry: past `expires_at` — the longest this delivery could legitimately still be
      running (attempt count × (busy-wait + turn, each with its own patience) + transcript
      watch + margin) — an in-flight record is treated as orphaned even if its pid is still
      alive (to guard against pid reuse). The file itself is deleted only after the TTL
      (default 7 days) has also passed from there — deleting it right at expiry would make the
      app go back to "I don't know" if it asks the next day. A finished record still follows
      `finished_at` + TTL as before.
  - **The answer is recovered from the peer's transcript.** Both agents write every turn to
    JSONL, so even if the transport broke or the process died before the answer could be
    received, the answer itself is still on disk. If a delivery ends without an answer, the
    peer session's last assistant message is read back (`is_reply_recovered`). Since this
    isn't a resend, the peer is never made to redo the same work.
  - **If the peer is busy, it waits — it doesn't reject.** Both agents already handle
    concurrent input — Claude waits for the current turn to finish before injecting, Codex
    queues it. But there's a limit, and past it the shim answers with
    `busy with another turn` / `already in flight`. That's the shim's own deadline, not ours,
    so instead of closing the delivery as failed, it's **retried after an interval** (within
    the job timeout). Any other error is treated as an outright failure.
  - **Even after giving up on the transport, the peer keeps working.** On the panel path, the
    peer is a session we neither spawned nor can stop, so a socket timing out doesn't mean the
    turn is over. So when a delivery times out, it isn't closed as a failure — instead,
    **the peer transcript is periodically re-checked** (every 15 seconds by default, up to 15
    minutes). It's picked up the moment the peer writes its answer. On the CLI path, that turn
    was our own child process and the timeout already killed its process group, so there's
    nothing left to wait for — it closes immediately, with no waiting.
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
  happens within this window (see section 3). A session with no panel open in any window still
  goes through headless CLI resume, and gets rejected with `thread-store conflict` if some
  window's panel is holding onto that conversation.
- **A reply wakes the sender session** — delivering the answer creates a new turn in the
  sender's session. If a human is doing something else in that session, it interrupts that
  flow.
- **Session discovery is mtime-based.** If several sessions are open at once in the same
  directory, pinning the target with `pin_agent_session` is the more reliable choice.

## License

[MIT](LICENSE)
