"""One command: real local HTTP demo, no external network or accounts."""
import json
import secrets
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from processor import Processor, make_handler


def main():
    sample = json.loads(Path(__file__).with_name('sample-payment.json').read_text())
    with tempfile.TemporaryDirectory() as folder:
        processor = Processor(folder)
        key = secrets.token_urlsafe(32)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(processor, key))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            results = []
            for _ in range(2):
                request = Request('http://127.0.0.1:' + str(server.server_port) + '/payments',
                                  data=json.dumps(sample).encode(),
                                  headers={'X-Processor-Key': key, 'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    results.append(json.load(response))
                processor.run_once()
            with processor.connect() as db:
                count = db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
                message = processor.vault.open(db.execute('SELECT message FROM outbox').fetchone()[0])
            assert [r['status'] for r in results] == ['queued', 'duplicate']
            assert count == 1
            report = {'demo': 'local HTTP; no n8n engine or external APIs', 'requests': results,
                      'receipt_count': count, 'receipt_preview': message, 'external_emails_sent': 0}
            Path(__file__).with_name('demo-result.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report, indent=2))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
