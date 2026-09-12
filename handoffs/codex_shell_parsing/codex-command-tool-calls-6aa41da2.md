# Codex command tool calls
_2026-09-11 10:26:27_

### User
Original custom instructions no longer available

### User
Ok so in codex it seems like (from what I can tell from the app server event stream) codex wraps every one of its command tool calls with a login exec shell, so that every one of the agents calls stay stateless. I'm having a hard time figuring out if there is some type of method to suppress the quoting mechanism, and for it to just output the call that the agent made and not the raw shell command that the harness used.  And then really, if I have to settle for regexing the call, I guess I'm going to need a good regular expression to remove the quotes so that the command actually makes sense to a syntax highlighter.

### Assistant
I’m going to separate two things here: whether Codex/app-server exposes a cleaner “logical command” field already, and—if not—how to unwrap the login-shell quoting without mangling real shell syntax.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
_Interpreting the request: I’m identifying the needed behavior around shell command wrapping and quoting. I’ll check authoritative documentation, compare the observed execution format, and explain whether the wrapper is configurable without exposing implementation details._

_Interpreted the request_

### Assistant
_Searched 8 websites_

### Assistant
The protocol docs confirm the important bit: `commandExecution` is already a **UI-facing normalized item**, but its `command` field is still the executable representation—OpenAI even documents examples like `bash -lc ls`. There isn’t a documented second field containing “exactly what the model typed before Codex wrapped it.”

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
_Refining command handling: I’m focusing on extracting the actual script from its login-shell wrapper. Rather than removing quotes with a regex, I’m parsing the outer command into arguments and selecting the script following `-c` or `-lc`; this preserves nested quoting correctly._

_Refined command handling_

### Assistant
_Searched 1 website_

### Assistant
_Worked for 1m 50s_

### Assistant
Yeah. Your read is basically right, with one distinction: **the `bash -lc` wrapper isn't what makes the calls stateless; spawning a fresh process for each ordinary shell tool call does.** The login shell is mainly there to recreate the expected shell/profile semantics for that fresh process. Codex now actually has an `allow_login_shell` setting; setting it to `false` prevents `login = true` shell invocations, but that only gets rid of the login-shell behavior—it does **not** make the shell persistent or cause App Server to expose a cleaner command.

And unfortunately, the current stable App Server representation seems to confirm what you're seeing. A `commandExecution` item exposes:

```text
{id, command, cwd, status, commandActions, aggregatedOutput?, exitCode?, durationMs?}
```

There is no documented `argv`, `script`, or `originalToolArguments` field. OpenAI's own JSON example literally shows `"command":"bash -lc ls"`. So **there does not appear to be a supported App Server option that says "give me the model's shell payload rather than the harness's executable command."**

There is one interesting possible escape hatch: recent Codex changelog entries mention **raw App Server events** and **raw response completions in TypeScript envelopes**. The stable App Server docs don't document a raw-tool-call contract yet, though, so I wouldn't couple your normal UI to it unless you're pinning the Codex binary/version. If those raw events contain the Responses function-call arguments, *that* would be the proper place to get the pre-wrapper model call.

### Don't regex the quotes if you can avoid it

If you're already doing any processing server-side in Python, `shlex` is almost exactly the operation you need.

Suppose App Server gives you this monstrosity:

```sh
/bin/bash -lc 'rg -n '"'"'foobar'"'"' src && sed -n '"'"'1,120p'"'"' foo.py'
```

Don't strip the first and last `'`. Parse the **outer command line**:

```py
import shlex
import os

SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}

def display_command(raw: str) -> str:
    try:
        argv = shlex.split(raw, posix=True)
    except ValueError:
        return raw

    if len(argv) >= 3:
        shell = os.path.basename(argv[0])

        if shell in SHELLS and argv[1] in {"-c", "-lc", "-cl"}:
            return argv[2]

    return raw
