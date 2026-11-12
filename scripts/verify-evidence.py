"""Check that implementation files still match the recorded verification snapshot."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'evidence/source-manifest.json').read_text('utf-8'))
changed = []
ignored_parts = {'node_modules', 'dist', '__pycache__', '.pytest_cache', 'evidence', '.git'}
ignored_names = {'pytest-results.xml', 'verified-tests.xml'}
current = {
    item.relative_to(root).as_posix()
    for item in root.rglob('*')
    if item.is_file() and not any(part in ignored_parts for part in item.relative_to(root).parts)
    and item.name not in ignored_names and not item.name.endswith(('.pyc', '.tsbuildinfo', '.log'))
    and (not item.name.startswith('.env') or item.name == '.env.example')
}
changed.extend(sorted(current - set(manifest['sha256'])))
for relative, expected in manifest['sha256'].items():
    source = root / relative
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        changed.append(relative)
if changed:
    print('Verification snapshot is stale. Changed or missing files:')
    print('\n'.join(changed))
    raise SystemExit(1)
print(f"PASS: {len(manifest['sha256'])} recorded files match the verification snapshot.")
print('Runtime scope and remaining checks are recorded in evidence/verification.json.')
