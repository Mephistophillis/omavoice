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
import re
import subprocess
from collections.abc import Awaitable, Callable

from .config import Config

log = logging.getLogger("omavoice.localvoice")

MAX_QUERY_CHARS = 2000

# edge-tts streams MP3; ffmpeg converts to PCM16 at the daemon's pipeline rate
# (cfg.sample_rate — 16 kHz in local mode), which is what on_audio expects.

# Conversational turns answered without the brain. Everything else —
# any fact, any question about files or the world — goes to ask_agent,
# mirroring the hard rule the original realtime instructions had.
_CHAT_ONLY = re.compile(
    r"^(привет|здравствуй|здравствуйте|пока|до связи|спасибо|благодарю"
    r"|ага|угу|ок(ей)?|хорошо|ладно|понятно|да|нет|что|повтори|стоп)\b",
    re.IGNORECASE,
)

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
        if self._worker is not None:
            self._audio_q.put_nowait(None)
            self._worker = None
            self._deliver_final = None
        # The recognizer is dropped but the MODEL stays in the cache: the next
        # session reuses it. KaldiRecognizer holds no audio of its own.
        self._recognizer = None
        self._model = None

    # -- outbound (daemon -> session) ----------------------------------------

    async def send_audio(self, pcm: bytes) -> None:
        """Queue mic PCM16 for vosk. Called from the daemon's mic loop."""
        if self._closed or self._model is None:
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
        rec.SetWords(False)
        self._recognizer = rec
        while True:
            pcm = self._audio_q.get()
            if pcm is None:
                return
            try:
                if rec.AcceptWaveform(pcm):
                    final = json.loads(rec.Result()).get("text", "")
                    if final:
                        self._deliver_final(final)
            except Exception:
                log.exception("vosk recognition failed")
                return

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

        # Conversational turns (greeting / thanks / bye) are answered by the
        # voice layer itself; everything else — every fact, every question —
        # goes to the brain, exactly like the original's hard rule.
        if _CHAT_ONLY.match(text):
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
        """
        import edge_tts

        text = (text or "").strip()
        if not text or self._closed:
            return
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