```

That converts the thing above directly into:

```sh
rg -n 'foobar' src && sed -n '1,120p' foo.py
```

**That is exactly what you want to feed Monaco/Shiki/whatever shell syntax highlighter you're using.**

The important advantage is that `shlex.split()` doesn't merely remove the quotes. It reverses the shell's argument-serialization layer. So stuff like:

```sh
'"'"'
```

or:

```sh
'\''
```

gets interpreted as part of the outer quoting scheme rather than showing up as quote garbage in your highlighted command.

### If you absolutely want regex

I'd use regex only to **recognize the wrapper**, not decode its contents:

```regex
^(?:\S*/)?(?:bash|zsh|sh|dash|ksh)\s+-(?:lc|cl|c)\s+(.+)$
```

Then capture group 1 is the serialized script argument.

But you still need to shell-unquote that group afterward. Once you start writing replacements for:

```text
'\''
'"'"'
\"
\\
```

you're halfway to implementing `shlex` badly.

So for your harness I'd do:

```text
App Server commandExecution.command
        ↓
shlex.split()
        ↓
detect [shell, -lc/-c, payload]
        ↓
payload
        ↓
syntax highlighter
```

And keep the **raw command separately** in case you eventually want an "execution details" view.

Also, `commandActions` is worth retaining, but I wouldn't substitute it for this. App Server exposes it as semantic execution metadata, not as a documented lossless representation of the model's original shell source.

So yes: **I'd unwrap it rather than trying to configure Codex around it.** Unless that newer raw-response event gives you the actual function-call arguments, `shlex.split(command)[2]` after validating the shell wrapper is probably the cleanest solution. memcite

### User
Is that Basically what the tui/app/vscode extension does? I guess I realize that it would probably be easier to figure out what the TUI is doing because that's the only one of the three that is actually open source

### Assistant
_Reviewing Codex architecture: I’m checking whether the TUI and editor integrations share a common event-handling pattern. I’ll compare their open-source implementation, especially how streamed updates are routed and rendered across interfaces._

_Reviewed Codex architecture_

### Assistant
Yeah—that’s the right place to look. I’m checking the TUI’s actual render path now, because it can tell us whether it reconstructs a display command from structured args or just prettifies the same wrapped string you’re seeing from app-server.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
Tool parameters:
- uri: skills://plugins/github/github/skill.md
- start_line: 1
- num_lines: 200

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
Tool parameters:
- query: search

### Tool
The output of this plugin was redacted.

### Assistant
_Searched 2 websites_

### Assistant
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
Tool parameters:
- query: pull

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
_Inspected command execution and parsing code with pull request metadata_

### Tool
The output of this plugin was redacted.

### Assistant
_Comparing implementations: I’m finding that the TUI does essentially this: it parses structured argv, recognizes shell wrappers, extracts the script, and renders a normalized command. Editor clients likely follow the same semantic model, though their closed implementations aren’t directly verifiable._

_Compared implementations_

### Assistant
_Searching the web_

### Assistant
_Worked for 54s_

### Assistant
**For the TUI, yes—almost exactly.** I checked current `openai/codex` main, and it literally has this display-cleanup path in `codex-rs/tui/src/exec_command.rs`:

```rust
pub(crate) fn strip_bash_lc_and_escape(command: &[String]) -> String {
    if let Some((_, script)) = extract_shell_command(command) {
        return script.to_string();
    }
    escape_command(command)
}
```

So the TUI explicitly says, essentially:

> “If this argv is a shell wrapper, show me the inner script instead of the wrapper.”

Otherwise it shell-escapes/joins the argv for display. file

And `extract_shell_command()` eventually recognizes an argv shaped like:

```text
["bash", "-lc", "rg -n 'foo' src"]
["zsh", "-lc", "rg -n 'foo' src"]
["/bin/bash", "-c", "rg -n 'foo' src"]
```

and returns the third element—the actual script. Current main recognizes Bash, Zsh, and `sh`, with either `-lc` or `-c`. file

So this:

```text
/bin/bash -lc 'rg -n '"'"'foobar'"'"' src'
```

becomes:

```sh
rg -n 'foobar' src
```

for presentation.

### And there's an even more relevant part for you

The TUI also has this function:

```rust
pub(crate) fn split_command_string(command: &str) -> Vec<String> {
    let Some(parts) = shlex::split(command) else {
        return vec![command.to_string()];
    };

    match shlex::try_join(parts.iter().map(String::as_str)) {
        Ok(round_trip)
            if round_trip == command
                || (!command.contains(":\\")
                    && shlex::split(&round_trip).as_ref() == Some(&parts)) =>
        {
            parts
        }
        _ => vec![command.to_string()],
    }
}
```

That's **extremely close to your exact situation**. file

It takes a rendered command string, uses `shlex::split()` to recover argv, and then importantly **round-trip checks it** before trusting the parse. That's nicer than the quick Python version I gave you because it avoids accidentally mangling something where POSIX `shlex` isn't a faithful parser—especially Windows commands.

So rather than inventing your own regex, I'd basically clone Codex's own strategy:

```python
import os
import shlex

