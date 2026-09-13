import asyncio
import unittest
from omavoice.brain import Brain, Answer
from omavoice.config import Config


from omavoice.localvoice import LocalVoiceSession


class LocalCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_discards_recording_and_awaits_recognition(self):
        async def ignore(*args):
            pass
        session = LocalVoiceSession(Config(), on_audio=ignore, on_event=ignore, on_tool_call=ignore)
        session._handy = True
        session.begin_utterance()
        session._hold_buf.extend(b'old speech')
        entered = asyncio.Event()
        async def recognize():
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(recognize())
        session._turn_tasks.add(task)
        await entered.wait()
        await session.cancel_response()
        self.assertFalse(session._ptt_held_evt.is_set())
        self.assertEqual(session._hold_buf, b'')
        self.assertTrue(task.done())


class DaemonLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_n_q_matrix(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch, AsyncMock
        from omavoice.__main__ import Daemon
        for command in ('reset', 'stop'):
            for phase in ('idle', 'recording', 'recognition', 'model', 'tool', 'confirmation', 'synthesis', 'playback'):
                with self.subTest(command=command, phase=phase), tempfile.TemporaryDirectory() as directory:
                    cfg = Config()
                    cfg.state_dir = Path(directory)
                    with patch.object(Daemon, '_load_preferences'):
                        daemon = Daemon(cfg)
                    daemon.mic.stop = AsyncMock()
                    daemon._flush_playback = AsyncMock()
                    daemon.start_session = AsyncMock(return_value={'ok': True})
                    async def ignore(*args):
                        return ''
                    session = LocalVoiceSession(cfg, on_audio=ignore, on_event=ignore, on_tool_call=ignore)
                    session._handy = True
                    daemon.session = session
                    old_id = daemon.conversation_id
                    if phase == 'recording':
                        session.begin_utterance()
                        daemon._ptt_held = True
                        session._hold_buf.extend(b'old')
                    entered = asyncio.Event()
                    async def work():
                        entered.set()
                        await asyncio.Event().wait()
                    task = None
                    if phase not in ('idle', 'recording', 'playback'):
                        task = asyncio.create_task(work())
                        session._turn_tasks.add(task)
                        await entered.wait()
                    if phase == 'playback':
                        daemon._play_queue.put_nowait(b'old')
                    result = await daemon._on_command({'cmd': command})
                    self.assertNotEqual(daemon.conversation_id, old_id)
                    self.assertEqual(result['snapshot']['turns'], [])
                    self.assertFalse(daemon._ptt_held)
                    self.assertEqual(session._hold_buf, b'')
                    if task:
                        self.assertTrue(task.done())
                    self.assertTrue(daemon._play_queue.empty())
                    daemon.mic.stop.assert_awaited()
                    self.assertEqual(len(daemon.brain._groq_history), 1)
                    if command == 'stop':
                        self.assertIsNone(daemon.session)

    async def test_background_closes_capture_without_cancelling_answer(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch, AsyncMock
        from omavoice.__main__ import Daemon
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config()
            cfg.state_dir = Path(directory)
            with patch.object(Daemon, '_load_preferences'):
                daemon = Daemon(cfg)
            daemon.session = AsyncMock()
            daemon.mic.stop = AsyncMock()
            daemon._ptt_held = True
            before = daemon.conversation_id
            result = await daemon._on_command({'cmd': 'background'})
            daemon.mic.stop.assert_awaited()
            self.assertFalse(daemon._ptt_held)
            self.assertEqual(daemon.conversation_id, before)
            daemon.session.cancel_response.assert_not_awaited()
            self.assertTrue(result['snapshot']['background'])

    async def test_idle_interrupt_does_not_claim_success(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch, AsyncMock
        from omavoice.__main__ import Daemon
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config()
            cfg.state_dir = Path(directory)
            with patch.object(Daemon, '_load_preferences'):
                daemon = Daemon(cfg)
            daemon._flush_playback = AsyncMock()
            result = await daemon._on_command({'cmd': 'cancel'})
            self.assertIs(result.get('cancelled'), False)
            self.assertEqual(result['snapshot']['turns'], [])

    async def test_answer_snapshot_archive_and_reconnect(self):
        import tempfile, json
        from pathlib import Path
        from unittest.mock import patch
        from omavoice.__main__ import Daemon
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config()
            cfg.state_dir = Path(directory)
            cfg.socket_path = Path(directory) / 'ipc.sock'
            with patch.object(Daemon, '_load_preferences'):
                daemon = Daemon(cfg)
            async def answer(query):
                return Answer('spoken', markdown='details', followups=['next?'])
            daemon.brain.ask = answer
            await daemon._on_command({'cmd': 'ask', 'query': 'question'})
            snapshot = await daemon._on_command({'cmd': 'snapshot'})
            self.assertTrue(snapshot['ok'])
            self.assertEqual(snapshot['turns'][0]['answer'], 'spoken')
            self.assertTrue(snapshot['request_id'])
            listing = await daemon._on_command({'cmd': 'history'})
            self.assertEqual(listing['items'][0]['conversation_id'], snapshot['conversation_id'])
            await daemon.server.start()
            try:
                reader, writer = await asyncio.open_unix_connection(str(cfg.socket_path))
                first = json.loads(await asyncio.wait_for(reader.readline(), 1))
                self.assertEqual(first['type'], 'snapshot')
                self.assertEqual(first['turns'], snapshot['turns'])
                writer.close()
                await writer.wait_closed()
            finally:
                await daemon.server.stop()

    async def test_reset_failure_still_publishes_reset_and_snapshot(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch, AsyncMock
        from omavoice.__main__ import Daemon
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config()
            cfg.state_dir = Path(directory)
            with patch.object(Daemon, '_load_preferences'):
                daemon = Daemon(cfg)
            daemon.mic.stop = AsyncMock()
            daemon._flush_playback = AsyncMock()
            daemon.start_session = AsyncMock(return_value={'ok': False, 'error': 'mic gone'})
            daemon.session = AsyncMock()
            broadcast_kinds = []
            original_broadcast = daemon.server.broadcast
            daemon.server.broadcast = lambda m: (broadcast_kinds.append(m.get('type')),
                                                 original_broadcast(m))
            old_id = daemon.conversation_id
            result = await daemon._on_command({'cmd': 'reset'})
            self.assertFalse(result['ok'], 'a failed restart must not read as success')
            self.assertIn('reset', broadcast_kinds)
            self.assertIn('snapshot', broadcast_kinds)
            self.assertNotEqual(result['conversation_id'], old_id)
            self.assertEqual(result['snapshot']['turns'], [])

    async def test_reset_invalidates_typed_request_before_clearing_context(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch, AsyncMock
        from omavoice.__main__ import Daemon
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config()
            cfg.state_dir = Path(directory)
            with patch.object(Daemon, '_load_preferences'):
                daemon = Daemon(cfg)
            daemon.mic.stop = AsyncMock()
            daemon._flush_playback = AsyncMock()
            entered = asyncio.Event()
            async def wait(query):
                entered.set()
                await asyncio.Event().wait()
            daemon.brain._ask_hermes = wait
            daemon.brain.backend = 'hermes'
            task = asyncio.create_task(daemon._on_command({'cmd': 'ask', 'query': 'old'}))
            await entered.wait()
            result = await daemon._on_command({'cmd': 'reset'})
            self.assertTrue(task.done(), 'N must stop text requests too')
            self.assertIn('conversation_id', result)
            self.assertEqual(result['snapshot']['turns'], [])
            self.assertEqual(len(daemon.brain._hermes_history), 1)


class BrainCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_rolls_back_partial_tool_chain(self):
        brain = Brain(Config())
        brain.backend = 'groq'
        original = list(brain._groq_history)
        entered = asyncio.Event()
        async def partial(query):
            brain._groq_history.extend([{'role': 'user', 'content': query},
                {'role': 'assistant', 'tool_calls': [{'id': 'unfinished'}]}])
            entered.set()
            await asyncio.Event().wait()
        brain._ask_groq = partial
        task = asyncio.create_task(brain.ask('first'))
        await entered.wait()
        await brain.cancel()
        self.assertTrue(task.done(), 'cancel must await the HTTP/tool task')
        self.assertEqual(brain._groq_history, original)
        async def next_query(query):
            self.assertEqual(brain._groq_history, original)
            return Answer('next succeeds')
        brain._ask_groq = next_query
        self.assertEqual((await brain.ask('next')).spoken, 'next succeeds')
