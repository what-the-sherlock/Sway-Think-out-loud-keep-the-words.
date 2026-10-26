# Sway — Data Handling and Privacy

**Verified against:** `1e85b9f`, 2026-08-26.

A dictation tool sees everything you write. This document states exactly what Sway
stores, where, for how long, what leaves the machine, and what an attacker with
various levels of access could get. It is written to be checkable against the code,
not to be reassuring.

---

## 1. The one-paragraph version

Sway keeps your transcripts in a SQLite file on your own disk, keeps your API key in
Windows Credential Manager, and emits **no telemetry of any kind**. The only network
traffic is two calls per dictation to Groq's API: the audio, and the transcript for
cleanup. Audio is never written to disk. Transcript text is never written to the log
file or to the latency database. Everything else — history, dictionary, settings,
timings — stays local and is deletable from the UI.

**The honest caveat:** Sway is local-*first*, not local-*only*. Your audio and your
transcript reach a third-party API. If that is unacceptable for a particular
utterance, do not dictate it.

---

## 2. What leaves the machine

Exactly two request types, both to `api.groq.com`, both over one persistent HTTP/2
connection.

| Call | Payload | When |
|---|---|---|
| **Transcription** | FLAC audio (16 kHz mono, VAD-trimmed), plus a `prompt` bias string built from your personal dictionary, plus an optional language hint | Every dictation, command, and streamed chunk |
| **Cleanup / instruction** | The transcript text, the cleanup system prompt, any fired transform directives, and a short label naming the focused application | Only when the model is not skipped (~46% of dictations) |

Plus one **HEAD request to `api.groq.com` at startup** to pre-warm the connection —
no payload, no identifying content.

### What is deliberately *not* sent

- **No window contents.** The focused app travels as a short label ("a code editor"),
  never as text scraped from the window. Context by reference, not by volume.
- **No transform library.** The model sees only the directives that actually fired,
  which is normally none.
- **No history.** Previous dictations are never included as context.
- **No telemetry.** No usage counts, no crash reports, no anonymised events, no
  opt-in analytics. There is no endpoint to send them to and no code to send them.
- **No audio for skipped calls.** A clip the VAD says contains no speech is dropped
  before encode — no upload, no quota spent.

### Your relationship with Groq

Sway sends the data; Groq's terms govern what happens to it there. Sway does not and
cannot make a claim about that. What Sway controls is the *volume*: local resolution
means snippets never reach the model at all, and the skip heuristic keeps roughly
half of transcripts out of the cleanup call.

---

## 3. What is stored, and where

All under `<data_dir>` — `%LOCALAPPDATA%\Sway` on Windows,
`$XDG_DATA_HOME/sway` elsewhere.

| File | Contents | Contains your words? | Retention |
|---|---|---|---|
| `history.db` | Pasted text, the raw transcript it came from, the instruction for commands, focused process name, per-entry metrics, detected corrections | **Yes** | `history_retention_days`, default 90; `0` keeps forever |
| `latency.db` | Run records and per-stage timings, plus metadata labels | **No** — audited | `latency_retention_days`, default 30 |
| `dictionary.db` | Your vocabulary terms and learning candidates | Partially — terms only | Until deleted |
| `settings.json` | Bindings, transforms, snippets, per-app rules, preferences | Your snippet and instruction text | Until changed |
| `instance.json` | Live port, token, PID for second-launch focus | No | Deleted on clean shutdown |
| `sway.log` | Rotating diagnostic log | **No transcript text** — audited | Rotated at 1 MB, 3 backups |

### The two audited invariants

These are checked, not assumed:

1. **No audio is persisted anywhere.** The ring buffer is memory. Encoded FLAC exists
   only as a request body. Nothing writes it to disk.
2. **No transcript text lands in `latency.db` or `sway.log`.** The latency metadata
   carries only labels — `words`, `characters`, `audio_ms`, `speech_ms`,
   `cleanup_skip_reason` — never the text they describe.

The single specified exception to (1) is the **failure retry queue**
(`<data_dir>/queue/`), which does not exist yet. When it ships it must be: FLAC blobs
on disk and never in any database; a hard cap on both count and total bytes with
oldest-evicted; deleted on successful retry; deleted on quit unless still pending;
enumerable and wipeable from "delete all"; and **never in `%TEMP%`**, which no
uninstall path covers.

---

## 4. The API key

Resolution order, which is the whole design:

1. **Windows Credential Manager** via `keyring` — where a normal user's key ends up,
   set from the UI, never written to a file Sway controls.
2. **`SWAY_GROQ_API_KEY`** (legacy `FLOW_GROQ_API_KEY` still accepted) — an explicit developer override. Deliberately *not* the
   bare `GROQ_API_KEY`, so a key exported for some other tool in the same shell
   cannot silently outrank the one you configured here.
