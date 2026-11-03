"""Migrate a stopped v1 ledger into a NEW encrypted v2 folder; source remains unchanged."""
import argparse
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from processor import Processor, validate
from storage import file_lock


def migrate(source, destination, mode='demo', config=None):
    source, destination = Path(source), Path(destination)
    if not source.is_file() or destination.exists():
        raise ValueError('Source must exist; destination must be a new folder')
    with file_lock(str(source) + '.dispatch.lock') as acquired:
        if not acquired:
            raise ValueError('Stop source service before migration')
        processor = Processor(destination, mode, config)
        processor.vault.backup(source, destination / 'legacy-backup.fernet')
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as legacy, processor.connect() as target:
            legacy.row_factory = sqlite3.Row
            for row in legacy.execute('SELECT * FROM payments'):
                payment = validate(json.loads(row['payload']))
                # v1 did not record durable queue attempts; uncertain emails need operator review.
                queue = 'done' if row['stage'] == 'sent' else ('manual' if row['stage'] == 'sending' else 'pending')
                target.execute('INSERT INTO payments(id,payload,fingerprint,stage,started,provider_id,message,created,updated,queue) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (row['id'], processor.vault.seal(payment), processor.vault.fingerprint(payment), row['stage'], row['started'], row['provider_id'],
                     processor.vault.seal(json.loads(row['message'])) if row['message'] else None, time.time(), time.time(), queue))
                processor.audit(target, row['id'], 'migrated_v1')
        return processor


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('new_folder')
    parser.add_argument('--mode', choices=('demo','live'), default='demo')
    args = parser.parse_args()
    migrate(Path(args.source).resolve(), args.new_folder, args.mode, dict(os.environ))
    print('Migration complete. Original plaintext source retained privately; verify restored v2 before retiring it.')