SHELLS = {"bash", "zsh", "sh"}

def codex_display_command(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return command

    # Don't trust shlex unless it round-trips reasonably.
    try:
        round_trip = shlex.join(parts)
        if shlex.split(round_trip) != parts:
            return command
    except ValueError:
        return command

    if len(parts) == 3:
        shell = os.path.basename(parts[0])

        if shell in SHELLS and parts[1] in ("-lc", "-c"):
            return parts[2]

    return command
```

The history is even more amusing: there was literally a Codex PR titled **“Strip zsh -lc wrapper from TUI command headers”** because the TUI originally only stripped literal `bash -lc`; the PR generalized that behavior to zsh and absolute shell paths.

So your instinct was dead on:

```text
raw execution command
        ↓
recover argv
        ↓
recognize shell wrapper
        ↓
extract script argument
        ↓
render/highlight script
```

**That is what Codex itself does in the TUI.**

For the desktop app and VS Code extension, I wouldn't claim they're using that exact Rust function because those clients aren't open-source in the same way. But conceptually I'd expect the same presentation layer: don't show the execution transport/wrapper when a cleaner logical command can be derived. App Server itself exposes command execution as a higher-level item, while the TUI independently performs this display normalization.

And this changes my recommendation slightly: **don't regex it. Port `split_command_string()` + `extract_shell_command()` from Codex.** Then your UI is behaving essentially identically to the official TUI, which is probably exactly the compatibility target you want. memcite

### User
It should more or less be getting the same data that the app server emits,  only the app server wraps it in json rpc basically

### Assistant
Yeah—that’s the architecture I’d expect too. The one caveat I’m checking is whether app-server is truly a thin JSON-RPC transport over the core events, or whether it also performs a normalization step before emitting them.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
Tool parameters:
- query: fetch

### Tool
The output of this plugin was redacted.

### Assistant
_Searched 1 website_

### Assistant
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
_Investigating GitHub sources: I’m checking the repository directly because the earlier search didn’t return useful results. I’ll locate the relevant event definitions and inspect the surrounding implementation to answer accurately._

### Assistant
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
Tool parameters:
- uri: /response/
- query: ExecCommandBegin

### Tool
The output of this plugin was redacted.

### Assistant
Tool parameters:
- uri: /response/
- query: item_event_to_server_notification

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
Tool parameters:
- uri: /response/
- query: ThreadItem::CommandExecution

### Assistant
Tool parameters:
- uri: /response/
- query: strip_bash_lc_and_escape

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
_Fetching TUI Command Lifecycle Module_

### Assistant
_Worked for 40s_

### User
It doesn't perform them normalization step, It's definitely the tui that does from the core harness.

### Assistant
_Worked for a few seconds_

### Assistant
Right. **For the command text specifically, you're correct.** My caveat about App Server possibly doing the display normalization was unnecessary.

The source makes the boundary pretty clear:

```text
core execution event
        ↓
app-server
        ↓
ThreadItem::CommandExecution {
    command: String,
    command_actions: ...
}
        ↓
JSON-RPC notification
```

Then the **TUI** receives that `ThreadItem::CommandExecution` and separately does:

```rust
let command = split_command_string(command);
let command_display = strip_bash_lc_and_escape(&command);
```

before rendering it. file

And the actual normalization implementation lives squarely under `codex-rs/tui`:

```rust
pub(crate) fn strip_bash_lc_and_escape(command: &[String]) -> String {
    if let Some((_, script)) = extract_shell_command(command) {
        return script.to_string();
    }
    escape_command(command)
}
```

file

So what you're consuming from App Server is essentially the same underlying execution information **before the TUI presentation layer gets its hands on it**.

There is some App Server transformation in the broader sense—it turns core `EventMsg`s into the canonical JSON-RPC `ThreadItem` lifecycle, suppresses/deprecates some old events, etc.—so I wouldn't call App Server *literally* a serializer over every core event. The source explicitly shows it translating core events into server notifications and treating old `ExecCommandBegin/End` events specially. file

But for the thing you actually care about:

**App Server does not clean up `/bin/bash -lc ...`; the TUI does.**

Which is actually great for your implementation, because you can just replicate the official presentation boundary:

```text
App Server command string
    ↓
split_command_string()
    ↓
strip_bash_lc_and_escape()
    ↓
syntax-highlight resulting shell script
```

No heuristic guessing about what the desktop app *probably* does is necessary. You have the exact TUI implementation sitting there as your reference.

### User
That's what I'm wondering if im piping directly from the tui, why is jsonrpc even necessary? It seems like a byte stream would be way more efficient... Even if it was just messagepack serialization

### Assistant
Yeah—if the only consumer were the TUI, JSON-RPC would be overkill. The reason to keep it is less “efficient local transport” and more “stable process boundary/API contract” for multiple clients.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### User
What I'm saying is, with the "app server" is json rpc the only, lame option?

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
_Searched 1 website_

### Assistant
_Worked for 13s_

### Assistant
Yeah. **As of the current App Server interface, JSON-RPC-ish messages are the only protocol it exposes.** You can change the transport, but not the serialization/protocol.

Your choices are:

- `stdio://` → newline-delimited JSON
- `ws://...` → one JSON-RPC message per WebSocket **text** frame
- `unix://...` → WebSocket over a Unix socket
- `off` → no listener

And OpenAI explicitly describes all of them as carrying the same bidirectional JSON-RPC 2.0-style protocol, except they omit the `"jsonrpc":"2.0"` field on the wire.

So there's currently **no supported**:

```text
MessagePack
CBOR
protobuf
bincode
raw framed Rust events
binary WebSocket frames
```

mode exposed by `codex app-server`.

Which means changing from:

```text
stdio JSONL
```

to:

```text
unix socket
```

doesn't solve the thing you're objecting to. You still have:

```text
Core Rust structs
      ↓
serialize to JSON
      ↓
frame/transport
      ↓
parse JSON
      ↓
your DTOs
```

rather than something like:

```text
Core Rust structs
      ↓
MessagePack/bincode
      ↓
length-prefixed byte stream
      ↓
your DTOs
```

The interesting wrinkle is that **the App Server now supports the TUI itself as a remote client**:

```sh
codex app-server --listen ws://127.0.0.1:4500

codex --remote ws://127.0.0.1:4500
```

and `--remote` also accepts `unix://`.

So OpenAI has apparently decided that App Server's JSON protocol is the common client boundary even for a TUI-over-remote setup, rather than introducing a Rust-native binary protocol just for it.

For **your particular architecture**, though, I agree with the motivation behind your complaint. If you're running:

```text
Codex child process
       │
       │ local IPC
       ▼
Rust harness
       │
       ▼
your own typed event bus
```

JSON is doing almost nothing useful once you've already committed to a tightly coupled local integration. It's primarily buying OpenAI a language-neutral, inspectable, versionable API boundary.

And unfortunately **App Server doesn't currently give you a `--codec msgpack` escape hatch.**

If you wanted to eliminate that overhead completely, you're no longer really "using App Server"; you'd be looking at either embedding/using the open-source Codex Rust crates directly or maintaining a very small fork that adds a binary transport alongside the existing outgoing-message abstraction. Given how the source is structured, the latter is probably much less insane than regexing stdout from the TUI—but it also couples you to Codex internals rather than its supported client protocol. memcite

### User
Okay, the most interesting thing you said was that it supports output to a unix socket??

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
Tool parameters:
- query: search

### Tool
The output of this plugin was redacted.

### Assistant
_Searched 1 website_

### Assistant
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Tool
The output of this plugin was redacted.

### Assistant
_Searched Codex documentation for Unix socket and WebSocket configuration_

### Tool
The output of this plugin was redacted.

### Assistant
_Searched 1 website_

### Assistant
_Worked for 17s_

### Assistant
Yes — **that part is real**, and it is probably useful for what you're building.

Codex App Server supports:

```bash
codex app-server --listen unix:///path/to/codex.sock
```

or simply:

```bash
codex app-server --listen unix://
```

where the latter uses Codex's default App Server control socket. The TUI's `--remote` can connect to those `unix://` endpoints as well.

The important catch is that it is **not a raw Unix byte-stream protocol**. They implemented:

```text
Unix domain socket
        ↓
HTTP Upgrade handshake
        ↓
WebSocket
        ↓
JSON-RPC message
        ↓
WebSocket text frame
```

OpenAI explicitly describes the Unix transport as **“WebSocket connections over … a Unix socket”**.

So compared with your current stdio setup:

```text
codex app-server
    │
    │ stdin/stdout JSONL
    ▼
your harness
```

you can instead do:

```text
codex app-server
    │
    │ Unix domain socket
    │ WebSocket frames
    ▼
your harness
```

That has some genuinely nice properties for you: **no child-process stdin/stdout ownership, clean reconnectability, multiple connection semantics, filesystem permissioning on the socket, and no TCP port at all.** The protocol above it remains exactly the same JSON-RPC App Server protocol.

And I think that's the interesting part given your architecture: your Rust service could treat Codex much more like a local daemon:

```bash
codex app-server --listen unix:///run/user/1000/te2-codex.sock
```

Then your Tokio side just connects to that UDS and speaks WebSocket/App Server. You aren't tying the Codex lifecycle to a particular stdin/stdout pipe anymore.

It **doesn't solve your JSON serialization complaint**, but it potentially solves a more important architectural annoyance: **App Server no longer has to be a stdio subprocess hanging directly off your harness.**

### User
Ok, so what It really boils down to is "what's faster"? An fd/pipe over python (Or several?) or UDS?

### Assistant
_Worked for 5s_

### Assistant
For **raw local IPC on Linux**, a plain pipe/FIFO-style fd is generally a little faster than a Unix domain stream socket. Pipes are simpler: fewer semantics, less protocol machinery, very optimized kernel path.

But for **your actual Codex setup**, that probably isn't the comparison that matters.

I'd rank your cases roughly like this:

1. **Rust ↔ raw pipe ↔ Codex** — probably fastest
2. **Rust ↔ raw UDS ↔ Codex** — extremely close
3. **Python ↔ pipe ↔ Codex** — depends heavily on what Python does
4. **Rust/Python ↔ WebSocket-over-UDS ↔ Codex** — more framing/parsing overhead
5. TCP/WebSocket locally — usually the most overhead, though still fast in absolute terms

The key point is that:

```text
pipe vs UDS
```

is probably a **tiny difference** compared with:

```text
Python object creation
JSON encode/decode
JSON-RPC dispatch
asyncio scheduling
Rust ↔ Python crossings
WebSocket framing
```

So if your current path is something like:

```text
Codex
  │
stdio pipes
  ▼
Python
  │
PyO3 / queue / socket / whatever
  ▼
Rust
```

and UDS lets you instead do:

```text
Codex
  │
WebSocket over UDS
  ▼
Rust
```

then **the UDS version could easily be faster end-to-end even though a raw pipe is technically the faster kernel primitive**, because you've deleted Python from the hot path.

That's the distinction I'd care about.

For a subprocess, stdio is normally:

```text
stdin  → pipe A
stdout ← pipe B
stderr ← pipe C
```

So you're already managing 2–3 independent fd streams.

A UDS gives you:

```text
         one full-duplex fd
Rust ◄────────────────────► Codex
```

which is architecturally nicer, especially when you start multiplexing many Codex instances.

### In your specific case

If the choice is:

```text
A:
Codex stdio JSONL
     ↓
Python subprocess handling
     ↓
Rust
```

versus:

```text
B:
Codex WebSocket/JSON-RPC over UDS
     ↓
Rust Tokio
```

**I'd take B.**

Not because UDS beats pipes—it doesn't necessarily—but because **direct Rust beats routing a high-volume event stream through Python**.

If instead you can do:

```text
Codex stdio JSONL
     ↓
Rust Tokio directly
```

then I'd expect **that** to beat WebSocket-over-UDS slightly. You'd have essentially:

```text
read(fd)
→ find newline
→ serde_json
```

versus:

```text
read(uds)
→ WebSocket frame parser
→ UTF-8/text frame
→ JSON-RPC
→ serde_json
```

So the real ordering for your harness is probably:

```text
direct Rust stdio pipes          ← fastest/simplest hot path
direct Rust WebSocket over UDS   ← slightly more CPU, nicer daemon semantics
Python-mediated stdio            ← likely worse than either
```

And unless you're pushing **a ridiculous number of Codex events**, the raw transport difference between the first two is probably lost in the noise next to JSON parsing and model/tool latency. The architectural benefits of UDS—reconnectability, independent lifetimes, one full-duplex endpoint, and easier multiplexing—are probably worth substantially more than the few percent you might lose to WebSocket framing.
