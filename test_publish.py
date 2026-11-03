import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile
from publish import build, FILES


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        for name in FILES:
            value = '{"nodes":[],"active":false}' if name.startswith('workflow.') else ('{"email":"customer@example.test"}' if name.startswith('sample-') else 'safe synthetic file\n')
            (self.root/name).write_text(value)
        self.archive = Path(self.temp.name)/'public.zip'

    def test_private_files_and_databases_never_packaged(self):
        (self.root/'.env').write_text('private secret')
        (self.root/'receipts-live.sqlite3').write_bytes(b'private customer data')
        (self.root/'credentials.json').write_text('private credentials')
        (self.root/'data').mkdir()
        (self.root/'data'/'private.txt').write_text('private')
        result = build(self.root,self.archive)
        with ZipFile(self.archive) as archive:
            names = archive.namelist()
            self.assertEqual(len(names),len(FILES)+1)
            self.assertNotIn('payment-receipt/.env',names)
            self.assertNotIn('payment-receipt/credentials.json',names)
        self.assertEqual(len(result['sha256']),64)

    def test_known_api_token_rejected(self):
        (self.root/'README.md').write_text('re_' + 'A'*32)
        with self.assertRaises(ValueError):
            build(self.root,self.archive)
        self.assertFalse(self.archive.exists())

    def test_literal_environment_secret_rejected(self):
        (self.root/'.env.example').write_text('PROCESSOR_KEY=' + 'x'*32)
        with self.assertRaises(ValueError):
            build(self.root,self.archive)

    def test_real_recipient_in_sample_rejected(self):
        (self.root/'sample-payment.json').write_text(json.dumps({'email':'customer@real-company.invalid'}))
        with self.assertRaises(ValueError):
            build(self.root,self.archive)

    def test_exported_credential_or_active_workflow_rejected(self):
        for body in ({'nodes':[],'active':True}, {'nodes':[{'credentials':{'httpHeaderAuth':{'id':'private'}}}]}):
            (self.root/'workflow.n8n.json').write_text(json.dumps(body))
            with self.assertRaises(ValueError):
                build(self.root,self.archive)


if __name__ == '__main__':
    unittest.main()
