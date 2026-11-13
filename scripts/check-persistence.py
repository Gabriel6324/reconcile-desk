"""Capture/verify actual HTTP history and export bytes across container recreation."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['capture', 'verify'])
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--evidence', default='artifacts/docker')
    args = parser.parse_args()
    target = Path(args.evidence)
    target.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def read(path):
        with opener.open(args.base_url + path, timeout=15) as response:
            assert response.status == 200
            return response.read()
    deadline = time.monotonic() + 60
    while True:
        try:
            assert json.loads(read('/api/ready'))['status'] == 'ready'
            break
        except (OSError, ValueError, AssertionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    runs = json.loads(read('/api/runs'))['items']
    assert runs, 'No browser-created history found'
    run = json.loads(read('/api/runs/' + runs[0]['id']))
    assert run['state'] == 'completed'
    groups = json.loads(read('/api/runs/' + run['id'] + '/results'))
    assert len(groups['items']) == 9
    snapshot = {'run': run, 'groups': groups,
                'export_sha256': hashlib.sha256(read('/api/runs/' + run['id'] + '/export')).hexdigest(),
                'source_sha256': {side: hashlib.sha256(read('/api/imports/' + run[side + '_import_id'] + '/source')).hexdigest()
                                  for side in ('left', 'right')}}
    path = target / 'persistence-before.json'
    if args.mode == 'capture':
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        assert snapshot == json.loads(path.read_text('utf-8')), 'History, sources, results or export changed after recreation'
        (target / 'persistence-check.json').write_text(json.dumps({
            'status': 'PASS', 'run_id': run['id'], 'export_sha256': snapshot['export_sha256'],
            'checks': ['Persisted run and result groups unchanged', 'Original files unchanged',
                       'Downloaded XLSX bytes unchanged', 'API healthy after database/API/web recreation']}, indent=2), encoding='utf-8')
    print(args.mode + ': PASS')


if __name__ == '__main__':
    main()
