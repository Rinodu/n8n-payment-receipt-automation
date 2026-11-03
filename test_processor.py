import concurrent.futures
import copy
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from processor import Processor, Problem, make_handler
from cryptography.fernet import Fernet

SAMPLE = json.loads(Path(__file__).with_name('sample-payment.json').read_text())
CONFIG = dict(AIRTABLE_TOKEN='test', AIRTABLE_BASE_ID='appTest', AIRTABLE_TABLE_ID='tblTest',
              DATA_KEYS=Fernet.generate_key().decode(), INDEX_KEY='test-index-' + 'x' * 32, MIDTRANS_SERVER_KEY='test-sandbox', RESEND_API_KEY='test', EMAIL_FROM='receipt@example.test', TEST_RECIPIENT=SAMPLE['email'])


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.processor = Processor(self.temp.name)

    def counts(self):
        with self.processor.connect() as db:
            return tuple(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         for table in ('payments', 'airtable_demo', 'outbox'))

    def test_happy_path_and_receipt(self):
        self.assertEqual(self.processor.process(SAMPLE)['status'], 'processed')
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.processor.connect() as db:
            message = self.processor.vault.open(db.execute('SELECT message FROM outbox').fetchone()[0])
        self.assertIn('150,000', message['text'])
        self.assertEqual(message['to'], [SAMPLE['email']])

    def test_duplicate_after_restart(self):
        self.processor.process(SAMPLE)
        restarted = Processor(self.temp.name)
        self.assertEqual(restarted.process(SAMPLE)['status'], 'duplicate')
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_concurrent_duplicate(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.processor.process(SAMPLE), range(16)))
        self.assertEqual(sum(r['status'] == 'processed' for r in results), 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_conflicting_duplicate(self):
        self.processor.process(SAMPLE)
        with self.assertRaises(Problem) as caught:
            self.processor.process(dict(SAMPLE, amount=99))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_invalid_payloads_have_no_effects(self):
        bad = [None, [], {}, dict(SAMPLE, amount=True), dict(SAMPLE, amount=-1),
               dict(SAMPLE, amount=1.5), dict(SAMPLE, currency='USD'), dict(SAMPLE, status='pending'),
               dict(SAMPLE, email='a\r\nBcc:x@example.test'), dict(SAMPLE, payment_id="x' OR 1"),
               dict(SAMPLE, paid_at='2026-10-29'), dict(SAMPLE, paid_at='broken')]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(Problem):
                self.processor.process(payload)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_distinct_payments(self):
        self.processor.process(SAMPLE)
        self.processor.process(dict(SAMPLE, payment_id='pay_002'))
        self.assertEqual(self.counts(), (2, 2, 2))

    def live(self, transport):
        return Processor(self.temp.name, 'live', CONFIG.copy(), transport)

    def test_live_api_contract_and_allowlist(self):
        calls = []
        def fake(method, url, token, body, extra=None):
            calls.append((method, url, copy.deepcopy(body), extra))
            return {'records': [{'id': 'recTest'}]} if method == 'PATCH' else {'id': 'emailTest'}
        processor = self.live(fake)
        processor.process(SAMPLE)
        processor.process(SAMPLE)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2]['performUpsert']['fieldsToMergeOn'], ['Payment ID'])
        self.assertEqual(calls[0][2]['records'][0]['fields']['Amount'], 150000)
        self.assertTrue(calls[1][3]['Idempotency-Key'].startswith('receipt-'))
        with self.assertRaises(Problem) as caught:
            processor.process(dict(SAMPLE, payment_id='blocked', email='other@example.test'))
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(len(calls), 2)

    def test_airtable_failure_never_sends_email(self):
        calls = []
        def fail(method, *args):
            calls.append(method)
            raise TimeoutError()
        processor = self.live(fail)
        with self.assertRaises(Problem):
            processor.process(SAMPLE)
        self.assertEqual(calls, ['PATCH'])
        with processor.connect() as db:
            self.assertEqual(db.execute('SELECT stage FROM payments').fetchone()[0], 'received')

    def test_airtable_timeout_can_retry_with_upsert(self):
        calls = []
        def fake(method, *args):
            calls.append(method)
            if len(calls) == 1:
                raise TimeoutError()
            return {'records': [{'id': 'recTest'}]} if method == 'PATCH' else {'id': 'email'}
        processor = self.live(fake)
        with self.assertRaises(Problem):
            processor.process(SAMPLE)
        processor.process(SAMPLE)
        self.assertEqual(calls, ['PATCH', 'PATCH', 'POST'])

    def test_email_timeout_reuses_key_and_frozen_message(self):
        calls = []
        def fake(method, url, token, body, extra=None):
            if method == 'PATCH':
                return {'records': [{'id': 'recTest'}]}
            calls.append((copy.deepcopy(body), extra))
            if len(calls) == 1:
                raise TimeoutError('Provider accepted but connection dropped')
            return {'id': 'email'}
        processor = self.live(fake)
        with self.assertRaises(Problem):
            processor.process(SAMPLE)
        processor.config['EMAIL_FROM'] = 'changed@example.test'
        processor.process(SAMPLE)
        self.assertEqual(calls[0], calls[1])

    def test_expired_email_retry_requires_manual_review(self):
        def fake(method, *args):
            if method == 'PATCH':
                return {'records': [{'id': 'recTest'}]}
            raise TimeoutError()
        processor = self.live(fake)
        with self.assertRaises(Problem):
            processor.process(SAMPLE)
        with processor.connect() as db:
            db.execute('UPDATE payments SET started=?', (time.time() - 24 * 3600,))
        with self.assertRaises(Problem) as caught:
            processor.process(SAMPLE)
        self.assertEqual(caught.exception.code, 'email_delivery_unknown_manual_reconciliation')

    def test_bad_configuration_fails_closed(self):
        with self.assertRaises(ValueError):
            Processor(self.temp.name, 'liv')
        with self.assertRaises(ValueError):
            Processor(self.temp.name, 'live')

    def test_workflow_structure(self):
        workflow = json.loads(Path(__file__).with_name('workflow.n8n.json').read_text())
        self.assertFalse(workflow['active'])
        nodes = {n['name']: n for n in workflow['nodes']}
        self.assertEqual(nodes['Payment Webhook']['parameters']['authentication'], 'headerAuth')
        self.assertEqual(nodes['Process Receipt']['parameters']['jsonBody'], '={{ $json.body }}')
        self.assertTrue(nodes['Process Receipt']['parameters']['options']['response']['response']['neverError'])
        for name, links in workflow['connections'].items():
            self.assertIn(name, nodes)
            for group in links['main']:
                for link in group:
                    self.assertIn(link['node'], nodes)

    def test_http_auth_validation_duplicate_and_malformed_json(self):
        token = 'test-only-' + 'x' * 32
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.processor, token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_port) + '/payments'
        def send(body, key=token):
            request = Request(url, data=body, headers={'X-Processor-Key': key, 'Content-Type': 'application/json'})
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                return error.code, json.load(error)
        payload = json.dumps(SAMPLE).encode()
        self.assertEqual(send(payload, 'wrong')[0], 401)
        self.assertEqual(send(b'broken')[0], 400)
        self.assertEqual(send(b'{}')[0], 422)
        self.assertEqual(send(b'x' * 16385)[0], 413)
        self.assertEqual(send(payload), (202, {'status': 'queued', 'payment_id': SAMPLE['payment_id'], 'mode': 'demo'}))
        self.processor.run_once()
        self.assertEqual(send(payload)[1]['status'], 'duplicate')


if __name__ == '__main__':
    unittest.main(verbosity=2)