3. **`GROQ_API_KEY`** from the environment or `.env` — the dev flow, unchanged.

A key found only at level 3 is **copied up** into the credential store on first run
and **`.env` is left exactly as it was**. Silently editing a file the user wrote is
worse than a duplicated secret, and the dev flow depends on `.env` continuing to work.

In memory the key is wrapped in a `Secret` that refuses to render itself in logs,
reprs, or tracebacks — so an unhandled exception cannot leak it into the log file.

**Known exposure:** `scripts/build_app.py` copies `.env` next to the built executable
so the packaged app works standalone. `dist/` is gitignored so this never reaches the
repository, but **shipping a zip of that build would ship the key**. The keyring path
is the fix; until a build stops doing this, do not distribute a build made on your own
machine.

---

## 5. The local server

The dashboard binds `127.0.0.1` on an ephemeral port. It is the one agreed exception
to the no-web-UI rule and it never becomes a public listener.

Three layers, because a process that can paste text into any focused window is worth
attacking:

1. **Loopback only.** Nothing off the machine can reach it.
2. **A per-launch token**, required on every `/api/` request, compared in constant
   time, never logged, and scrubbed from the address bar the moment the page loads.
   Without it, any web page you visit could script requests at `localhost` and read
   your entire history.
3. **Origin checks on every write.** A cross-origin POST is rejected outright.

`instance.json` holds a live token, so it is not an ordinary state file: owner-only
mode, rewritten never appended, deleted on clean shutdown, and **a stale file is
treated as absent** — the recorded PID must still be alive before the port is trusted,
because by then it could belong to an unrelated process. Note honestly that
`os.chmod` is close to a no-op against real Windows ACLs; the directory's inherited
ACL is what actually protects it, and the mode is belt and braces for the POSIX
adapter.

---

## 6. Threat model

| Adversary | Can they get your transcripts? | Why / why not |
|---|---|---|
| A remote attacker with no access to your machine | **No** | Nothing listens on a non-loopback interface. There is no server component and no account. |
| A malicious web page you visit | **No** | The token gates every route, and writes additionally require same-origin. Without the token a `localhost` fetch gets a 403. |
| Another user account on the same machine | **Mostly no** | `%LOCALAPPDATA%` is ACL'd to your user by Windows. This is the OS's guarantee, not Sway's. |
| Malware running as *you* | **Yes** | It can read `history.db` directly. Nothing at the application layer defends against code running with your own privileges — and note this is equally true of your browser profile and your notes app. |
| Someone with physical access to an unlocked machine | **Yes** | The history window is one click from the tray. |
| Groq | **Yes, for what you send** | See §2. |
| Sway's authors | **No** | There is no telemetry, no endpoint, and no code path that sends anything anywhere but Groq. |

**Not defended against, stated plainly:** the databases are not encrypted at rest.
SQLCipher would add a dependency and a startup cost to defend against an adversary who
already has your file system, i.e. one who has already won. If you need encryption at
rest, use full-disk encryption, which defends the whole problem rather than one file.

---

## 7. Deleting your data

| What | How |
|---|---|
| One dictation | History page → delete on the entry |
| All dictations | History page → **delete all** (genuinely wipes rows, not a UI hide) |
| Ageing out automatically | Settings → history retention (default 90 days), latency retention (default 30) |
| A dictionary term | Dictionary page → remove |
| Everything | Quit Sway, delete `%LOCALAPPDATA%\Sway`, remove the Sway entry from Windows Credential Manager, and delete the `HKCU\...\Run` key if launch-at-login was enabled |

**Gap (PRD FR-11.7):** the last row is a manual procedure. A documented,
single-action uninstall/reset that covers settings, the keyring entry, all three
databases, the queue directory, `instance.json`, and the Run registry key is specified
and not yet implemented.

---

## 8. Verifying these claims yourself

Nothing here has to be taken on trust:

```powershell
# What is stored, and how big
Get-ChildItem $env:LOCALAPPDATA\Sway

# Prove no transcript text is in the latency DB or the log
sqlite3 $env:LOCALAPPDATA\Sway\latency.db "SELECT metadata FROM runs LIMIT 20;"
Select-String -Path $env:LOCALAPPDATA\Sway\sway.log -Pattern "<a phrase you dictated>"

# See exactly what the app resolved, secrets redacted
sway config

# Prove nothing but Groq is contacted: run Sway, dictate, and watch
Get-NetTCPConnection -OwningProcess (Get-Process Sway).Id
```

The last one should show one outbound connection to Groq and one **listening socket
bound to 127.0.0.1** — never `0.0.0.0`.
