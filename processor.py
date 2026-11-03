"""Durable, encrypted single-host payment receipt service."""
import hashlib
import hmac
import json
import logging
import os
import random
import re
import socket
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from gateway import NoRedirect, get_status, verify_midtrans
from storage import Vault, file_lock

LOG = logging.getLogger('receipts')


class Problem(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def validate(data):
    if not isinstance(data, dict):
        raise Problem(422, 'invalid_object')
    result = {}
    for key in ('payment_id', 'order_id'):
        value = data.get(key)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
            raise Problem(422, 'invalid_' + key)
        result[key] = value
    email = data.get('email')
    if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', email):
        raise Problem(422, 'invalid_email')
    amount = data.get('amount')
    if type(amount) is not int or not 0 < amount <= 1_000_000_000:
        raise Problem(422, 'invalid_amount')
    if data.get('currency') != 'IDR' or data.get('status') != 'paid':
        raise Problem(422, 'only_paid_idr_supported')
    try:
        paid_at = datetime.fromisoformat(data['paid_at'].replace('Z', '+00:00'))
        if paid_at.utcoffset() is None:
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise Problem(422, 'invalid_paid_at')
    result.update(email=email, amount=amount, currency='IDR', status='paid',
                  paid_at=paid_at.astimezone(timezone.utc).isoformat())
    return result


def api(method, url, token, body, extra=None):
    if not url.startswith(('https://api.airtable.com/v0/', 'https://api.resend.com/emails')):
        raise ValueError('Outbound API host not allowed')
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    headers.update(extra or {})
    request = Request(url, data=json.dumps(body).encode(), headers=headers, method=method)
    with build_opener(NoRedirect).open(request, timeout=15) as response:
        data = response.read(65537)
        if len(data) > 65536:
            raise ValueError('Oversized upstream response')
        return json.loads(data)


class Processor:
    def __init__(self, folder, mode='demo', config=None, transport=api):
        if mode not in ('demo', 'live'):
            raise ValueError('MODE must be demo or live')
        self.folder, self.mode = Path(folder), mode
        self.config, self.transport = config or {}, transport
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = self.folder / ('receipts-' + mode + '.sqlite3')
        self.dispatch_lock = str(self.db) + '.dispatch.lock'
        self.vault = Vault(self.folder, mode, self.config)
        if mode == 'live':
            for key in ('AIRTABLE_TOKEN', 'AIRTABLE_BASE_ID', 'AIRTABLE_TABLE_ID', 'RESEND_API_KEY', 'EMAIL_FROM', 'TEST_RECIPIENT', 'MIDTRANS_SERVER_KEY'):
                if not self.config.get(key):
                    raise ValueError('Missing ' + key)
            for key, prefix in (('AIRTABLE_BASE_ID', 'app'), ('AIRTABLE_TABLE_ID', 'tbl')):
                if not re.fullmatch(prefix + r'[A-Za-z0-9]+', self.config[key]):
                    raise ValueError('Invalid ' + key)
            if '\r' in self.config['EMAIL_FROM'] or '\n' in self.config['EMAIL_FROM']:
                raise ValueError('Invalid sender')
        with self.connect() as db:
            existing = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payments'").fetchone()
            if existing and 'fingerprint' not in {r[1] for r in db.execute('PRAGMA table_info(payments)')}:
                raise ValueError('Legacy plaintext ledger: migrate using migrate_legacy.py before starting v2')
            db.execute('CREATE TABLE IF NOT EXISTS payments (id TEXT PRIMARY KEY, payload TEXT, fingerprint TEXT NOT NULL, stage TEXT NOT NULL, started REAL, provider_id TEXT, message TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, queue TEXT NOT NULL DEFAULT \'pending\', last_error TEXT, created REAL NOT NULL, updated REAL NOT NULL, airtable_id TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS airtable_demo (id TEXT PRIMARY KEY, fields TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, message TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, at REAL NOT NULL, ref TEXT NOT NULL, event TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS system_state (id TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS due_jobs ON payments(queue,next_at)')
            db.execute('PRAGMA user_version=2')
        self.verify_keys()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def verify_keys(self):
        path = self.folder / (self.mode + '-keycheck.json')
        with file_lock(str(path) + '.lock', blocking=True) as acquired:
            if not acquired:
                raise ValueError('Key check busy')
            if path.exists():
                value = json.loads(path.read_text())
                if self.vault.open(value['encrypted']) != 'receipt-v2' or value['index'] != self.vault.fingerprint('receipt-v2'):
                    raise ValueError('Wrong data/index key')
            else:
                with path.open('x') as target:
                    json.dump({'encrypted': self.vault.seal('receipt-v2'), 'index': self.vault.fingerprint('receipt-v2')}, target)

    def audit(self, db, pid, event):
        ref = self.vault.fingerprint(pid)[:16]
        db.execute('INSERT INTO audit(at,ref,event) VALUES (?,?,?)', (time.time(), ref, event))
        LOG.info('event=%s ref=%s', event, ref)

    def register_order(self, raw):
        # Trusted local/admin operation, never an unauthenticated HTTP route.
        order = validate(dict(raw, payment_id='order_validation', status='paid', paid_at='2026-01-01T00:00:00Z'))
        value = {key: order[key] for key in ('order_id', 'email', 'amount', 'currency')}
        with self.connect() as db:
            old = db.execute('SELECT fingerprint FROM orders WHERE id=?', (value['order_id'],)).fetchone()
            fingerprint = self.vault.fingerprint(value)
            if old and old[0] != fingerprint:
                raise Problem(409, 'order_conflict')
            db.execute('INSERT OR IGNORE INTO orders VALUES (?,?,?)', (value['order_id'], self.vault.seal(value), fingerprint))

    def gateway_notification(self, body, fetch=get_status):
        if not self.config.get('MIDTRANS_SERVER_KEY'):
            raise Problem(503, 'gateway_not_configured')
        if not isinstance(body, dict) or not isinstance(body.get('order_id'), str):
            raise Problem(422, 'invalid_notification')
        with self.connect() as db:
            order = db.execute('SELECT payload FROM orders WHERE id=?', (body['order_id'],)).fetchone()
        if not order:
            raise Problem(422, 'unknown_order')
        try:
            payment = verify_midtrans(body, self.config['MIDTRANS_SERVER_KEY'], self.vault.open(order[0]), fetch)
        except ValueError as error:
            allowed = {'invalid_notification', 'invalid_order_id', 'invalid_signature', 'gateway_identity_mismatch',
                       'gateway_not_paid', 'gateway_fraud_not_accepted', 'gateway_amount_mismatch', 'order_currency_mismatch', 'gateway_missing_timestamp'}
            code = str(error) if str(error) in allowed else 'gateway_invalid_response'
            raise Problem(422, code)
        except Exception:
            raise Problem(503, 'gateway_unavailable')
        return self.enqueue(payment)

    def enqueue(self, raw):
        payment = validate(raw)
        pid, now = payment['payment_id'], time.time()
        if self.mode == 'live' and payment['email'] != self.config['TEST_RECIPIENT']:
            raise Problem(403, 'recipient_not_allowlisted')
        fingerprint = self.vault.fingerprint(payment)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            stored = db.execute('SELECT fingerprint,stage,queue FROM payments WHERE id=?', (pid,)).fetchone()
            if stored:
                if stored[0] != fingerprint:
                    raise Problem(409, 'payment_id_conflict')
                if stored[1] == 'sent':
                    return {'status': 'duplicate', 'payment_id': pid, 'mode': self.mode}
                if stored[2] in ('dead', 'manual'):
                    raise Problem(409, 'requires_operator_review')
                return {'status': 'queued', 'payment_id': pid, 'mode': self.mode}
            db.execute('INSERT INTO payments(id,payload,fingerprint,stage,created,updated) VALUES (?,?,?,?,?,?)',
                       (pid, self.vault.seal(payment), fingerprint, 'received', now, now))
            self.audit(db, pid, 'queued')
        return {'status': 'queued', 'payment_id': pid, 'mode': self.mode}

    def stage(self, pid, stage, provider_id=None):
        with self.connect() as db:
            db.execute('UPDATE payments SET stage=?,provider_id=COALESCE(?,provider_id),updated=? WHERE id=?', (stage, provider_id, time.time(), pid))
            self.audit(db, pid, stage)

    def _deliver(self, pid):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = dict(db.execute('SELECT * FROM payments WHERE id=?', (pid,)).fetchone())
        payment = self.vault.open(row['payload'])
        if self.mode == 'live' and payment['email'] != self.config['TEST_RECIPIENT']:
            raise Problem(403, 'recipient_not_allowlisted')
        fields = {'Payment ID': pid, 'Order ID': payment['order_id'], 'Email': payment['email'], 'Amount': payment['amount'],
                  'Currency': 'IDR', 'Paid At': payment['paid_at'], 'Status': 'paid'}
        if row['stage'] == 'received':
            try:
                if self.mode == 'demo':
                    with self.connect() as db:
                        db.execute('INSERT OR REPLACE INTO airtable_demo VALUES (?,?)', (pid, self.vault.seal(fields)))
                else:
                    cfg = self.config
                    response = self.transport('PATCH', 'https://api.airtable.com/v0/' + cfg['AIRTABLE_BASE_ID'] + '/' + cfg['AIRTABLE_TABLE_ID'], cfg['AIRTABLE_TOKEN'],
                        {'performUpsert': {'fieldsToMergeOn': ['Payment ID']}, 'records': [{'fields': fields}]})
                    if not response.get('records'):
                        raise ValueError('Invalid Airtable response')
                    record_id = response['records'][0]['id']
                    if not isinstance(record_id, str) or not re.fullmatch(r'rec[A-Za-z0-9]+', record_id):
                        raise ValueError('Invalid Airtable record ID')
                    with self.connect() as db:
                        db.execute('UPDATE payments SET airtable_id=? WHERE id=?', (record_id, pid))
            except Exception:
                raise Problem(503, 'airtable_failed_retry_same_payload')
            self.stage(pid, 'stored')
        message = {'from': self.config.get('EMAIL_FROM', 'Demo <receipt@example.test>'), 'to': [payment['email']],
                   'subject': 'Receipt ' + pid, 'text': 'PORTFOLIO DEMO\nPayment: ' + pid + '\nOrder: ' + payment['order_id'] +
                   '\nPaid: IDR ' + format(payment['amount'], ',') + '\nDate: ' + payment['paid_at']}
        with self.connect() as db:
            db.execute('UPDATE payments SET stage=?,started=COALESCE(started,?),message=COALESCE(message,?),updated=? WHERE id=?',
                       ('sending', time.time(), self.vault.seal(message), time.time(), pid))
            started, saved = db.execute('SELECT started,message FROM payments WHERE id=?', (pid,)).fetchone()
        if self.mode == 'live' and time.time() - started >= 23 * 3600:
            raise Problem(409, 'email_delivery_unknown_manual_reconciliation')
        try:
            if self.mode == 'demo':
                with self.connect() as db:
                    db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?)', (pid, saved))
                provider_id = 'demo-' + pid
            else:
                # Stable across key rotation and process restart. One namespace per live base.
                identity = self.config['AIRTABLE_BASE_ID'] + ':' + pid
                key = 'receipt-' + hashlib.sha256(identity.encode()).hexdigest()
                provider_id = self.transport('POST', 'https://api.resend.com/emails', self.config['RESEND_API_KEY'],
                                             self.vault.open(saved), {'Idempotency-Key': key})['id']
                if not isinstance(provider_id, str) or not provider_id:
                    raise ValueError('Invalid email provider response')
        except Exception:
            raise Problem(503, 'email_unknown_retry_same_payload_within_23h')
        with self.connect() as db:
            db.execute("UPDATE payments SET stage='sent',queue='done',provider_id=?,last_error=NULL,updated=? WHERE id=?", (provider_id, time.time(), pid))
            self.audit(db, pid, 'sent')

    def _attempt(self, pid):
        with self.connect() as db:
            db.execute('UPDATE payments SET attempts=attempts+1,updated=? WHERE id=?', (time.time(), pid))
        try:
            self._deliver(pid)
        except Exception as error:
            error = error if isinstance(error, Problem) else Problem(503, 'unexpected_worker_error')
            with self.connect() as db:
                attempts = db.execute('SELECT attempts FROM payments WHERE id=?', (pid,)).fetchone()[0]
                queue = 'manual' if error.status in (403, 409) else ('dead' if attempts >= 8 else 'retry')
                delay = min(3600, 2 ** min(attempts, 12)) + random.uniform(0, 1)
                db.execute('UPDATE payments SET queue=?,last_error=?,next_at=?,updated=? WHERE id=?',
                           (queue, error.code, time.time() + delay, time.time(), pid))
                self.audit(db, pid, queue + ':' + error.code)
            raise error

    def process(self, raw):
        """Synchronous CLI/test helper. Public HTTP uses enqueue + worker instead."""
        result = self.enqueue(raw)
        if result['status'] == 'duplicate':
            return result
        with file_lock(self.dispatch_lock) as acquired:
            if not acquired:
                return result
            with self.connect() as db:
                row = db.execute('SELECT stage FROM payments WHERE id=?', (result['payment_id'],)).fetchone()
            if row[0] == 'sent':
                return dict(result, status='duplicate')
            self._attempt(result['payment_id'])
        return dict(result, status='processed')

    def run_once(self):
        # OS lock survives across Python processes and releases on crash. No expiring lease can overlap effects.
        with file_lock(self.dispatch_lock) as acquired:
            if not acquired:
                return False
            with self.connect() as db:
                row = db.execute("SELECT id FROM payments WHERE queue IN ('pending','retry') AND next_at<=? ORDER BY created LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return False
            try:
                self._attempt(row[0])
            except Problem:
                pass
            return True

    def metrics(self):
        with self.connect() as db:
            queues = dict(db.execute('SELECT queue,COUNT(*) FROM payments GROUP BY queue').fetchall())
            oldest = db.execute("SELECT MIN(created) FROM payments WHERE queue IN ('pending','retry')").fetchone()[0]
            system = dict(db.execute('SELECT id,value FROM system_state').fetchall())
        return {'queues': queues, 'oldest_pending_seconds': round(time.time() - oldest) if oldest else 0,
                'attention_required': bool(queues.get('dead', 0) or queues.get('manual', 0) or system.get('maintenance_error')),
                'system':system}

    def maintenance(self, retention_days=30, backup_days=7):
        if retention_days < 1 or backup_days < 1:
            raise ValueError('Retention must be at least one day')
        with file_lock(self.dispatch_lock) as acquired:
            if not acquired:
                return False
            backups = self.folder / 'backups'
            backups.mkdir(exist_ok=True)
            destination = backups / (self.mode + '-' + str(time.time_ns()) + '.fernet')
            self.vault.backup(self.db, destination)
            cutoff = time.time() - retention_days * 86400
            with self.connect() as db:
                ids = [r[0] for r in db.execute("SELECT id FROM payments WHERE queue='done' AND updated<? AND payload IS NOT NULL", (cutoff,))]
                purged_orders = set()
                for pid in ids:
                    payload, record_id = db.execute('SELECT payload,airtable_id FROM payments WHERE id=?', (pid,)).fetchone()
                    purged_orders.add(self.vault.open(payload)['order_id'])
                    if self.mode == 'live':
                        if not record_id:
                            raise ValueError('Cannot purge external email without record identity')
                        cfg = self.config
                        self.transport('PATCH', 'https://api.airtable.com/v0/' + cfg['AIRTABLE_BASE_ID'] + '/' + cfg['AIRTABLE_TABLE_ID'], cfg['AIRTABLE_TOKEN'],
                                       {'records': [{'id': record_id, 'fields': {'Email': None}}]})
                        time.sleep(0.25)  # Keep retention updates below the base's normal API rate.
                    db.execute('UPDATE payments SET payload=NULL,message=NULL WHERE id=?', (pid,))
                    db.execute('DELETE FROM airtable_demo WHERE id=?', (pid,))
                    db.execute('DELETE FROM outbox WHERE id=?', (pid,))
                    self.audit(db, pid, 'personal_data_purged')
                db.execute('DELETE FROM audit WHERE at<?', (cutoff,))
                # An order can hold the same email; remove only when all its paid jobs are old and done.
                remaining = set()
                for encrypted, in db.execute('SELECT payload FROM payments WHERE payload IS NOT NULL'):
                    remaining.add(self.vault.open(encrypted)['order_id'])
                for order_id in purged_orders - remaining:
                    db.execute('DELETE FROM orders WHERE id=?', (order_id,))
                db.execute("INSERT OR REPLACE INTO system_state VALUES ('last_backup_at',?)", (str(time.time()),))
                db.execute("DELETE FROM system_state WHERE id='maintenance_error'")
            for backup in backups.glob(self.mode + '-*.fernet'):
                if backup.stat().st_mtime < time.time() - backup_days * 86400:
                    backup.unlink()
        return True


def make_handler(processor, token):
    tokens = tuple(value for value in token.split(',') if value)
    rate_lock, hits = threading.Lock(), deque()
    limit = int(processor.config.get('RATE_LIMIT_PER_MINUTE', '60'))
    class Handler(BaseHTTPRequestHandler):
        server_version = 'ReceiptService'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass

        def reply(self, status, body):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            for key, value in (('Content-Type', 'application/json'), ('Content-Length', str(len(encoded))),
                               ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'), ('Connection', 'close')):
                self.send_header(key, value)
            if status == 429:
                self.send_header('Retry-After', '60')
            self.end_headers()
            self.wfile.write(encoded)
            self.wfile.flush()
            self.close_connection = True
            # A close with unread request bytes can reset TCP on Windows and discard this response.
            # Half-close the response, then drain a bounded amount for at most 50 ms.
            try:
                self.connection.shutdown(socket.SHUT_WR)
                deadline = time.monotonic() + 0.05
                remaining = 65536
                while remaining and time.monotonic() < deadline:
                    self.connection.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = self.connection.recv(min(4096, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass

        def authenticated(self):
            supplied = self.headers.get('X-Processor-Key', '').encode()
            valid = False
            for value in tokens:
                valid |= hmac.compare_digest(supplied, value.encode())
            return valid

        def do_GET(self):
            if self.path == '/health':
                return self.reply(200, {'service': 'receipt-processor'})
            if self.path == '/metrics' and self.authenticated():
                return self.reply(200, processor.metrics())
            self.reply(404, {'error': 'not_found'})

        def do_POST(self):
            try:
                with rate_lock:
                    now = time.monotonic()
                    while hits and hits[0] < now - 60:
                        hits.popleft()
                    if len(hits) >= limit:
                        raise Problem(429, 'rate_limited')
                    hits.append(now)
                if self.path not in ('/payments', '/gateway/midtrans'):
                    raise Problem(404, 'not_found')
                if not self.authenticated():
                    raise Problem(401, 'unauthorized')
                lengths = self.headers.get_all('Content-Length', [])
                if self.headers.get('Transfer-Encoding') or len(lengths) != 1:
                    raise Problem(400, 'invalid_framing')
                try:
                    length = int(lengths[0])
                except ValueError:
                    raise Problem(400, 'invalid_content_length')
                if not 0 < length <= 16384:
                    raise Problem(413, 'invalid_body_size')
                if self.headers.get_content_type() != 'application/json':
                    raise Problem(415, 'json_required')
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError()
                    def pairs(items):
                        result = {}
                        for key, value in items:
                            if key in result:
                                raise ValueError('duplicate_json_key')
                            result[key] = value
                        return result
                    body = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError, TimeoutError):
                    raise Problem(400, 'invalid_json')
                if self.path == '/payments' and processor.mode == 'live':
                    raise Problem(403, 'live_requires_verified_gateway')
                result = processor.gateway_notification(body) if self.path == '/gateway/midtrans' else processor.enqueue(body)
                self.reply(200 if result['status'] == 'duplicate' or self.path == '/gateway/midtrans' else 202, result)
            except Problem as error:
                self.reply(error.status, {'error': error.code})
            except Exception:
                self.reply(500, {'error': 'internal_error'})
    return Handler


class LimitedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def serve():
    tokens = os.environ.get('PROCESSOR_KEYS') or os.environ.get('PROCESSOR_KEY', '')
    if not tokens or any(len(key) < 32 for key in tokens.split(',')):
        raise SystemExit('Set PROCESSOR_KEYS to comma-separated random keys >=32 characters (current,previous).')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    processor = Processor(os.environ.get('DATA_DIR', 'data'), os.environ.get('MODE', 'demo'), dict(os.environ))
    stop = threading.Event()
    def worker():
        next_maintenance = 0
        while not stop.is_set():
            try:
                processor.run_once()
                if time.time() >= next_maintenance:
                    next_maintenance = time.time() + 300
                    processor.maintenance(int(os.environ.get('RETENTION_DAYS', '30')), int(os.environ.get('BACKUP_DAYS', '7')))
                    next_maintenance = time.time() + 86400
            except Exception:
                LOG.error('worker_or_maintenance_failed')
                with processor.connect() as db:
                    db.execute("INSERT OR REPLACE INTO system_state VALUES ('maintenance_error','worker_or_maintenance_failed')")
            stop.wait(0.5)
    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()
    server = LimitedServer((os.environ.get('BIND_HOST', '127.0.0.1'), int(os.environ.get('PORT', '8787'))), make_handler(processor, tokens))
    try:
        LOG.info('service_started mode=%s', processor.mode)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        worker_thread.join(timeout=35)


if __name__ == '__main__':
    serve()
