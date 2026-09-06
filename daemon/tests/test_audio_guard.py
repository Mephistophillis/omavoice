"""Prevent speaker echo reaching Realtime while keeping protected barge-in."""

import array
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from omavoice.__main__ import Daemon, _ECHO_TAIL_SECONDS
from omavoice.audio import AutoGain, NoiseGate, Speaker, rms_level
from omavoice.devices import AEC_SINK, AEC_SOURCE, Devices


PCM = array.array("h", [5000, -5000] * 240).tobytes()


class AudioGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.daemon = Daemon.__new__(Daemon)
        d = self.daemon
        d.cfg = SimpleNamespace(debug=False, chunk_ms=20)
        d.session = SimpleNamespace(connected=True, send_audio=AsyncMock())
        d.devices = Devices("mic", "speakers", False, "no AEC")
        d.mic = SimpleNamespace(target="mic", stop=AsyncMock())
        d.speaker = SimpleNamespace(
            target="speakers", playing=False, remaining=0.0,
            write=AsyncMock(), flush_now=AsyncMock(),
        )
        d.gate = NoiseGate(None, hangover_ms=1400, chunk_ms=20)
        d.autogain = AutoGain()
        d.autogain.gain = 16.0
        d._mic_dump = d._dump = d._voice_dump = None
        d._play_queue = asyncio.Queue()
        d._play_writing = False
        d._quiet_after = 0.0
        d._pending_level = 0.0
        d._pending_bands = [0.0] * 4
        d._speech_chunks = 0
        d._check_deaf_server = Mock()
        d._set_state = lambda state: setattr(d, "state", state)
        d._emit = Mock()
        d.state = "listening"
        d.paused = False

    async def input_chunk(self, pcm=PCM):
        self.daemon._on_input_chunk(pcm, rms_level(pcm), [0.0] * 4)
        await asyncio.sleep(0)
        return self.daemon.session.send_audio.call_args.args[0]

    async def test_physical_speaker_echo_is_silenced_even_with_open_gate_and_high_gain(self):
        d = self.daemon
        d.speaker.playing = True
        d.speaker.remaining = 3.0
        d.gate.step(0.8)
        self.assertEqual(await self.input_chunk(), bytes(len(PCM)))
        self.assertFalse(d.gate._open)
        self.assertEqual(d.autogain.gain, 16.0)
        self.assertEqual(d._speech_chunks, 0)

    async def test_missing_route_metadata_fails_closed_during_playback(self):
        self.daemon.devices = None
        self.daemon.speaker.playing = True
        self.assertEqual(await self.input_chunk(), bytes(len(PCM)))

    async def test_queued_audio_is_protected_before_player_receives_it(self):
        await self.daemon._on_audio(PCM)
        self.assertFalse(self.daemon.speaker.playing)
        self.assertEqual(await self.input_chunk(), bytes(len(PCM)))

    async def test_inflight_write_is_protected_when_queue_and_timer_are_empty(self):
        d = self.daemon
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_write(pcm):
            entered.set()
            await release.wait()

        d.speaker.write = blocked_write
        d._play_queue.put_nowait(PCM)
        pump = asyncio.create_task(d._play_pump())
        try:
            await entered.wait()
            self.assertTrue(d._play_queue.empty())
            self.assertFalse(d.speaker.playing)
            self.assertEqual(await self.input_chunk(), bytes(len(PCM)))
        finally:
            release.set()
            pump.cancel()
            await pump

    async def test_tail_is_silenced_then_new_user_speech_passes(self):
        d = self.daemon
        d._quiet_after = asyncio.get_running_loop().time() + 0.9
        self.assertEqual(await self.input_chunk(), bytes(len(PCM)))
        d._quiet_after = asyncio.get_running_loop().time() - 0.01
        self.assertNotEqual(await self.input_chunk(), bytes(len(PCM)))

    async def test_valid_aec_pair_keeps_voice_barge_in(self):
        d = self.daemon
        d.devices = Devices(AEC_SOURCE, AEC_SINK, False, "AEC", echo_cancelled=True)
        d.mic.target, d.speaker.target = AEC_SOURCE, AEC_SINK
        d.speaker.playing = True
        self.assertEqual(await self.input_chunk(), PCM)

    async def test_headphones_keep_voice_barge_in(self):
        d = self.daemon
        d.devices = Devices("mic", "headphones", True, "worn")
        d.speaker.target = "headphones"
        d.speaker.playing = True
        self.assertEqual(await self.input_chunk(), PCM)

    async def test_microphone_fallback_invalidates_aec_exemption(self):
        d = self.daemon
        d.devices = Devices(AEC_SOURCE, AEC_SINK, False, "AEC", echo_cancelled=True)
        d.mic.target, d.speaker.target = "physical-fallback", AEC_SINK
        d.speaker.playing = True
        self.assertEqual(await self.input_chunk(), bytes(len(PCM)))

    async def test_output_change_invalidates_headphone_exemption(self):
        d = self.daemon
        d.devices = Devices("mic", "headphones", True, "worn")
        d.speaker.playing = True
        self.assertEqual(await self.input_chunk(), bytes(len(PCM)))

    async def test_flush_keeps_short_tail_without_waiting_for_discarded_seconds(self):
        d = self.daemon
        d.state = "speaking"
        d.session.cancel_response = AsyncMock()
        d.speaker.playing = True
        d.speaker.remaining = 30.0
        d._room_is_loud()

        async def flush():
            d.speaker.playing = False
            d.speaker.remaining = 0.0

        d.speaker.flush_now = flush
        before = asyncio.get_running_loop().time()
        await d._on_event({"type": "input_audio_buffer.speech_started"})
        self.assertGreaterEqual(d._quiet_after, before + _ECHO_TAIL_SECONDS)
        self.assertLess(d._quiet_after, before + _ECHO_TAIL_SECONDS + 0.1)
        self.assertEqual(await self.input_chunk(), bytes(len(PCM)))

    async def test_resume_refreshes_route_without_replacing_conversation(self):
        d = self.daemon
        d.paused = True
        d.cfg.input_target = d.cfg.output_target = ""
        d._bad_inputs = set()
        d._broadcast_audio = AsyncMock()
        d.mic.start = AsyncMock()
        d.speaker.start = AsyncMock()
        conversation = d.session
        aec = Devices(AEC_SOURCE, AEC_SINK, False, "restored AEC", echo_cancelled=True)
        with patch("omavoice.__main__.device_choice.resolve", AsyncMock(return_value=aec)) as resolve:
            result = await d.start_session()
        self.assertEqual(result, {"ok": True, "resumed": True})
        resolve.assert_awaited_once_with("", "", avoid=set())
        self.assertIs(d.session, conversation)
        self.assertIs(d.devices, aec)
        self.assertEqual((d.mic.target, d.speaker.target), (AEC_SOURCE, AEC_SINK))
        d.mic.start.assert_awaited_once()
        d.speaker.start.assert_awaited_once()
        self.assertIsNotNone(d.speaker.verify_target)

    async def test_headphones_removed_while_paused_enable_speaker_guard_on_resume(self):
        d = self.daemon
        d.devices = Devices("headset-mic", "headphones", True, "old headphones")
        d.mic.target, d.speaker.target = "headset-mic", "headphones"
        d.paused = True
        d.cfg.input_target = d.cfg.output_target = ""
        d._bad_inputs = set()
        d._broadcast_audio = AsyncMock()
        d.mic.start = d.speaker.start = AsyncMock()
        speakers = Devices("mic", "speakers", False, "headphones unplugged")
        with patch("omavoice.__main__.device_choice.resolve", AsyncMock(return_value=speakers)):
            await d.start_session()
        d.speaker.playing = True
        self.assertEqual(await self.input_chunk(), bytes(len(PCM)))

    async def test_resume_stays_paused_until_both_audio_devices_are_ready(self):
        d = self.daemon
        d.paused = True
        d.cfg.input_target = d.cfg.output_target = ""
        d._bad_inputs = set()
        d._broadcast_audio = AsyncMock()

        async def start_device():
            self.assertTrue(d.paused)
            await d._on_audio(PCM)
            self.assertTrue(d._play_queue.empty())

        d.mic.start = start_device
        d.speaker.start = start_device
        with patch("omavoice.__main__.device_choice.resolve", AsyncMock(return_value=d.devices)):
            await d.start_session()
        self.assertFalse(d.paused)

    async def test_changed_kept_streams_stop_before_targets_are_reassigned(self):
        d = self.daemon
        d.cfg.input_target = d.cfg.output_target = ""
        d._bad_inputs = set()
        d._broadcast_audio = AsyncMock()
        # Model start()'s real semantics: an existing process keeps its device,
        # regardless of later assignments to the wrapper's target attribute.
        actual = {"mic": d.mic.target, "speaker": d.speaker.target}

        async def stop_mic():
            self.assertEqual(d.mic.target, "mic")
            actual["mic"] = None

        async def stop_speaker():
            self.assertEqual(d.speaker.target, "speakers")
            actual["speaker"] = None

        d.mic.stop = stop_mic
        d.speaker.flush_now = stop_speaker
        aec = Devices(AEC_SOURCE, AEC_SINK, False, "restored AEC", echo_cancelled=True)
        with patch("omavoice.__main__.device_choice.resolve", AsyncMock(return_value=aec)):
            await d._select_audio_devices()
        actual["mic"] = actual["mic"] or d.mic.target
        actual["speaker"] = actual["speaker"] or d.speaker.target
        self.assertEqual(actual, {"mic": AEC_SOURCE, "speaker": AEC_SINK})
        self.assertTrue(d._allows_voice_interruption())

    async def test_unchanged_route_preserves_existing_audio_streams(self):
        d = self.daemon
        d.cfg.input_target = d.cfg.output_target = ""
        d._bad_inputs = set()
        d._broadcast_audio = AsyncMock()
        with patch("omavoice.__main__.device_choice.resolve", AsyncMock(return_value=d.devices)):
            await d._select_audio_devices()
        d.mic.stop.assert_not_awaited()
        d.speaker.flush_now.assert_not_awaited()


class SpeakerTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_output_is_invalidated_before_pcm_is_written(self):
        speaker = Speaker(SimpleNamespace(
            output_target=AEC_SINK, sample_rate=24000, channels=1,
        ))
        speaker.verify_target = AsyncMock(return_value=False)
        writes = []
        process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(
            write=lambda pcm: writes.append((speaker.target, pcm)), drain=AsyncMock(),
        ))
        with patch("omavoice.audio._pdeathsig_argv", return_value=["pw-play"]), \
                patch("omavoice.audio.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            await speaker.write(PCM)
        speaker.verify_target.assert_awaited_once_with(AEC_SINK)
        self.assertEqual(writes, [("", PCM)])
        self.assertNotIn("--target", spawn.call_args.args)

    async def test_failed_output_verification_does_not_keep_aec_exemption(self):
        speaker = Speaker(SimpleNamespace(
            output_target=AEC_SINK, sample_rate=24000, channels=1,
        ))
        speaker.verify_target = AsyncMock(side_effect=OSError("audio server unavailable"))
        with patch("omavoice.audio._pdeathsig_argv", return_value=["pw-play"]), \
                patch("omavoice.audio.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            await speaker.start()
        self.assertEqual(speaker.target, "")
        self.assertNotIn("--target", spawn.call_args.args)

    async def test_existing_output_keeps_its_explicit_target(self):
        speaker = Speaker(SimpleNamespace(
            output_target=AEC_SINK, sample_rate=24000, channels=1,
        ))
        speaker.verify_target = AsyncMock(return_value=True)
        with patch("omavoice.audio._pdeathsig_argv", return_value=["pw-play"]), \
                patch("omavoice.audio.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            await speaker.start()
        speaker.verify_target.assert_awaited_once_with(AEC_SINK)
        self.assertEqual(speaker.target, AEC_SINK)
        self.assertIn(AEC_SINK, spawn.call_args.args)

    async def test_written_pcm_counts_as_playing_before_drain_returns(self):
        speaker = Speaker(SimpleNamespace(
            output_target="", sample_rate=24000, channels=1,
        ))
        entered, release = asyncio.Event(), asyncio.Event()

        async def drain():
            entered.set()
            await release.wait()

        speaker._proc = SimpleNamespace(
            returncode=None, stdin=SimpleNamespace(write=Mock(), drain=drain),
        )
        writer = asyncio.create_task(speaker.write(PCM * 50))
        try:
            await entered.wait()
            self.assertTrue(speaker.playing)
            self.assertGreater(speaker.remaining, 0.5)
        finally:
            release.set()
            await writer


if __name__ == "__main__":
    unittest.main()
