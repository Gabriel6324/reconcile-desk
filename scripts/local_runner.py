"""Current-workspace convenience launcher; portable deployments use Compose."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parents[1]
RUNTIME = WORKSPACE / 'work/runtime'

def main():
    import psycopg
    config_file = RUNTIME / 'local-config.json'
    if not config_file.exists():
        raise SystemExit('Local runtime is not present. Use the Docker instructions in README.md.')
    config = json.loads(config_file.read_text('utf-8'))
    env = {**os.environ, 'DATABASE_URL': config['DATABASE_URL'],
           'ALLOWED_ORIGINS': 'http://127.0.0.1:8080,http://localhost:8080'}
    children = []
    logs = []
    database_process = None
    def start(args, cwd, label):
        log = open(RUNTIME / (label + '.log'), 'ab')
        logs.append(log)
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=log, stderr=log,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        children.append(proc)
        return proc
    def connect():
        return psycopg.connect(config['DATABASE_URL'].replace('postgresql+psycopg:', 'postgresql:'), connect_timeout=2)
    try:
        try:
            with connect(): pass
        except psycopg.OperationalError:
            database_process = start([str(RUNTIME / 'postgresql/pgsql/bin/postgres.exe'), '-D', str(RUNTIME / 'pgdata'),
                   '-h', '127.0.0.1', '-p', '54329', '-c', 'shared_buffers=16MB'], PROJECT, 'postgres-local')
            for _ in range(20):
                time.sleep(1)
                try:
                    with connect(): pass
                    break
                except psycopg.OperationalError: continue
            else: raise RuntimeError('PostgreSQL did not start. Check work/runtime/postgres-local.log.')
        start([sys.executable, '-m', 'uvicorn', 'api.main:app', '--host', '127.0.0.1', '--port', '8000', '--no-access-log'], PROJECT / 'backend', 'api-local')
        for _ in range(30):
            time.sleep(.5)
            try:
                with urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=2) as r:
                    if r.status == 200: break
            except Exception: continue
        else: raise RuntimeError('API did not start. Check work/runtime/api-local.log.')
        start(['node', str(PROJECT / 'frontend/node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', '8080'], PROJECT / 'frontend', 'web-local')
        print('Reconcile Desk: http://127.0.0.1:8080', flush=True)
        print('Keep this window open. Press Ctrl+C to stop. Saved data stays in the database.', flush=True)
        while True:
            time.sleep(1)
            if any(p.poll() is not None for p in children):
                raise RuntimeError('A service exited; inspect work/runtime/*-local.log.')
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                if child is database_process:
                    try:
                        subprocess.run([str(RUNTIME / 'postgresql/pgsql/bin/pg_ctl.exe'), '-D', str(RUNTIME / 'pgdata'),
                                        '-m', 'fast', '-w', '-t', '10', 'stop'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    except subprocess.TimeoutExpired:
                        print('Database shutdown exceeded its deadline. PostgreSQL will recover on next startup.')
                    if child.poll() is not None:
                        continue
                child.terminate()
                try: child.wait(timeout=8)
                except subprocess.TimeoutExpired: child.kill()
        for log in logs: log.close()

if __name__ == '__main__':
    main()
