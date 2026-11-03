"""Build a public ZIP from an explicit allowlist and reject common secret leaks."""
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

FILES = (
    '.env.example', '.gitignore', '.dockerignore', 'README.md', 'SECURITY.md', 'OPERATIONS.md', 'TESTING.md',
    'requirements.txt', 'processor.py', 'gateway.py', 'storage.py', 'admin.py', 'migrate_legacy.py',
    'run_demo.py', 'start-demo.ps1', 'demo.ps1', 'configure.ps1', 'start-private.ps1',
    'test_processor.py', 'test_hardening.py', 'test_publish.py', 'publish.py',
    'sample-payment.json', 'sample-order.json', 'workflow.n8n.json', 'workflow.midtrans.n8n.json',
    'demo-result.json', 'n8n-test-result.json', 'deployment-test-result.json', 'Dockerfile', 'compose.yaml', 'Caddyfile',
)
PATTERNS = (
    re.compile(r'\b(?:pat[A-Za-z0-9]{14,}\.[A-Za-z0-9]{20,}|re_[A-Za-z0-9]{24,}|SB-Mid-server-[A-Za-z0-9_-]{10,})\b'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'(?im)^[ \t]*(?:PROCESSOR_KEYS?|DATA_KEYS|INDEX_KEY|RESEND_API_KEY|AIRTABLE_TOKEN|MIDTRANS_SERVER_KEY)[ \t]*=[ \t]*[^\s#]+'),
)


def build(root, destination):
    root, destination = Path(root), Path(destination)
    approved = []
    for name in FILES:
        source = root / name
        if source.is_symlink() or not source.is_file():
            raise ValueError('Missing or unsafe release file: ' + name)
        content = source.read_text(encoding='utf-8')
        patterns = PATTERNS if name == '.env.example' else PATTERNS[:2]
        if any(pattern.search(content) for pattern in patterns):
            raise ValueError('Possible secret in approved file: ' + name)
        if name.startswith('workflow.'):
            workflow = json.loads(content)
            if workflow.get('active') or any(node.get('credentials') for node in workflow['nodes']):
                raise ValueError('Workflow must be inactive and credential-free')
        if name.startswith('sample-') and not json.loads(content)['email'].endswith('@example.test'):
            raise ValueError('Public sample must use synthetic email')
        approved.append(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, 'w', ZIP_DEFLATED) as archive:
        for source in approved:
            archive.write(source, 'payment-receipt/' + source.name)
        archive.writestr('payment-receipt/MANIFEST.json', json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in approved}, indent=2))
    return {'files':len(approved)+1, 'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(), 'secret_scan':'passed (heuristic, not a guarantee)'}


if __name__ == '__main__':
    root = Path(__file__).parent
    print(json.dumps(build(root, root.parent/'payment-receipt-portfolio.zip'), indent=2))
