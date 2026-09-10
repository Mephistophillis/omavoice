# omavoice-hermes

A local-first voice assistant for [Omarchy](https://omarchy.org), forked from
[baranskyi/omavoice](https://github.com/baranskyi/omavoice). Press
`SUPER+CTRL+M`, a panel with a pixel waveform comes up, you talk — it talks
back. The thing Siri kept promising to be.

The difference from upstream: **no cloud voice API**. The ears are vosk
running on this machine, the mouth is edge-tts, and the brain is your local
[Hermes Agent](https://hermes-agent.nousresearch.com) gateway. Nothing
speech-related leaves the machine, and there is no per-minute audio bill.
The cloud LLM behind your Hermes config still answers the questions — through
the same provider you already type to.

```
SUPER+CTRL+M ─► omarchy-shell shell toggle <plugin-id>
                          │
   ┌─ QML plugin (Quickshell) ──────────────────────┐
   │  Overlay.qml   centred panel, waveform, MD     │
   │  BarWidget.qml state icon in the bar           │
   │  Client.qml    Unix socket, NDJSON             │
   └────────────────────────────────────────────────┘
                          │  $XDG_RUNTIME_DIR/omavoice.sock
   ┌─ omavoiced (Python, systemd --user) ───────────┐
   │  pw-record ─► vosk (STT)                       │
   │                   │ turn endpointing           │
   │                   ▼                            │
   │  hermes gateway /v1/chat/completions ── brain  │
   │                   │                            │
   │  edge-tts ─► ffmpeg ─► pw-play ── mouth        │
   └────────────────────────────────────────────────┘
```

There is no networking and no audio in the QML, and that is not a stylistic
choice: the plugin shares a process with the bar — anything slow or networked
in there would hang the whole desktop.

## How it works

Three parts, and the split is the whole design.

**The ears** are [vosk](https://alphacephei.com/vosk/) with the
`vosk-model-ru-0.42` model (a small model is available for testing; see
`OMAVOICE_VOSK_MODEL`). The model loads once at daemon start (~90–160 s on a
weak CPU) and stays cached for every session. Turn endpointing is ours, not
vosk's: vosk commits a fragment after its own ~0.5 s silence window, which
cut "what's the weather in Malaga… in Spain" into two questions. The daemon
accumulates fragments and partials into a turn buffer and flushes it as one
turn only after a real pause (`OMAVOICE_SILENCE_MS`, default 1500 ms),
anchored to when speech actually ended (word timings) and kept alive by
energy detection, which reacts within one chunk instead of a decoder's
0.3–0.9 s.

**The brain** is the local Hermes gateway (`hermes-gateway.service`, its
OpenAI-compatible API on `127.0.0.1:8642`). Dialog history is kept in the
daemon and replayed per call; the system prompt pins the voice contract:
strict JSON (`spoken` 1–3 sentences, optional `markdown` for the panel), no
tools unless the question explicitly asks for them — a voice agent that
walks the filesystem or sends desktop shortcuts because someone said "fix my
buttons" out loud is a hazard, not a feature. `codex exec` / `claude -p`
remain available as optional backends if they are on your `PATH`
(`omavoice-ctl backend codex`); the gateway path needs no binary, no key of
its own, and answers in ~5–8 s.

**The mouth** is [edge-tts](https://github.com/rany2/edge_tts) streaming
into ffmpeg into `pw-play`: the first sentence starts playing before the
rest is synthesized. While the assistant speaks, the microphone is gated and
fragments heard during playback are dropped (half-duplex echo guard); with
the echo canceller in place and headphones, interruption stays possible.

Marginal cost of a voice minute: vosk and edge-tts are free; you pay only
for the text tokens your Hermes provider already charges.

## Requirements

| | Why | Note |
|---|---|---|
| **Omarchy 4.0+** with `omarchy-shell` | the plugin is Quickshell QML | already there if you run Omarchy |
| **Hermes Agent** with the gateway running | the brain | `hermes config set platforms.api_server.enabled true`, port 8642, `API_SERVER_KEY` in `~/.hermes/.env` |
| **PipeWire** with `pw-record` / `pw-play` | audio in and out | standard on Omarchy |
| **Python 3.11+** | the daemon | the virtualenv is built from your own `python3`; `uv`, if present, only installs the pinned package |
| *optional:* `codex` / `claude` on `PATH` | alternate brains | not needed for the hermes backend |
| *optional:* `ollama` + a small model | fully-local brain (`OMAVOICE_BACKEND=ollama`) | qwen2.5:3b measured: 3–10 s warm answers on a 2-core CPU; 8B models do not fit 7.5 GB RAM |

Packages installed into the virtualenv (`vosk`, `edge-tts`, `websockets`,
`aiohttp`) come from `daemon/requirements.lock`, version-pinned and bound to
digests with `--require-hashes`. Nothing is installed system-wide, nothing
asks for `sudo`, and no executable code enters the environment except through
the lockfile. The vosk model (~1.4 GB for ru-0.42) is downloaded on first
daemon start into `~/.local/share/omavoice/models/`.

## Install

```bash
omarchy plugin add https://github.com/Mephistophillis/omavoice-hermes --enable
bash ~/.config/omarchy/plugins/io.github.baranskyi.omavoice/scripts/setup.sh
systemctl --user enable --now omavoice
```

Settings live in `~/.config/omavoice/env` and are read once at daemon start:

```bash
OMAVOICE_VOICE_ENGINE=local          # "realtime" returns to the upstream OpenAI path (needs its paid key)
OMAVOICE_BACKEND=hermes              # hermes | ollama | codex | claude
OMAVOICE_VOSK_MODEL=vosk-model-ru-0.42
OMAVOICE_TTS_VOICE=ru-RU-DmitryNeural
OMAVOICE_SILENCE_MS=1500             # pause that ends a turn
# OMAVOICE_HERMES_URL=http://127.0.0.1:8642
# OMAVOICE_HERMES_MODEL=glm-5.3-flash   # optional, for a gateway with several
# OMAVOICE_OLLAMA_MODEL=qwen2.5:3b       # local brain, no cloud at all
# OMAVOICE_OLLAMA_URL=http://127.0.0.1:11434
```

The gateway key is read from `~/.hermes/.env` (`API_SERVER_KEY`) at ask time —
never copied elsewhere.

**The hotkey**, in `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + CTRL + M", "Voice assistant", "omarchy-shell shell toggle io.github.baranskyi.omavoice")
```

### Echo cancellation

Without it the microphone picks up the assistant's own voice through the
speakers. The config ships with the plugin and `setup.sh` installs it at
`~/.config/pipewire/pipewire.conf.d/99-omavoice-echo-cancel.conf`; reload
PipeWire once (`systemctl --user restart pipewire`) and rerun setup to
verify. `OMAVOICE_INPUT=echo-cancel-source` and
`OMAVOICE_OUTPUT=omavoice_playback`, both at once or neither. On top of the
canceller: a self-measuring noise gate (opens at 8× the room floor, replaces
sub-floor audio with digital silence), and real-playback-time accounting —
the gate stays raised until `pw-play` drains plus 0.9 s of room tail.

With headphones or a verified canceller pair, you can interrupt the answer by
voice. Without them, the daemon falls back to protected half duplex: speak
after the answer finishes; `I` or `omavoice-ctl cancel` interrupts
immediately.

## Using it

| Action | How |
|---|---|
| Open the panel | `SUPER+CTRL+M`, or click the crystal in the bar |
| Send to background | `Esc`, a click outside, or the same hotkey |
| Stop, keeping the conversation | `Q`, right-click the crystal, `omavoice-ctl stop` |
| Interrupt an answer | `I`, or `omavoice-ctl cancel` |
| New conversation | `N`, or `omavoice-ctl reset` |
| Ask in writing | `omavoice-ctl ask "..."` |
| Make it speak a line | `omavoice-ctl say "..."` (echo testing) |
| Switch agent | middle-click the crystal, or `omavoice-ctl backend hermes` |
| Inspect state | `omavoice-ctl status` · `make logs` |

`Esc` and `Q` are different things: backgrounding changes nothing — the
microphone stays open and the answer is still spoken; `Q` releases the
microphone and cuts the answer off but keeps the conversation in memory.
Forgetting is `N`.

### The bar says when it can hear you

The crystal and the word beside it glow while the microphone is open
(listening / thinking / speaking all count; while the agent works, the word
becomes `looking Ns` with a live counter). After `Q` it goes out — that is
the one thing that closes the mic. Right-click stops listening from the bar
itself.

## Tuning the pause

A turn ends after `OMAVOICE_SILENCE_MS` of true quiet. 1500 ms covers a
thinking pause between clauses plus vosk's partial latency; lower it if the
assistant feels slow to answer, raise it if you think mid-sentence and your
questions still split. The first answer of a session feels slower than the
rest: the filler ("секунду") covers the gateway round trip (~5–8 s).

## Removal

```bash
bash ~/.config/omarchy/plugins/io.github.baranskyi.omavoice/scripts/uninstall.sh
omarchy plugin remove io.github.baranskyi.omavoice
```

Left behind on purpose: `~/.config/omavoice/env`, the vosk models under
`~/.local/share/omavoice/models/`, and the PipeWire echo-cancel config.

## Development

```bash
make sync       # copy the checkout into the plugins directory
make watch      # re-copy on every save; the shell reloads by itself
make validate   # manifest check, symlink check
make logs       # journalctl for the daemon
```

Parts are testable separately, bottom up:

```bash
# the audio path, no network and no key
~/.local/share/omavoice/venv/bin/python -m omavoice.audio --loopback

# the brain (hermes gateway), no microphone
~/.local/share/omavoice/venv/bin/python -m omavoice.brain "how much disk space?"

# the whole loop, no panel
~/.local/share/omavoice/venv/bin/python -m omavoice --headless
```

Notes that cost time to learn:

- **QML type cache**: editing a non-entry-point QML file is picked up neither
  by saving nor `rescanPlugins`; use `omarchy-restart-shell`.
- **A virtualenv cannot live inside the plugin folder** — Omarchy's validator
  rejects symlinks; that is why it lives under `~/.local/share/omavoice/`.
- **When the assistant mishears**, the first question is what it thinks it
  heard: `journalctl --user -u omavoice | grep -E "heard:|turn:|said:"`.
- **systemd-oomd** on memory-tight machines can kill the daemon during the
  vosk load peak; a drop-in with `Slice=background.slice` and
  `TimeoutStopSec=45` keeps it out of the monitored slice.
- **Testing the ears offline**: synth material with edge-tts, but trim each
  clip to speech — edge-tts pads ~1 s of tail silence, which silently changes
  every pause in your timeline.

## Layout

```
manifest.json       kinds: overlay + bar-widget, keepLoaded
Overlay.qml         the panel, layer-shell above everything
Waveform.qml        a figure of points on a Canvas
PrimeRadiant.qml    the logo and the bar icon
BarWidget.qml       the state icon (glow, looking-counter, click actions)
StateHues.qml       per-state colours shared by panel and bar
Client.qml          socket, state, replay handling
Undertext.qml       the transcript under the waveform

daemon/omavoice/
  __main__.py   the state machine, where everything is joined
  localvoice.py vosk ears, turn endpointing, edge-tts mouth, turn loop
  realtime.py   upstream OpenAI Realtime path (OMAVOICE_VOICE_ENGINE=realtime)
  brain.py      hermes gateway / codex exec / claude -p, answer contract
  audio.py      pw-record / pw-play, RMS, interruption
  devices.py    source/sink selection, echo-canceller routing
  ipc.py        Unix socket, NDJSON, broadcast + replay
  config.py     env knobs, key file handling
  ctl.py        omavoice-ctl
  probe.py      stream a PCM dump through the pipeline

schemas/answer.json   the shape of the agent's answer
systemd/    the user unit, as a template setup.sh fills in
pipewire/   the echo cancellation config
scripts/    setup.sh, uninstall.sh
```

## Security and privacy

- **The daemon runs as a user unit** and records audio only while a session
  is open; the microphone is released on `Q`/stop.
- **Speech never leaves the machine**: vosk and edge-tts are local/free; the
  only network call is the question text to your own gateway on localhost.
  (edge-tts itself talks to Microsoft's free neural-voice endpoint for
  synthesis — swap `OMAVOICE_TTS_VOICE` for a piper voice if that matters
  to you.)
- **The voice brain is leashed**: the system prompt forbids running
  commands, web/file access and desktop input unless the question explicitly
  asks for it. This is a real fence, learned the hard way: a gateway session
  has the full tool surface, and one vague complaint spoken near the
  microphone once became eight minutes of the agent "fixing" the desktop —
  sending SUPER+Q to windows and starting a second fcitx5.
- **Destructive-sounding requests** (`rm -rf`, `mkfs`, `shutdown`) are
  refused before any agent is asked.
- **Bounded output**: everything a backend writes is read to a ceiling and
  the process ended at it; every field that survives into an answer is cut
  to size before it is kept or broadcast.
- **Links and paths from an answer** are opened through an argv vector,
  never a shell string; links must match `http(s)://`; images are stripped
  from markdown.
- The optional codex/claude backends keep upstream's sandboxing: per-run
  permission profiles, denied write/web tools, no `~/.codex` or Claude
  settings loaded. See upstream's README for the full measured write-up.

## Cost

Voice minutes are free (vosk + edge-tts). You pay only the text tokens of
your gateway's provider, exactly as if you had typed the question.

## License

MIT — see [LICENSE](LICENSE). Upstream: [baranskyi/omavoice](https://github.com/baranskyi/omavoice).
