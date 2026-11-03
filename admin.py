"""Local-only operator CLI. No HTTP admin endpoints."""
import argparse
import json
import os
import time
from pathlib import Path
from processor import Processor, Problem
from storage import file_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default=os.environ.get('DATA_DIR', 'data'))
    parser.add_argument('--mode', choices=('demo', 'live'), default=os.environ.get('MODE', 'demo'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    order = commands.add_parser('register-order')
    order.add_argument('file')
    backup = commands.add_parser('backup')
    backup.add_argument('destination')
    restore = commands.add_parser('restore')
    restore.add_argument('source')
    restore.add_argument('destination', help='New SQLite file; never overwrite an existing ledger')
    commands.add_parser('rotate-data')
    commands.add_parser('maintain')
    reconcile = commands.add_parser('reconcile-sent')
    reconcile.add_argument('payment_id')
    reconcile.add_argument('--provider-id', required=True)
    reconcile.add_argument('--evidence', required=True, help='Private provider evidence reference; not a secret or customer data')
    retry = commands.add_parser('retry')
    retry.add_argument('payment_id')
    retry.add_argument('--evidence', required=True)
    args = parser.parse_args()
    processor = Processor(args.data_dir, args.mode, dict(os.environ))
    if args.command == 'status':
        print(json.dumps(processor.metrics(), indent=2))
        with processor.connect() as db:
            rows = db.execute("SELECT id,stage,queue,attempts,last_error FROM payments WHERE queue IN ('dead','manual')").fetchall()
        print(json.dumps([dict(zip(('payment_id','stage','queue','attempts','last_error'), row)) for row in rows], indent=2))
    elif args.command == 'register-order':
        processor.register_order(json.loads(Path(args.file).read_text()))
        print('Trusted order registered.')
    elif args.command == 'backup':
        processor.vault.backup(processor.db, args.destination)
        print('Encrypted backup created. Keep decryption keys separately.')
    elif args.command == 'restore':
        processor.vault.restore(args.source, args.destination)
        print('Restored to a new file; validate it before switching the stopped service.')
    elif args.command == 'maintain':
        if not processor.maintenance():
            raise SystemExit('Dispatcher busy; retry later.')
        print('Backup and retention completed.')
    else:
        with file_lock(processor.dispatch_lock) as acquired:
            if not acquired:
                raise SystemExit('Dispatcher busy; retry later.')
            with processor.connect() as db:
                if args.command == 'rotate-data':
                    processor.vault.rotate(db)
                else:
                    if len(args.evidence.strip()) < 8:
                        raise SystemExit('Evidence reference must be at least 8 characters.')
                    row = db.execute('SELECT stage,started FROM payments WHERE id=?', (args.payment_id,)).fetchone()
                    if not row:
                        raise SystemExit('Payment not found.')
                    evidence_hash = processor.vault.fingerprint(args.evidence)[:16]
                    if args.command == 'reconcile-sent':
                        if row[0] != 'sending' or not args.provider_id.strip():
                            raise SystemExit('Only uncertain sending payments can be reconciled with provider evidence.')
                        db.execute("UPDATE payments SET stage='sent',queue='done',provider_id=?,updated=? WHERE id=?",
                                   (args.provider_id, time.time(), args.payment_id))
                    else:
                        if row[0] == 'sent':
                            raise SystemExit('Already sent; retry forbidden.')
                        if row[1] and time.time() - row[1] >= 23*3600:
                            raise SystemExit('Email retry window expired. Reconcile provider delivery; automatic reset forbidden.')
                        db.execute("UPDATE payments SET queue='pending',attempts=0,next_at=0,updated=? WHERE id=?", (time.time(), args.payment_id))
                    processor.audit(db, args.payment_id, args.command + ':' + evidence_hash)
            if args.command == 'rotate-data':
                path = processor.folder / (processor.mode + '-keycheck.json')
                value = json.loads(path.read_text())
                value['encrypted'] = processor.vault.seal('receipt-v2')
                path.write_text(json.dumps(value))
            print('Operation completed; do not discard old keys until backup recovery is verified.')


if __name__ == '__main__':
    main()
