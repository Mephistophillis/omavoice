"""The local voice session — vosk ears, edge-tts mouth, nothing more.

Drop-in replacement for realtime.RealtimeSession with the same public surface
(connect, run, send_audio, cancel_response, say, open_floor, cancel_tools,
close, last_activity, awaiting_first_turn, connected). Everything the rest of
the daemon needs to react to arrives through the same callbacks; everything it
wants to say goes through send_audio / respond / say.

The turn loop, unlike the server-side Realtime API, lives here:

    mic chunk -> vosk (final = end of phrase, ~0.7s of silence)
              -> on_event(input_audio_transcription.completed)
              -> every factual question goes to ask_agent (the daemon routes
                 it to the brain) — the vosk transcript is never answered
                 from "weights", it is all there is
              -> the spoken text is synthesized by edge-tts in a streaming
                 fashion (first sentence starts playing before the rest is
                 synthesized) and each PCM chunk goes to on_audio

Half-duplex by design: while the assistant speaks, incoming mic audio is
still fed to vosk but phrase finals are held back — the echo path (speakers
-> mic) is not cancelled on most machines, and answering our own voice is
the failure mode the original omavoice guards against most carefully.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable

from .config import Config

log = logging.getLogger("omavoice.localvoice")

MAX_QUERY_CHARS = 2000

# Sentinel queued after the release silence: when the worker pops it, the
# turn is committed. Deterministic — counting chunks instead raced with real
# mic chunks still ahead in the queue.
_PTT_FLUSH = object()

# edge-tts streams MP3; ffmpeg converts to PCM16 at the daemon's pipeline rate
# (cfg.sample_rate — 16 kHz in local mode), which is what on_audio expects.

# Conversational turns answered without the brain. Everything else —
# any fact, any question about files or the world — goes to ask_agent,
# mirroring the hard rule the original realtime instructions had.
#
# The match must be on the WHOLE turn, never a prefix: "Привет. Расскажи,
# когда родился Пушкин?" starts with a greeting but IS a question. The old
# prefix regex swallowed such turns whole — the transcript arrived, matched
# ^привет, and the turn died with no brain call and no spoken word (the
# user heard silence for a minute until the watchdog idled the session).
_CHAT_WORDS = frozenset((
    "привет", "здравствуй", "здравствуйте", "пока", "до связи",
    "спасибо", "благодарю", "ага", "угу", "ок", "окей", "хорошо",
    "ладно", "понятно", "да", "нет", "что", "повтори", "стоп",
))


def _is_chat_only(text: str) -> bool:
    """True only when the entire turn is social filler (1-2 words)."""
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    return 0 < len(words) <= 2 and all(w in _CHAT_WORDS for w in words)

_SENTENCE_END = re.compile(r"[.!?…]\s|$")

# filler spoken while the brain works — one or two words, like the original
_FILLERS = ("секунду", "сейчас посмотрю", "проверяю", "секундочка")


class LocalVoiceSession:
    """One live conversation with local ears and mouth."""

    # Process-level vosk model cache: loading ru-0.42 costs ~70 s and 1.2 GB,
    # and sessions come and go. None until the first connect() loads it.
    _MODEL_CACHE = None
    _MODEL_PATH = None

    def __init__(
        self,
        cfg: Config,
        *,
        on_audio: Callable[[bytes], Awaitable[None]],
        on_event: Callable[[dict], Awaitable[None]],
        on_tool_call: Callable[[str, str], Awaitable[str]],
    ) -> None:
        self.cfg = cfg
        self.on_audio = on_audio
        self.on_event = on_event
        self.on_tool_call = on_tool_call
        self._recognizer = None
        self._model = None
        self._closed = False
        self._speaking = False
        self._await_first_turn = True
        self._abort_play = asyncio.Event()
        self._brain_task: asyncio.Task | None = None
        self.sent_events = 0
        self.last_activity = 0.0
        self._warned_no_socket = False
        # Recognition runs on ONE dedicated thread (KaldiRecognizer is not
        # thread-safe; see _start_worker). Audio flows one way through this
        # queue; finals hop back via loop.call_soon_threadsafe.
        import queue as _queue

        self._audio_q: "queue.Queue[bytes | None]" = _queue.Queue()
        self._worker = None
        self._worker_loop = None
        self._deliver_final = None
        self._final_tasks: set = set()
        self._drain_task: asyncio.Task | None = None
        # Push-to-talk state, shared with the recognition thread. `held` says
        # the mic gate is open (a hold is one turn, pauses inside it do not
        # end it); `flush_now` asks the worker to commit the turn at once —
        # set on key release, checked at the top of the worker loop.
        self._ptt_held_evt = threading.Event()
        # handy (batch) engine: the whole V-hold is ONE turn — mic PCM is
        # buffered while held and transcribed as a single WAV on release.
        # Vosk's streaming machinery (worker/sentinel/silence) stays for the
        # "vosk" engine; here it is simply never started.
        self._handy = getattr(cfg, "stt_engine", "vosk") == "handy"
        self._hold_buf = bytearray()
        self._turn_tasks: set = set()

    @property
    def awaiting_first_turn(self) -> bool:
        return self._await_first_turn

    @property
    def connected(self) -> bool:
        return self._model is not None

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Load the vosk model. No network, no key.

        The model is a process-level cache: it is 3.5 GB on disk and ~1.2 GB
        resident, and a session that ends must not make the next one pay for
        it again — an early version reloaded it per session and the machine
        spent a minute and 3 GB of swap doing nothing else.
        """
        if self._handy:
            # The heavy model lives in the handy CLI's own process, loaded
            # per transcription on the iGPU. Nothing to warm here — that is
            # the point: the daemon's RSS stays small and there is no
            # 90 s/5 GB vosk load for the OOM killer to pick on.
            import shutil

            if not shutil.which("handy"):
                raise RuntimeError(
                    "handy binary not found — install handy-bin or set "
                    "OMAVOICE_STT_ENGINE=vosk"
                )
            self._model = True  # sentinel: `connected` is True for handy too
            self.last_activity = asyncio.get_running_loop().time()
            self._await_first_turn = True
            log.info("handy stt ready (model=%s)", self.cfg.handy_model)
            return

        from vosk import Model  # deferred: heavy import

        self._recognizer = None
        cls = type(self)
        if cls._MODEL_CACHE is None or cls._MODEL_PATH != self.cfg.vosk_model_dir():
            model_path = str(self.cfg.vosk_model_dir())
            log.info("loading vosk model %s", model_path)
            import time

            t0 = time.monotonic()
            cls._MODEL_CACHE = await asyncio.to_thread(Model, model_path)
            cls._MODEL_PATH = self.cfg.vosk_model_dir()
            log.info("vosk model ready in %.1fs", time.monotonic() - t0)
        else:
            log.info("vosk model already loaded (cached)")
        self._model = cls._MODEL_CACHE
        self.last_activity = asyncio.get_running_loop().time()
        self._await_first_turn = True

    async def close(self) -> None:
        self._closed = True
        self._abort_play.set()
        if self._brain_task and not self._brain_task.done():
            self._brain_task.cancel()
        if self._drain_task:
            self._drain_task.cancel()
        for t in list(self._turn_tasks):
            t.cancel()
        if self._worker is not None:
            self._audio_q.put_nowait(None)
            self._worker = None
            self._deliver_final = None
        # The recognizer is dropped but the MODEL stays in the cache: the next
        # session reuses it. KaldiRecognizer holds no audio of its own.
        self._recognizer = None
        self._model = None

    # -- push-to-talk ---------------------------------------------------------

    def begin_utterance(self) -> None:
        """V pressed: everything the mic hears until release is ONE turn.

        Called from the daemon's event loop; the events are the only state
        touched, and they are thread-safe by construction. The recognizer
        itself is NOT reset here — that would race the worker thread; a fresh
        turn starts clean because the previous one was flushed on its release.
        """
        if self._handy:
            # A fresh hold discards whatever a lost release left behind.
            self._hold_buf.clear()
        self._ptt_held_evt.set()

    def end_utterance(self) -> None:
        """V released: commit the turn now, not when silence would have.

        handy engine: the buffered hold IS the utterance — boundaries are
        exactly the key, no endpointing to guess. The buffer is snapshotted
        as bytes before any new hold can touch it, and transcription runs as
        a task so the IPC reply (and the next key press) are not blocked on
        the ~5 s the CLI takes.

        Vosk engine: vosk commits a final only after its own ~0.5 s internal
        silence window — but the daemon closes the mic gate the instant the
        key goes up, so the real audio stream just stops and that window
        never arrives: the last clause would live on as a low-quality
        partial forever. So ~0.7 s of synthesized silence is queued for the
        recognizer to commit properly, followed by a sentinel; when the
        worker pops the sentinel it commits whatever accumulated as one turn.
        FIFO order makes this deterministic — no clock, no counting.
        """
        self._ptt_held_evt.clear()
        if self._handy:
            buf = bytes(self._hold_buf)
            self._hold_buf.clear()
            if buf:
                task = asyncio.get_running_loop().create_task(
                    self._handy_turn(buf))
                self._turn_tasks.add(task)
                task.add_done_callback(self._turn_tasks.discard)
            return
        chunk = bytes(int(self.cfg.sample_rate * self.cfg.channels * 2 * 0.02))
        for _ in range(35):  # 35 x 20 ms = 0.7 s of digital silence
            self._audio_q.put_nowait(chunk)
        self._audio_q.put_nowait(_PTT_FLUSH)

    # -- outbound (daemon -> session) ----------------------------------------

    async def send_audio(self, pcm: bytes) -> None:
        """Queue mic PCM16 for vosk. Called from the daemon's mic loop."""
        if self._closed or self._model is None:
            return
        if self._handy:
            # Only audio captured while V is held belongs to a turn; the
            # daemon's mic pump already gates everything else away.
            if self._ptt_held_evt.is_set():
                self._hold_buf.extend(pcm)
            return
        if self._worker is None:
            self._start_worker()
        self._audio_q.put_nowait(pcm)

    def _start_worker(self) -> None:
        """One dedicated recognition thread, started lazily on first audio.

        KaldiRecognizer is NOT thread-safe, and `asyncio.to_thread` runs its
        callees on a shared pool: two chunks arriving close together were
        recognised concurrently from different worker threads, corrupting the
        recognizer's state until Kaldi's own assert aborted the whole daemon
        (libvosk KaldiAssertFailure in ExtractWindow). A single long-lived
        worker thread gives the same loop-breathing without the data race,
        and the queue keeps chunk order.

        Finals cross back with loop.call_soon_threadsafe — the canonical
        thread->loop bridge; no second queue to deadlock or leak.
        """
        import threading

        if self._worker is not None:
            return
        loop = asyncio.get_running_loop()
        self._worker_loop = loop

        def _deliver(final: str) -> None:
            def _run() -> None:
                self.last_activity = loop.time()
                if not self._closed and not self._speaking and final:
                    task = asyncio.ensure_future(self._on_final(final))
                    self._final_tasks.add(task)
                    task.add_done_callback(self._final_tasks.discard)

            try:
                loop.call_soon_threadsafe(_run)
            except RuntimeError:
                pass  # loop closed — session is gone anyway

        self._deliver_final = _deliver
        self._worker = threading.Thread(
            target=self._worker_main, name="vosk-rec", daemon=True)
        self._worker.start()

    def _worker_main(self) -> None:
        from vosk import KaldiRecognizer

        rec = KaldiRecognizer(self._model, self.cfg.sample_rate)
        # Word timings in finals: the anchor for our own endpointing. Vosk
        # commits a final ~0.3-0.5 s AFTER the speech it describes ended (its
        # internal endpointer waits for its own silence window first), so
        # "when the final arrived" is the wrong place to start counting OUR
        # silence window from — the two windows overlap and the effective
        # threshold shrinks below what silence_ms promises. The last word's
        # `end` is when the person actually stopped talking.
        rec.SetWords(True)
        self._recognizer = rec

        # Our own endpointing, on top of vosk's. Vosk commits a final after
        # ~0.5 s of silence — its own threshold, not ours, and not tunable
        # through the C API. A person composing a question out loud pauses
        # between clauses ("what's the weather in Malaga… in Spain"), and vosk
        # hands us each clause as a finished phrase. The old code sent every
        # clause straight to the brain, so "ну" became a question of its own.
        #
        # Instead: accumulate the turn in a buffer. Each new final APPENDS to
        # it; each partial with new words resets the silence clock (speech is
        # still evolving); silence that lasts `silence_ms` since the speech
        # actually ENDED (per word timings, not per vosk's late commit) means
        # the person has stopped talking — flush the whole buffer as one turn.
        #
        # Push-to-talk replaces the clock with the key: while the hold event
        # is set, finals accumulate and silence NEVER flushes — a pause inside
        # the hold is just a pause. Release flushes via _flush_now (see
        # end_utterance); the synthetic silence it queues lets vosk commit
        # its pending final first, so the tail of the phrase survives as a
        # proper final rather than a trailing partial.
        final_parts: list[str] = []
        partial_text = ""
        # `now` minus this is the silence elapsed; anchored to speech end.
        last_change = time.monotonic()
        audio_t = 0.0  # seconds of audio pulled off the queue
        bytes_per_s = self.cfg.sample_rate * self.cfg.channels * 2
        silence_s = max(0.2, self.cfg.silence_ms / 1000.0)

        import array as _array

        def _voiced(chunk: bytes) -> bool:
            """Is there speech in this chunk, by energy alone.

            The daemon's gate replaces sub-floor noise with zeros before this
            audio arrives, so anything clearly non-zero is above the room's
            measured floor. Energy rises within one chunk of speech starting
            — unlike a vosk partial, which needs 0.3-0.9 s of decoding first
            and let a resumed clause miss the flush deadline (measured).
            """
            if not chunk:
                return False
            samples = _array.array("h")
            samples.frombytes(chunk[: len(chunk) - len(chunk) % 2])
            if not samples:
                return False
            peak = max(abs(s) for s in samples)
            return peak > 200

        while True:
            pcm = self._audio_q.get()
            if pcm is None or pcm is _PTT_FLUSH:
                # Session over, or a push-to-talk release: a turn still in
                # the buffer is delivered, not dropped — the person said it,
                # and vanishing speech is worse than an abrupt end. The
                # release silence queued ahead of the sentinel has already
                # been fed through (FIFO), so vosk has committed its final
                # and the buffer holds the whole utterance.
                if final_parts or partial_text:
                    self._flush_turn(final_parts, partial_text, rec)
                if pcm is None:
                    return
                final_parts = []
                partial_text = ""
                continue
            audio_t += len(pcm) / bytes_per_s

            # Echo guard: while the assistant is speaking, the mic still runs
            # (the canceller is good but not perfect), and the old code's
            # "drop finals during playback" invariant must survive the
            # accumulator — otherwise the assistant's own residual echo piles
            # up in the buffer and is answered as a turn once it ends.
            if self._speaking:
                if final_parts or partial_text:
                    final_parts = []
                    partial_text = ""
                    rec.Reset()
                continue

            try:
                if rec.AcceptWaveform(pcm):
                    result = json.loads(rec.Result())
                    final = result.get("text", "")
                    if final:
                        final_parts.append(final)
                        partial_text = ""
                        # Anchor the clock to when the speech ended, not to
                        # when vosk got around to telling us. audio_t is the
                        # audio clock; the mic feeds real-time, so audio
                        # seconds and wall seconds advance together.
                        words = result.get("result") or []
                        speech_end = float(words[-1]["end"]) if words else audio_t
                        lag = min(max(audio_t - speech_end, 0.0), 2.0)
                        last_change = time.monotonic() - lag
                partial = json.loads(rec.PartialResult()).get("partial", "")
                if (partial and partial != partial_text) or _voiced(pcm):
                    # New words mid-stream, or energy above the floor: speech
                    # is alive and the turn keeps growing. The clock restarts.
                    partial_text = partial or partial_text
                    last_change = time.monotonic()
                elif (final_parts or partial_text) and not self._ptt_held_evt.is_set():
                    # Silence while a turn is open: flush once it has lasted
                    # long enough. Measured from the anchored speech end, so
                    # vosk's commit delay cannot eat into the budget. Under a
                    # PTT hold this arm is dead — the key, not the clock,
                    # decides when the turn is over.
                    if time.monotonic() - last_change >= silence_s:
                        self._flush_turn(final_parts, partial_text, rec)
                        final_parts = []
                        partial_text = ""
                elif not (final_parts or partial_text):
                    # Idle noise before any speech: nothing to keep, nothing
                    # to flush; do not let phantom partials arm the clock.
                    pass
            except Exception:
                log.exception("vosk recognition failed")
                return

    def _flush_turn(self, final_parts: list[str], partial_text: str, rec) -> None:
        """Commit the accumulated turn and hand it to the conversation loop."""
        # Prefer the committed finals; a trailing partial is a clause vosk
        # never committed — better than losing it, worse than a final, so it
        # is appended only when there is nothing committed after it.
        text = " ".join(part.strip() for part in final_parts if part.strip())
        partial = (partial_text or "").strip()
        if not text:
            text = partial
        elif partial and partial != final_parts[-1].strip():
            # The partial has grown past the last final (speech resumed after
            # the commit but before our flush): keep its tail only.
            last = final_parts[-1].strip()
            if partial.startswith(last):
                text = text + " " + partial[len(last):].strip()
            else:
                text = text + " " + partial
        text = " ".join(text.split())
        if text:
            log.info("turn: %s", text[:160])
            if self._deliver_final is not None:
                self._deliver_final(text)
        rec.Reset()

    async def _feed(self) -> None:
        """Historic drain loop, kept as a no-op seam for tests.

        Finals now hop the thread boundary via call_soon_threadsafe (see
        _start_worker); there is no queue for the loop to wait on."""
        return

    async def _on_final(self, text: str) -> None:
        text = " ".join(text.split()).strip()
        if not text:
            return
        if len(text) > MAX_QUERY_CHARS:
            text = text[:MAX_QUERY_CHARS]
        self.sent_events += 1

        # Emit the transcript first: the daemon decides whether this opens the
        # conversation (its `_opens_conversation`) and calls open_floor(),
        # which clears _await_first_turn — mirroring "do not speak before you
        # are spoken to" from the original session.
        await self.on_event(
            {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": text}
        )
        if self._await_first_turn:
            # The daemon judged it not an opening turn; stay quiet.
            return

        # Conversational turns (a bare greeting / thanks / bye) are answered
        # by the voice layer itself; everything else — every fact, every
        # question, even one glued to a greeting — goes to the brain.
        if _is_chat_only(text):
            short = self._smalltalk(text)
            if short:
                await self._speak_and_close(short)
            return

        await self._ask_brain_and_speak(text)

    def _smalltalk(self, text: str) -> str:
        t = text.lower()
        if re.match(r"^(пока|до связи|всего доброго)", t):
            return "До связи."
        if re.match(r"^спасибо", t):
            return "Пожалуйста."
        if re.match(r"^(привет|здравствуй|здравствуйте)", t):
            return "Привет!"
        return ""

    async def _speak_and_close(self, text: str) -> None:
        await self._speak(text)
        await self.on_event(
            {"type": "response.output_audio_transcript.done", "transcript": text}
        )

    # -- the brain round trip ------------------------------------------------

    async def _ask_brain_and_speak(self, query: str) -> None:
        self._abort_play.clear()
        await self._speak(_FILLERS[len(query) % len(_FILLERS)])
        task = asyncio.create_task(self._brain_roundtrip(query))
        self._brain_task = task
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("brain round trip failed")
            await self._speak("Не получилось обратиться к агенту.")
            await self.on_event(
                {"type": "response.output_audio_transcript.done",
                 "transcript": f"Ошибка агента: {exc}"}
            )

    async def _brain_roundtrip(self, query: str) -> None:
        answer = await self.on_tool_call("ask_agent", query)
        spoken = (answer or "").strip()
        if not spoken:
            spoken = "Агент ничего не ответил."
        await self.on_event(
            {"type": "response.output_audio_transcript.delta",
             "delta": spoken}
        )
        await self._speak(spoken)
        await self.on_event(
            {"type": "response.output_audio_transcript.done",
             "transcript": spoken}
        )

    # -- speaking (edge-tts -> ffmpeg -> on_audio) ---------------------------

    async def _speak(self, text: str) -> None:
        """Synthesize `text` and stream PCM to the speakers via on_audio.

        Playback of a long answer can be aborted (barge-in / cancel):
        synthesis and conversion tasks are stopped, the queue drained.
        Each utterance starts by CLEARING the abort flag — cancel_response()
        sets it, and without this reset every later utterance (a smalltalk
        reply, an error line) would be born already aborted and stay silent
        forever.
        """
        import edge_tts

        text = (text or "").strip()
        if not text or self._closed:
            return
        self._abort_play.clear()
        self._speaking = True
        try:
            ff = subprocess.Popen(
                ["ffmpeg", "-v", "quiet", "-i", "pipe:0",
                 "-f", "s16le", "-ar", str(self.cfg.sample_rate), "-ac", "1", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
            try:
                comm = edge_tts.Communicate(text, self.cfg.tts_voice)
                pump = asyncio.create_task(self._tts_pump(comm, ff))
                conv = asyncio.create_task(self._conv_pump(ff))
                done, _ = await asyncio.wait(
                    {pump, conv}, return_when=asyncio.FIRST_COMPLETED
                )
                if self._abort_play.is_set():
                    pump.cancel()
                    conv.cancel()
                for t in (pump, conv):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            finally:
                for s in (ff.stdin, ff.stdout):
                    with contextlib_suppress():
                        s.close()
                ff.terminate()
        finally:
            self._speaking = False

    async def _tts_pump(self, comm, ff) -> None:
        try:
            async for chunk in comm.stream():
                if self._abort_play.is_set() or self._closed:
                    break
                if chunk["type"] == "audio":
                    ff.stdin.write(chunk["data"])
        finally:
            with contextlib_suppress():
                ff.stdin.close()

    async def _conv_pump(self, ff) -> None:
        loop = asyncio.get_running_loop()
        while True:
            data = await loop.run_in_executor(None, ff.stdout.read1, 48000)
            if not data:
                break
            if self._abort_play.is_set() or self._closed:
                break
            await self.on_audio(data)
            # keep the "still working" signal alive for the daemon watchdog
            self.last_activity = loop.time()

    # -- handy batch transcription --------------------------------------------

    async def _handy_turn(self, pcm: bytes) -> None:
        """One V-hold as one turn: WAV -> handy CLI -> the conversation loop.

        Runs as a task (see end_utterance) so a ~5 s transcription never
        blocks the IPC reply or the next key press. The subprocess is
        killed on cancel (session closed), and a short hold (<0.35 s) is
        ignored — a stray tap is not a turn.
        """
        import wave
        import tempfile

        if self._closed:
            return
        rate = self.cfg.sample_rate
        if len(pcm) < int(rate * 0.35) * 2:  # sub-0.35 s: key chatter, skip
            return
        self.last_activity = asyncio.get_running_loop().time()
        with tempfile.NamedTemporaryFile(
            suffix=".wav", prefix="omavoice-", delete=False
        ) as tmp:
            path = tmp.name
        try:
            def _write() -> None:
                with wave.open(path, "wb") as w:
                    vosk_ch = self.cfg.channels
                    w.setnchannels(vosk_ch)
                    w.setsampwidth(2)
                    w.setframerate(rate)
                    w.writeframes(pcm)

            await asyncio.to_thread(_write)
            log.info("handy: transcribing %.1fs of speech",
                     len(pcm) / (rate * self.cfg.channels * 2))
            # handy links a GUI toolkit even for --transcribe-file: with no
            # display at all its tao/gtk event loop panics in ~50 ms (rc=101,
            # empty stdout) and every turn silently reads "(empty)". A daemon
            # started at boot (enabled unit racing Hyprland's display-env
            # import) has neither WAYLAND_DISPLAY nor DISPLAY — find the
            # socket ourselves instead of trusting the start-time environment.
            env = dict(os.environ)
            if not env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY"):
                runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
                try:
                    sockets = sorted(
                        n for n in os.listdir(runtime)
                        if n.startswith("wayland-") and not n.endswith(".lock")
                    )
                except OSError:
                    sockets = []
                if sockets:
                    env["WAYLAND_DISPLAY"] = sockets[0]
                    log.info("handy: no display env; using %s from %s",
                             sockets[0], runtime)
            proc = await asyncio.create_subprocess_exec(
                "handy", "--transcribe-file", path,
                "--model", self.cfg.handy_model, "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                out, err = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                raise
            text = ""
            for ln in (out or b"").decode(errors="replace").splitlines():
                ln = ln.strip()
                if ln.startswith("{"):
                    try:
                        text = json.loads(ln).get("text", "") or ""
                    except ValueError:
                        pass
            if not text and proc.returncode != 0:
                # A dead handy (GTK panic, bad model id) must not look like
                # silence: keep the tail of its stderr in the journal.
                tail = (err or b"").decode(errors="replace").strip().splitlines()
                log.warning("handy: exited rc=%s: %s", proc.returncode,
                            " | ".join(tail[-2:]) or "(no stderr)")
            log.info("turn: %s", text[:160] if text else "(empty)")
            if text and not self._closed:
                self.last_activity = asyncio.get_running_loop().time()
                task = asyncio.ensure_future(self._on_final(text))
                self._turn_tasks.add(task)
                task.add_done_callback(self._turn_tasks.discard)
        finally:
            with contextlib_suppress():
                os.unlink(path)

    # -- controls -------------------------------------------------------------

    async def cancel_response(self) -> None:
        """Stop the answer in flight — the person started talking over it."""
        self._abort_play.set()
        if self._brain_task and not self._brain_task.done():
            self._brain_task.cancel()

    async def cancel_tools(self) -> None:
        await self.cancel_response()

    async def say(self, text: str) -> None:
        await self._speak(text)

    async def open_floor(self) -> None:
        # Nothing to configure server-side; the first transcript opens the
        # floor here too (see _on_final).
        self._await_first_turn = False

    async def run(self) -> None:
        """Pump recognised phrases until closed. Audio arrives via send_audio."""
        self._drain_task = asyncio.create_task(self._feed())
        try:
            while not self._closed:
                await asyncio.sleep(0.25)
        finally:
            if self._drain_task:
                self._drain_task.cancel()
            if self._worker is not None:
                self._audio_q.put_nowait(None)
        log.info("local voice session ended")


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True
