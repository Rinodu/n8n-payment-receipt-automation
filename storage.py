"""Encrypted payloads, single-host process locks, and consistent encrypted backups."""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager, closing
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet


@contextmanager
def file_lock(path, blocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0)
        acquired = False
        try:
            if os.name == 'nt':
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                    acquired = True
                except BlockingIOError:
                    pass
            yield acquired
        finally:
            if acquired:
                if os.name == 'nt':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Vault:
    def __init__(self, folder, mode, config):
        keys = config.get('DATA_KEYS', '')
        index = config.get('INDEX_KEY', '')
        if mode == 'live' and (not keys or len(index) < 32):
            raise ValueError('Live requires DATA_KEYS (Fernet keys) and a stable INDEX_KEY >=32 characters')
        if not keys:
            # Demo only: this key is on the same disk. Live keys must be stored separately.
            path = Path(folder) / '.demo-key.json'
            with file_lock(str(path) + '.lock', blocking=True) as acquired:
                if not acquired:
                    raise RuntimeError('Key initialization busy')
                if not path.exists():
                    value = {'key': Fernet.generate_key().decode(), 'index': os.urandom(32).hex()}
                    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(descriptor, 'w') as stream:
                        json.dump(value, stream)
                value = json.loads(path.read_text())
                keys, index = value['key'], value['index']
        if not index:
            raise ValueError('INDEX_KEY is required when supplying DATA_KEYS')
        self.cipher = MultiFernet([Fernet(key.strip().encode()) for key in keys.split(',')])
        self.index = index.encode()

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value, sort_keys=True).encode()).decode()

    def open(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def fingerprint(self, value):
        return hmac.new(self.index, json.dumps(value, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def backup(self, db_path, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(':memory:')) as snapshot:
            source.backup(snapshot)
            encrypted = self.cipher.encrypt(snapshot.serialize())
        # Exclusive create prevents silently replacing the operator's last good backup.
        with destination.open('xb') as target:
            target.write(encrypted)
        return destination

    def restore(self, source, destination):
        destination = Path(destination)
        if destination.exists():
            raise ValueError('Restore destination must not exist; restore to a separate folder first')
        data = self.cipher.decrypt(Path(source).read_bytes())
        with closing(sqlite3.connect(':memory:')) as checked:
            checked.deserialize(data)
            if checked.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup failed integrity check')
            with closing(sqlite3.connect(destination)) as target:
                checked.backup(target)

    def rotate(self, connection):
        for table, columns in (('payments', ('payload', 'message')), ('orders', ('payload',)),
                               ('airtable_demo', ('fields',)), ('outbox', ('message',))):
            for column in columns:
                for row_id, value in connection.execute(f'SELECT id,{column} FROM {table} WHERE {column} IS NOT NULL').fetchall():
                    rotated = self.cipher.rotate(value.encode()).decode()
                    connection.execute(f'UPDATE {table} SET {column}=? WHERE id=?', (rotated, row_id))
