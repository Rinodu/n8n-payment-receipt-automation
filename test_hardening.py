import hashlib
import json
import os
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from cryptography.fernet import Fernet, InvalidToken

from gateway import verify_midtrans
from migrate_legacy import migrate
from processor import Processor, Problem, make_handler
from storage import file_lock
from test_processor import SAMPLE, CONFIG


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'ledger'
        self.p = Processor(self.folder)

    def notification(self):
        body = {'order_id': SAMPLE['order_id'], 'status_code': '200', 'gross_amount': '150000.00'}
        body['signature_key'] = hashlib.sha512((''.join(body.values()) + 'sandbox-test').encode()).hexdigest()
        return body

    def status(self):
        return {'order_id': SAMPLE['order_id'], 'currency': 'IDR', 'gross_amount': '150000.00',
                'transaction_status': 'settlement', 'fraud_status': 'accept', 'transaction_id': SAMPLE['payment_id'],
                'transaction_time': '2026-10-29 10:00:00', 'settlement_time': '2026-10-29 10:01:00'}

    def test_gateway_valid_signature_status_and_trusted_order(self):
        self.p.config['MIDTRANS_SERVER_KEY'] = 'sandbox-test'
        self.p.register_order(SAMPLE)
        body = dict(self.notification(), email='attacker@example.test', transaction_status='capture')
        result = self.p.gateway_notification(body, lambda *_: self.status())
        self.assertEqual(result['status'], 'queued')
        self.p.run_once()
        with self.p.connect() as db:
            stored = self.p.vault.open(db.execute('SELECT payload FROM payments').fetchone()[0])
        self.assertEqual(stored['email'], SAMPLE['email'])
        self.assertEqual(self.p.gateway_notification(body, lambda *_: self.status())['status'], 'duplicate')

    def test_gateway_forgery_never_calls_status(self):
        self.p.config['MIDTRANS_SERVER_KEY'] = 'sandbox-test'
        self.p.register_order(SAMPLE)
        with self.assertRaises(Problem) as caught:
            self.p.gateway_notification(dict(self.notification(), gross_amount='1.00'), lambda *_: self.fail('Forged input reached status API'))
        self.assertEqual(caught.exception.code, 'invalid_signature')

    def test_gateway_rejects_pending_amount_currency_identity_and_fraud(self):
        for change in ({'transaction_status':'pending'}, {'gross_amount':'1.00'}, {'currency':'USD'},
                       {'order_id':'another_order'}, {'fraud_status':'challenge'},
                       {'transaction_status':'capture', 'fraud_status':'deny'}, {'gross_amount':'NaN'},
                       {'gross_amount':'150000.50'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_midtrans(self.notification(), 'sandbox-test', SAMPLE, lambda *_: dict(self.status(), **change))

    def test_gateway_unknown_order_has_no_effect(self):
        self.p.config['MIDTRANS_SERVER_KEY'] = 'sandbox-test'
        with self.assertRaises(Problem):
            self.p.gateway_notification(self.notification(), lambda *_: self.status())
        self.assertEqual(self.p.metrics()['queues'], {})

    def test_encryption_no_plain_email_in_database_or_backups(self):
        self.p.process(SAMPLE)
        self.p.register_order(SAMPLE)
        self.assertNotIn(SAMPLE['email'].encode(), self.p.db.read_bytes())
        destination = Path(self.temp.name) / 'snapshot.fernet'
        self.p.vault.backup(self.p.db, destination)
        self.assertNotIn(SAMPLE['email'].encode(), destination.read_bytes())
        self.assertFalse(destination.read_bytes().startswith(b'SQLite'))

    def test_backup_restore_retains_dedupe_and_receipt(self):
        self.p.process(SAMPLE)
        backup = Path(self.temp.name) / 'snapshot.fernet'
        self.p.vault.backup(self.p.db, backup)
        key = json.loads((self.folder / '.demo-key.json').read_text())
        restored_dir = Path(self.temp.name) / 'restore'
        restored_dir.mkdir()
        target = restored_dir / 'receipts-demo.sqlite3'
        self.p.vault.restore(backup, target)
        restored = Processor(restored_dir, config={'DATA_KEYS':key['key'], 'INDEX_KEY':key['index']})
        self.assertEqual(restored.process(SAMPLE)['status'], 'duplicate')
        with restored.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.p.vault.restore(backup, target)

    def test_corrupt_backup_rejected_without_destination(self):
        backup = Path(self.temp.name) / 'broken.fernet'
        backup.write_bytes(b'corrupted')
        target = Path(self.temp.name) / 'not-created.sqlite3'
        with self.assertRaises(InvalidToken):
            self.p.vault.restore(backup, target)
        self.assertFalse(target.exists())

    def test_key_rotation_and_old_backup_recovery(self):
        self.p.process(SAMPLE)
        old = json.loads((self.folder / '.demo-key.json').read_text())
        new = Fernet.generate_key().decode()
        rotated = Processor(self.folder, config={'DATA_KEYS':new + ',' + old['key'], 'INDEX_KEY':old['index']})
        with rotated.connect() as db:
            rotated.vault.rotate(db)
            encrypted = db.execute('SELECT payload FROM payments').fetchone()[0]
        self.assertEqual(json.loads(Fernet(new.encode()).decrypt(encrypted.encode()))['email'], SAMPLE['email'])
        with self.assertRaises(InvalidToken):
            Fernet(old['key'].encode()).decrypt(encrypted.encode())
        self.assertEqual(rotated.process(SAMPLE)['status'], 'duplicate')

    def test_wrong_index_key_fails_before_processing(self):
        self.p.process(SAMPLE)
        old = json.loads((self.folder / '.demo-key.json').read_text())
        with self.assertRaises(ValueError):
            Processor(self.folder, config={'DATA_KEYS':old['key'], 'INDEX_KEY':'wrong-index-' + 'x'*32})

    def test_multiprocess_claims_produce_one_receipt(self):
        self.p.enqueue(SAMPLE)
        code = 'from processor import Processor; import sys; p=Processor(sys.argv[1]); [p.run_once() for _ in range(4)]'
        children = [subprocess.Popen([sys.executable, '-c', code, str(self.folder)], cwd=Path(__file__).parent,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        for child in children:
            output, error = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, error.decode())
        with self.p.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT attempts FROM payments').fetchone()[0], 1)

    def test_killed_lock_owner_releases_lock(self):
        code = 'from storage import file_lock; import sys,time;\nwith file_lock(sys.argv[1]) as ok:\n print(ok,flush=True); time.sleep(60)'
        child = subprocess.Popen([sys.executable, '-c', code, self.p.dispatch_lock], cwd=Path(__file__).parent,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(child.stdout.readline().strip(), b'True')
            with file_lock(self.p.dispatch_lock) as acquired:
                self.assertFalse(acquired)
        finally:
            child.kill()
            child.communicate(timeout=10)
        with file_lock(self.p.dispatch_lock) as acquired:
            self.assertTrue(acquired)

    def test_worker_resumes_after_restart(self):
        self.p.enqueue(SAMPLE)
        restarted = Processor(self.folder)
        self.assertTrue(restarted.run_once())
        self.assertEqual(restarted.metrics()['queues'], {'done':1})

    def test_process_crash_after_email_acceptance_retries_same_key_and_body(self):
        live_folder = Path(self.temp.name)/'crash-ledger'
        journal = Path(self.temp.name)/'provider-fixture.json'
        config_file = Path(self.temp.name)/'config.json'
        config_file.write_text(json.dumps(CONFIG))
        code = '''import json,os,sys
from pathlib import Path
from processor import Processor
config=json.loads(Path(sys.argv[2]).read_text())
payment=json.loads(Path('sample-payment.json').read_text())
def transport(method,url,token,body,extra=None):
    if method=='PATCH': return {'records':[{'id':'recCrash'}]}
    Path(sys.argv[3]).write_text(json.dumps({'message':body,'headers':extra}))
    os._exit(17)
p=Processor(sys.argv[1],'live',config,transport)
p.process(payment)
'''
        child = subprocess.run([sys.executable,'-c',code,str(live_folder),str(config_file),str(journal)],
                               cwd=Path(__file__).parent,capture_output=True,timeout=30)
        self.assertEqual(child.returncode,17,child.stderr.decode())
        original = json.loads(journal.read_text())
        calls=[]
        def retry(method,url,token,body,extra=None):
            self.assertEqual(method,'POST')
            self.assertEqual(body,original['message'])
            self.assertEqual(extra,original['headers'])
            calls.append(method)
            return {'id':'provider-accepted-before-crash'}
        p=Processor(live_folder,'live',CONFIG.copy(),retry)
        self.assertTrue(p.run_once())
        self.assertEqual(calls,['POST'])
        self.assertEqual(p.metrics()['queues'],{'done':1})

    def test_live_retention_clears_airtable_email_by_saved_record_id(self):
        calls=[]
        def fake(method,url,token,body,extra=None):
            calls.append(body)
            return {'records':[{'id':'recRetention'}]} if method=='PATCH' else {'id':'email'}
        p=Processor(self.folder,'live',CONFIG.copy(),fake)
        p.process(SAMPLE)
        with p.connect() as db:
            db.execute('UPDATE payments SET updated=?',(time.time()-40*86400,))
        with patch('processor.time.sleep'):
            self.assertTrue(p.maintenance())
        self.assertEqual(calls[-1],{'records':[{'id':'recRetention','fields':{'Email':None}}]})

    def test_retry_backoff_dead_letter_and_attention(self):
        def failing(*_args):
            raise TimeoutError()
        p = Processor(self.folder, 'live', CONFIG.copy(), failing)
        p.enqueue(SAMPLE)
        for attempt in range(8):
            p.run_once()
            with p.connect() as db:
                attempts, next_at, queue = db.execute('SELECT attempts,next_at,queue FROM payments').fetchone()
                self.assertEqual(attempts, attempt+1)
                self.assertGreater(next_at, time.time())
                db.execute('UPDATE payments SET next_at=0')
        self.assertEqual(queue, 'dead')
        self.assertTrue(p.metrics()['attention_required'])
        self.assertFalse(p.run_once())

    def test_audit_does_not_contain_personal_data(self):
        with self.assertLogs('receipts', level='INFO') as captured:
            self.p.process(SAMPLE)
        self.assertNotIn(SAMPLE['email'], '\n'.join(captured.output))
        self.assertNotIn(SAMPLE['payment_id'], '\n'.join(captured.output))

    def test_retention_preserves_dedupe_and_clears_local_personal_data(self):
        self.p.register_order(SAMPLE)
        self.p.process(SAMPLE)
        with self.p.connect() as db:
            db.execute('UPDATE payments SET updated=?', (time.time()-40*86400,))
        self.assertTrue(self.p.maintenance())
        with self.p.connect() as db:
            self.assertEqual(db.execute('SELECT payload,message FROM payments').fetchone(), (None,None))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)
        self.assertEqual(self.p.process(SAMPLE)['status'], 'duplicate')

    def test_migration_preserves_sent_and_holds_uncertain(self):
        source = Path(self.temp.name) / 'legacy.sqlite3'
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE payments(id TEXT,payload TEXT,stage TEXT,started REAL,provider_id TEXT,message TEXT)')
            db.execute('INSERT INTO payments VALUES (?,?,?,?,?,?)', (SAMPLE['payment_id'], json.dumps(SAMPLE), 'sent', time.time(), 'sent-old', None))
            db.execute('INSERT INTO payments VALUES (?,?,?,?,?,?)', ('uncertain', json.dumps(dict(SAMPLE,payment_id='uncertain')), 'sending', time.time(), None, None))
        db.close()
        original = source.read_bytes()
        migrated = migrate(source, Path(self.temp.name)/'migrated')
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(migrated.process(SAMPLE)['status'], 'duplicate')
        self.assertTrue(migrated.metrics()['attention_required'])
        self.assertNotIn(SAMPLE['email'].encode(), migrated.db.read_bytes())

    def http(self, p=None, token=None):
        token = token or 'a'*32
        p = p or self.p
        server = ThreadingHTTPServer(('127.0.0.1',0), make_handler(p, token))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_port)
        def request(path, payload=None, key='a'*32):
            req = Request(url+path, data=payload, headers={'X-Processor-Key':key,'Content-Type':'application/json'})
            try:
                response = urlopen(req, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response), dict(response.headers)
        return request

    def test_rate_limit_and_security_headers(self):
        self.p.config['RATE_LIMIT_PER_MINUTE'] = '2'
        request = self.http()
        request('/payments', b'{}')
        request('/payments', b'{}')
        status, body, headers = request('/payments', b'{}')
        self.assertEqual(status, 429)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn('Retry-After', headers)

    def test_rejects_ambiguous_duplicate_json_keys(self):
        request = self.http()
        self.assertEqual(request('/payments', b'{"amount":1,"amount":2}')[0], 400)

    def test_early_rejection_responses_survive_unread_bodies(self):
        request = self.http()
        for _ in range(12):
            self.assertEqual(request('/payments', b'x'*17000)[0], 413)
            self.assertEqual(request('/payments', b'x'*20000, key='incorrect')[0], 401)
        self.assertEqual(self.p.metrics()['queues'], {})

    def test_old_and_new_auth_keys_work_during_rotation(self):
        request = self.http(token='a'*32 + ',' + 'b'*32)
        self.assertEqual(request('/metrics', key='a'*32)[0], 200)
        self.assertEqual(request('/metrics', key='b'*32)[0], 200)
        self.assertEqual(request('/metrics', key='c'*32)[0], 404)

    def test_live_http_forbids_unverified_payment_route(self):
        p = Processor(self.folder, 'live', CONFIG.copy(), lambda *_: self.fail('No external API expected'))
        request = self.http(p)
        status, body, _ = request('/payments', json.dumps(SAMPLE).encode())
        self.assertEqual(status,403)
        self.assertEqual(body['error'],'live_requires_verified_gateway')

    def test_real_service_worker_and_automatic_backup(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0))
            port=probe.getsockname()[1]
        token='service-test-'+'x'*32
        folder=Path(self.temp.name)/'service'
        env=dict(os.environ,MODE='demo',DATA_DIR=str(folder),PROCESSOR_KEYS=token,PORT=str(port),BIND_HOST='127.0.0.1')
        # Isolate from any real account environment on the host.
        for key in ('DATA_KEYS','INDEX_KEY','AIRTABLE_TOKEN','RESEND_API_KEY','MIDTRANS_SERVER_KEY'):
            env.pop(key,None)
        service=subprocess.Popen([sys.executable,'processor.py'],cwd=Path(__file__).parent,env=env,
                                 stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        try:
            base='http://127.0.0.1:'+str(port)
            ready=False
            for _ in range(100):
                if service.poll() is not None: break
                try:
                    with urlopen(base+'/health',timeout=1) as response:
                        ready=response.status==200
                except OSError: pass
                if ready: break
                time.sleep(.05)
            self.assertTrue(ready)
            req=Request(base+'/payments',data=json.dumps(SAMPLE).encode(),
                        headers={'X-Processor-Key':token,'Content-Type':'application/json'})
            with urlopen(req,timeout=5) as response:
                self.assertEqual(response.status,202)
            metrics={}
            for _ in range(100):
                with urlopen(Request(base+'/metrics',headers={'X-Processor-Key':token}),timeout=5) as response:
                    metrics=json.load(response)
                if metrics['queues'].get('done') and metrics['system'].get('last_backup_at'): break
                time.sleep(.05)
            self.assertEqual(metrics['queues'],{'done':1})
            self.assertIn('last_backup_at',metrics['system'])
            self.assertTrue(list((folder/'backups').glob('*.fernet')))
        finally:
            service.terminate()
            logs,_=service.communicate(timeout=20)
        self.assertNotIn(SAMPLE['email'].encode(),logs)
        self.assertNotIn(token.encode(),logs)


if __name__ == '__main__':
    unittest.main(verbosity=2)
