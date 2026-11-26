"""Private, integrity-checked strategy snapshots on the persistent data volume."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import time

RETENTION = 30 * 86400
ID = re.compile(r'^[a-f0-9]{32}$')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')


class StrategyVault:
    def __init__(self, root):
        self.root = Path(root)
        for path in (self.root, self.root/'trash', self.root/'versions'):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)

    def _path(self, bucket, id):
        if bucket not in ('trash', 'versions') or not ID.fullmatch(id):
            raise ValueError('Invalid archive entry.')
        path = self.root/bucket/id
        if path.is_symlink() or not path.is_dir():
            raise ValueError('That archive entry no longer exists.')
        return path

    def save(self, bucket, name, metadata, content):
        if bucket not in ('trash', 'versions') or not NAME.fullmatch(name):
            raise ValueError('Invalid strategy archive.')
        now = time.time()
        id = secrets.token_hex(16)
        parent = self.root/bucket
        tmp = parent/('.pending-'+id)
        tmp.mkdir(mode=0o700)
        record = {'id': id, 'name': name, 'metadata': metadata,
                  'saved_utc': dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
                  'expires': now + RETENTION if bucket == 'trash' else None,
                  'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
        try:
            (tmp/'strategy.py').write_bytes(content)
            (tmp/'record.json').write_text(json.dumps(record), encoding='utf-8')
            os.replace(tmp, parent/id)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)
        return record

    def read(self, bucket, id, allow_expired=False):
        path = self._path(bucket, id)
        for name in ('record.json', 'strategy.py'):
            p = path/name
            if not stat.S_ISREG(os.lstat(p).st_mode) or p.stat().st_size > 1_100_000:
                raise ValueError('Archive entry is invalid.')
        record = json.loads((path/'record.json').read_text(encoding='utf-8'))
        content = (path/'strategy.py').read_bytes()
        if (not isinstance(record, dict) or record.get('id') != id
                or not isinstance(record.get('name'), str) or not NAME.fullmatch(record['name'])
                or hashlib.sha256(content).hexdigest() != record.get('sha256')):
            raise ValueError('Archive integrity check failed. Nothing was restored.')
        if bucket == 'trash' and (not isinstance(record.get('expires'), (int, float))
                                  or (not allow_expired and record['expires'] <= time.time())):
            raise ValueError('The 30-day recovery period has expired.')
        return record, content

    def remove(self, bucket, id):
        shutil.rmtree(self._path(bucket, id))

    def purge_expired(self):
        for p in (self.root/'trash').iterdir():
            if not ID.fullmatch(p.name) or p.is_symlink():
                continue
            try:
                record, _ = self.read('trash', p.name, allow_expired=True)
                if record['expires'] <= time.time():
                    self.remove('trash', p.name)
            except (ValueError, OSError, TypeError):
                continue

    def entries(self, bucket):
        if bucket not in ('trash', 'versions'):
            raise ValueError('Invalid archive.')
        self.purge_expired()
        rows = []
        for p in (self.root/bucket).iterdir():
            if not ID.fullmatch(p.name):
                continue
            try:
                record, _ = self.read(bucket, p.name)
                rows.append(record)
            except (ValueError, OSError, TypeError):
                continue
        return sorted(rows, key=lambda r: r['saved_utc'], reverse=True)
