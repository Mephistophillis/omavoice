"""Private per-conversation archive. Legacy Markdown is never mutated."""
from __future__ import annotations
import contextlib
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

_READ_LIMIT = 1024 * 1024


class Archive:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink():
            raise ValueError('archive directory is a symlink')
        os.chmod(root, 0o700)
        self._previews = {}

    def _path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
            raise ValueError('invalid conversation id')
        return self.root / (identity + '.json')

    def read(self, identity):
        fd = os.open(self._path(identity), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            import stat
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError('not a regular archive file')
            raw = handle.read(_READ_LIMIT + 1)
        if len(raw) > _READ_LIMIT:
            raise ValueError('archive conversation exceeds read limit')
        return json.loads(raw)

    def append(self, identity, turn):
        path = self._path(identity)
        try:
            data = self.read(identity)
        except FileNotFoundError:
            data = {'conversation_id': identity, 'started_at': time.time(),
                    'ended_at': None, 'turns': []}
        if any(t['request_id'] == turn['request_id'] for t in data['turns']):
            return
        data['turns'].append(turn)
        # Serialize and enforce the read limit BEFORE touching the file: the
        # old truncate-then-write destroyed the whole archive when json.dump
        # died mid-way, and a conversation that can never be read back is not
        # an archive, it is a dead file that also refuses new turns.
        payload = json.dumps(data, ensure_ascii=False).encode()
        if len(payload) > _READ_LIMIT:
            raise ValueError('archived conversation would exceed the read limit')
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=path.name + '.tmp.')
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic within the filesystem: a concurrent reader sees either
            # the whole previous archive or the whole new one, never a gap.
            os.replace(tmp, path)
            tmp = None
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def list(self):
        return {'items': [{'conversation_id': p.stem} for p in sorted(self.root.glob('*.json'))
                          if re.fullmatch('[0-9a-f]{32}', p.stem) and not p.is_symlink()]}

    def preview(self, scope):
        ids = [x['conversation_id'] for x in self.list()['items']] if scope == 'all' else [scope]
        paths = [self._path(x) for x in ids]
        token = secrets.token_urlsafe(32)
        self._previews[token] = [(p, p.stat().st_mtime_ns, p.stat().st_size) for p in paths]
        return {'ok': True, 'token': token, 'count': len(paths),
                'bytes': sum(p.stat().st_size for p in paths), 'scope': scope}

    def delete(self, token):
        paths = self._previews.pop(token, None)
        if paths is None:
            return {'ok': False, 'error': 'invalid confirmation token'}
        if any(p.is_symlink() or not p.exists() or
               (p.stat().st_mtime_ns, p.stat().st_size) != (mtime, size)
               for p, mtime, size in paths):
            return {'ok': False, 'error': 'archive changed; preview again'}
        for path, _, _ in paths:
            path.unlink()
        return {'ok': True, **self.list()}
