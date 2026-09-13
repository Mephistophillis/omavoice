import importlib
import tempfile
import unittest
from pathlib import Path


class ArchiveTests(unittest.TestCase):
    def test_archive_bounds_symlinks_and_stale_preview(self):
        from omavoice.archive import Archive
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = Archive(root)
            outside = root / 'outside'
            outside.write_text('{"secret": true}')
            (root / ('c'*32 + '.json')).symlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                archive.read('c'*32)
            with self.assertRaises(ValueError):
                archive.read('../outside')
            identity = 'a'*32
            archive.append(identity, {'request_id': '1', 'user': 'x', 'answer': 'x'})
            preview = archive.preview(identity)
            archive.append(identity, {'request_id': '2', 'user': 'x', 'answer': 'x'})
            self.assertFalse(archive.delete(preview['token'])['ok'])
            self.assertEqual(len(archive.read(identity)['turns']), 2)
            (root / ('b'*32 + '.json')).write_bytes(b'x' * 1100000)
            with self.assertRaises(ValueError):
                archive.read('b'*32)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_conversation_is_deduplicated_private_and_delete_is_confirmed(self):
        import omavoice
        self.assertIsNotNone(importlib.util.find_spec('omavoice.archive'), 'conversation archive missing')
        from omavoice.archive import Archive
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / '2026-01-01.md'
            legacy.write_text('legacy remains')
            archive = Archive(root)
            turn = {'request_id': 'b'*32, 'user': 'question', 'answer': 'answer'}
            archive.append('a'*32, turn)
            archive.append('a'*32, turn)
            self.assertEqual(len(archive.read('a'*32)['turns']), 1)
            self.assertEqual((root / ('a'*32 + '.json')).stat().st_mode & 0o777, 0o600)
            preview = archive.preview('a'*32)
            self.assertEqual(preview['count'], 1)
            self.assertFalse(archive.delete('wrong')['ok'])
            self.assertTrue(archive.delete(preview['token'])['ok'])
            self.assertFalse(archive.delete(preview['token'])['ok'])
            self.assertEqual(archive.list()['items'], [])
            self.assertEqual(legacy.read_text(), 'legacy remains')

    def test_failed_append_preserves_the_existing_archive(self):
        from omavoice.archive import Archive
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(Path(directory))
            identity = 'd'*32
            archive.append(identity, {'request_id': '1', 'user': 'q', 'answer': 'a'})
            with self.assertRaises(TypeError):
                # An unserializable turn must fail the write, not destroy
                # what is already on disk (no truncate-then-die).
                archive.append(identity, {'request_id': '2', 'user': object()})
            self.assertEqual(len(archive.read(identity)['turns']), 1)
            self.assertFalse(list(Path(directory).glob('*.tmp.*')), 'temp file left behind')

    def test_append_refuses_to_exceed_the_read_limit(self):
        from omavoice.archive import Archive
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(Path(directory))
            identity = 'e'*32
            archive.append(identity, {'request_id': '1', 'user': 'q', 'answer': 'x' * 700_000})
            with self.assertRaises(ValueError):
                # A conversation that can never be read back must never be
                # written: the limit is enforced before the replace.
                archive.append(identity, {'request_id': '2', 'user': 'q', 'answer': 'y' * 400_000})
            data = archive.read(identity)
            self.assertEqual([t['request_id'] for t in data['turns']], ['1'])
