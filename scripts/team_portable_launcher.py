"""Self-contained Windows launcher for the extracted Reconcile Desk package.

Layout: app/scripts/team_portable_launcher.py, runtime/python, runtime/postgresql/pgsql.
Only the package's own data directory is managed. No service installation is used.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'app'
DATA = ROOT / 'data'
PGDATA = DATA / 'pgdata'
PGBIN = ROOT / 'runtime' / 'postgresql' / 'pgsql' / 'bin'
STATE = DATA / 'launcher-state.json'
STOP = DATA / 'stop-request.json'
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
START_TIMEOUT = 120
PGROOT = ROOT


class PostgresPathAlias:
    """Give PostgreSQL an ASCII executable path on Windows with legacy ACP paths."""

    def __init__(self):
        self.drive = None

    def acquire(self):
        global PGROOT, PGBIN
        if str(ROOT).isascii():
            return
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        define = kernel.DefineDosDeviceW
        define.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p]
        define.restype = ctypes.c_bool
        occupied = kernel.GetLogicalDrives()
        for letter in reversed('DEFGHIJKLMNOPQRSTUVWXYZ'):
            if occupied & (1 << (ord(letter) - ord('A'))):
                continue
            drive = letter + ':'
            if define(8, drive, str(ROOT)):
                self.drive = drive
                PGROOT = Path(drive + '\\')
                PGBIN = PGROOT / 'runtime/postgresql/pgsql/bin'
                return
        raise RuntimeError('中文目录启动需要一个可用的临时盘符。请解压到纯英文路径后重试。')

    def release(self):
        if self.drive:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            define = kernel.DefineDosDeviceW
            define.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p]
            define.restype = ctypes.c_bool
            # Exact-match removal can only remove the mapping this process created.
            define(8 | 4 | 2, self.drive, str(ROOT))


def pg_path(path):
    return PGROOT / path.relative_to(ROOT)


def say(message):
    print(message, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def write_json(path, value):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(4) + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class InstanceLock:
    """An OS file lock releases automatically if the launcher exits unexpectedly."""

    def __init__(self):
        self.handle = None

    def acquire(self):
        import msvcrt
        DATA.mkdir(parents=True, exist_ok=True)
        self.handle = open(DATA / 'launcher.lock', 'a+b')
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b'0')
            self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self.handle.close()
            self.handle = None
            return False

    def release(self):
        if self.handle is not None:
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None


def same_path(first, second):
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def valid_state(state):
    return isinstance(state, dict) and state.get('root') == str(ROOT) and isinstance(state.get('token'), str)


def choose_port(preferred):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(('127.0.0.1', preferred))
        except OSError:
            listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def load_configuration():
    path = DATA / 'config.json'
    configuration = read_json(path)
    if configuration is None:
        if (PGDATA / 'PG_VERSION').exists():
            raise RuntimeError('数据库配置缺失。请恢复 data/config.json 后再启动，原始数据已保留。')
        configuration = {'password': secrets.token_hex(32), 'postgres_port': 54342}
        write_json(path, configuration)
    if not isinstance(configuration.get('password'), str) or not configuration['password']:
        raise RuntimeError('data/config.json 的数据库配置无效。')
    port = configuration.get('postgres_port')
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise RuntimeError('data/config.json 的数据库端口无效。')
    return configuration


def connect_database(configuration, database='postgres'):
    import psycopg
    return psycopg.connect(host='127.0.0.1', port=configuration['postgres_port'],
                           user='reconcile', password=configuration['password'],
                           dbname=database, autocommit=True, connect_timeout=2)


def owns_running_database(configuration):
    """Never adopt a server based on its port or PID alone."""
    import psycopg
    try:
        with connect_database(configuration) as connection:
            directory = connection.execute('SHOW data_directory').fetchone()[0]
            return same_path(directory, PGDATA)
    except psycopg.Error:
        return False


def initialize_database(configuration, log):
    if (PGDATA / 'PG_VERSION').exists():
        return
    if PGDATA.exists() and any(PGDATA.iterdir()):
        raise RuntimeError('数据库目录含未完成的初始化文件。请查看 data/logs/postgres.log；目录内容已保留。')
    password_file = DATA / '.init-password'
    password_file.write_text(configuration['password'], encoding='utf-8')
    environment = os.environ.copy()
    environment['TEMP'] = str(DATA / 'tmp')
    environment['TMP'] = environment['TEMP']
    try:
        result = subprocess.run([str(PGBIN / 'initdb.exe'), '-D', str(pg_path(PGDATA)), '-U', 'reconcile',
                                 '-A', 'scram-sha-256', '--encoding=UTF8', '--locale=C',
                                 '--pwfile', str(pg_path(password_file))],
                                cwd=PGROOT, env=environment, stdout=log, stderr=log,
                                creationflags=HIDDEN, timeout=START_TIMEOUT)
        if result.returncode:
            raise RuntimeError('数据库首次初始化失败。请查看 data/logs/postgres.log。')
    finally:
        password_file.unlink(missing_ok=True)


def create_project_database(configuration):
    import psycopg
    with connect_database(configuration) as connection:
        if not connection.execute('SELECT 1 FROM pg_database WHERE datname=%s', ('reconcile_team',)).fetchone():
            connection.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier('reconcile_team')))


def confirm_recovery(configuration,check_stop=lambda:None):
    marker=read_json(DATA/'recovery.json')
    if marker and marker.get('incomplete'):
        raise RuntimeError('此副本的备份恢复尚未通过校验，请重新恢复到另一新目录。')
    if not marker or not marker.get('requires_confirmation'):
        return
    say('此目录是备份恢复的新副本，旧会话已失效，任务队列暂停。')
    say('请确认原工作区已停止，避免同时处理同一批业务。')
    say('输入 RESUME 后按回车才会恢复任务；其他输入会保留暂停并停止本次启动。')
    # Console input may block indefinitely; keep the owning launcher responsive
    # to Stop.cmd and console-close while the restored worker remains held.
    import msvcrt
    say('确认恢复任务：')
    answer=[];console=sys.stdin.isatty()
    peek=ctypes.windll.kernel32.PeekNamedPipe
    peek.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong,ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong),ctypes.c_void_p]
    peek.restype=ctypes.c_bool
    handle=msvcrt.get_osfhandle(sys.stdin.fileno())
    while True:
        check_stop()
        if console:
            if not msvcrt.kbhit():time.sleep(.1);continue
            char=msvcrt.getwch()
            if char in ('\x00','\xe0'):msvcrt.getwch();continue
            if char=='\x03':raise KeyboardInterrupt
            if char in ('\r','\n'):say('');break
            if char=='\b':
                if answer:answer.pop();sys.stdout.write('\b \b');sys.stdout.flush()
                continue
            if len(answer)<128:answer.append(char);sys.stdout.write(char);sys.stdout.flush()
        else:
            available=ctypes.c_ulong()
            if not peek(handle,None,0,None,ctypes.byref(available),None):break
            if not available.value:time.sleep(.1);continue
            char=os.read(sys.stdin.fileno(),1).decode('ascii',errors='replace')
            if not char or char=='\n':break
            if len(answer)<128:answer.append(char)
    check_stop()
    if ''.join(answer).strip()!='RESUME':
        raise RuntimeError('恢复副本仍暂停，原目录和备份保持不变。')
    with connect_database(configuration,'reconcile_team') as connection:
        with connection.transaction():
            connection.execute('UPDATE team_job_capacity SET recovery_hold=false WHERE id=1')
            connection.execute("INSERT INTO team_audit_events(id,org_id,workspace_id,actor_id,action,resource_type,resource_id,request_id,safe_detail,created_at) VALUES (%s,%s,NULL,NULL,'recovery.console_resume','deployment','local',%s,%s,clock_timestamp())",
                (secrets.token_hex(16),'00000000-0000-0000-0000-000000000001',secrets.token_hex(16),'{"via":"portable_recovery_console"}'))
    marker['requires_confirmation']=False
    write_json(DATA/'recovery.json',marker)


def stop_database(configuration, log):
    if not owns_running_database(configuration):
        return
    result = subprocess.run([str(PGBIN / 'pg_ctl.exe'), '-D', str(pg_path(PGDATA)), '-m', 'fast', '-w', '-t', '20', 'stop'],
                            cwd=PGROOT, stdout=log, stderr=log, creationflags=HIDDEN, timeout=25)
    if result.returncode:
        say('数据库停止未得到成功回执。请查看 data/logs/postgres.log。')


def is_ready(url,instance_id):
    if not instance_id:return False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + '/api/local-instance',timeout=2) as response:
            if response.status!=200 or json.load(response).get('instance_id')!=instance_id:return False
        with opener.open(url + '/api/ready', timeout=2) as response:
            return response.status == 200 and json.load(response).get('worker') == 'lease_present'
    except (OSError, ValueError, urllib.error.URLError):
        return False


def open_existing(no_browser):
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        state = read_json(STATE)
        if valid_state(state):
            if state.get('status') == 'ready' and is_ready(state.get('url', ''),state.get('instance_id')):
                say('工作区已在运行：' + state['url'])
                if not no_browser:
                    webbrowser.open(state['url'])
                return 0
            if state.get('status') in ('failed', 'stopped'):
                raise RuntimeError('已有启动操作正在退出。请稍后重新启动。')
        time.sleep(.25)
    raise RuntimeError('另一启动窗口仍占用工作区。请查看该窗口或运行“停止.cmd”。')


def serve():
    """The dedicated stdin pipe requests graceful shutdown and detects parent exit."""
    import uvicorn
    sys.path.insert(0, str(APP / 'backend'))
    configuration = uvicorn.Config('team.local:app', host='127.0.0.1',
                                    port=int(os.environ['RECONCILE_PORTABLE_WEB_PORT']),
                                    access_log=False, log_level='info')
    server = uvicorn.Server(configuration)
    def watch_parent():
        # A blocking stdin read can prevent Windows from initializing the stdin
        # handle inherited by multiprocessing workers. Poll the control pipe.
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        peek = kernel.PeekNamedPipe
        peek.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        peek.restype = ctypes.c_bool
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        while not server.should_exit:
            available = ctypes.c_ulong()
            if not peek(handle, None, 0, None, ctypes.byref(available), None) or available.value:
                server.should_exit = True
                return
            time.sleep(.2)
    threading.Thread(target=watch_parent, daemon=True).start()
    server.run()


def start(no_browser):
    if (ROOT/'restore-pending.json').exists():
        raise RuntimeError('恢复副本尚未完成，请等待恢复成功后再启动；失败的副本不能作为空工作区使用。')
    lock = InstanceLock()
    if not lock.acquire():
        return open_existing(no_browser)
    configuration = None
    api_process = None
    postgres_process = None
    worker_process = None
    database_owned = False
    logs = []
    token = secrets.token_hex(24)
    state = {'root': str(ROOT), 'token': token, 'instance_id':secrets.token_hex(24), 'status': 'starting', 'url': None}
    failure = None
    console_callback = None
    stop_event = threading.Event()
    alias = PostgresPathAlias()
    shutdown_complete = threading.Event()
    def check_stop_request():
        request = read_json(STOP)
        if stop_event.is_set() or (valid_state(request) and request['token'] == token):
            raise KeyboardInterrupt
    try:
        (DATA / 'logs').mkdir(exist_ok=True)
        (DATA / 'tmp').mkdir(exist_ok=True)
        write_json(STATE, state)
        if os.name == 'nt':
            # Give the launcher an opportunity to clean up on console close as well as Ctrl+C.
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
            def on_console_event(event):
                if event in (0, 1, 2, 5, 6):
                    stop_event.set()
                    if event in (2, 5, 6):
                        shutdown_complete.wait(4)
                    return True
                return False
            console_callback = callback_type(on_console_event)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(console_callback, True)
        for path in (PGBIN / 'initdb.exe', APP / 'backend/team/local.py', APP / 'frontend/dist/index.html'):
            if not path.is_file():
                raise RuntimeError('运行包文件缺失。请完整解压 ZIP 后再双击启动。')
        configuration = load_configuration()
        alias.acquire()
        pg_log = open(DATA / 'logs/postgres.log', 'ab', buffering=0)
        api_log = open(DATA / 'logs/api.log', 'ab', buffering=0)
        worker_log = open(DATA / 'logs/worker.log', 'ab', buffering=0)
        logs.extend([pg_log, api_log, worker_log])
        say('正在启动对齐工作区，首次运行需要初始化数据库……')
        initialize_database(configuration, pg_log)
        check_stop_request()
        if owns_running_database(configuration):
            database_owned = True
        else:
            configuration['postgres_port'] = choose_port(configuration['postgres_port'])
            write_json(DATA / 'config.json', configuration)
            postgres_process = subprocess.Popen([str(PGBIN / 'postgres.exe'), '-D', str(pg_path(PGDATA)),
                '-h', '127.0.0.1', '-p', str(configuration['postgres_port']), '-c', 'shared_buffers=32MB'],
                cwd=PGROOT, stdout=pg_log, stderr=pg_log, creationflags=HIDDEN)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                check_stop_request()
                if postgres_process.poll() is not None:
                    raise RuntimeError('数据库启动失败。请查看 data/logs/postgres.log。')
                if owns_running_database(configuration):
                    database_owned = True
                    break
                time.sleep(.25)
            else:
                raise RuntimeError('数据库启动超时。请查看 data/logs/postgres.log。')
        create_project_database(configuration)
        check_stop_request()
        port = choose_port(8080)
        url = 'http://127.0.0.1:' + str(port)
        environment = os.environ.copy()
        # team.app constructs its HTTPS default app at import time; the local
        # factory supplies its own exact loopback origins after that import.
        environment.pop('TEAM_ALLOWED_ORIGINS',None)
        environment.update({
            'DATABASE_URL': 'postgresql+psycopg://reconcile:' + configuration['password']
                            + '@127.0.0.1:' + str(configuration['postgres_port']) + '/reconcile_team',
            'WEB_DIST': str(APP / 'frontend/dist'),
            'RECONCILE_PORTABLE_WEB_PORT': str(port),
            'RECONCILE_PORTABLE_INSTANCE_ID':state['instance_id'],
            'PYTHONNOUSERSITE': '1', 'PYTHONUTF8': '1',
            'TEMP': str(DATA / 'tmp'), 'TMP': str(DATA / 'tmp'),
        })
        migration = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(Path(__file__).resolve()), 'migrate'],
            cwd=APP / 'backend', env=environment, stdout=api_log, stderr=api_log, creationflags=HIDDEN, timeout=120)
        if migration.returncode:
            raise RuntimeError('数据库迁移未完成；已保留数据，请查看 data/logs/api.log。')
        confirm_recovery(configuration,check_stop_request)
        worker_process = subprocess.Popen([sys.executable, '-I', '-X', 'utf8', str(Path(__file__).resolve()), 'worker'],
            cwd=APP / 'backend', env=environment, stdin=subprocess.PIPE,
            stdout=worker_log, stderr=worker_log, creationflags=HIDDEN)
        api_process = subprocess.Popen([sys.executable, '-I', '-X', 'utf8', str(Path(__file__).resolve()), 'serve'],
            cwd=APP / 'backend', env=environment, stdin=subprocess.PIPE,
            stdout=api_log, stderr=api_log, creationflags=HIDDEN)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            check_stop_request()
            if worker_process.poll() is not None:
                raise RuntimeError('后台执行器已退出，请查看 data/logs/worker.log。')
            if api_process.poll() is not None:
                raise RuntimeError('工作区服务启动失败。请查看 data/logs/api.log。')
            if is_ready(url,state['instance_id']):
                break
            time.sleep(.25)
        else:
            raise RuntimeError('工作区服务启动超时。请查看 data/logs/api.log。')
        state.update(status='ready', url=url)
        write_json(STATE, state)
        say('对齐工作区已就绪：' + url)
        say('请保留此窗口。双击“停止.cmd”或按 Ctrl+C 可正常停止；资料保存在 data 文件夹。')
        if not no_browser:
            webbrowser.open(url)
        while not stop_event.wait(.3):
            request = read_json(STOP)
            if valid_state(request) and request['token'] == token:
                break
            if worker_process.poll() is not None:
                raise RuntimeError('后台执行器已退出，请查看 data/logs/worker.log。')
            if api_process.poll() is not None:
                raise RuntimeError('工作区服务意外退出。请查看 data/logs/api.log。')
            if postgres_process is not None and postgres_process.poll() is not None:
                raise RuntimeError('数据库服务意外退出。请查看 data/logs/postgres.log。')
    except KeyboardInterrupt:
        pass
    except Exception as error:
        failure = str(error) if isinstance(error, RuntimeError) else '服务操作失败，请保留数据后重试。'
        say('启动或运行未完成：' + failure)
    finally:
        state['status'] = 'stopping'
        write_json(STATE, state)
        for process in (api_process, worker_process):
            stop_child(process)
        if configuration is not None and database_owned:
            try:
                stop_database(configuration, logs[0])
            except (OSError, subprocess.TimeoutExpired) as error:
                say('数据库停止需要核查，请查看 data/logs/postgres.log。')
        if postgres_process is not None and postgres_process.poll() is None:
            # This handle was created by this launcher; never use a PID from a state file.
            postgres_process.terminate()
            try:
                postgres_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                postgres_process.kill()
        for log in logs:
            log.close()
        alias.release()
        state['status'] = 'failed' if failure else 'stopped'
        if failure:
            state['error'] = failure
        write_json(STATE, state)
        if console_callback is not None:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(console_callback, False)
        lock.release()
        shutdown_complete.set()
    if failure:
        return 1
    say('工作区已停止，数据已保留。')
    return 0


def stop():
    lock = InstanceLock()
    if lock.acquire():
        lock.release()
        say('此运行包当前没有正在工作的启动窗口。')
        return 0
    state = read_json(STATE)
    if not valid_state(state):
        raise RuntimeError('启动窗口正在初始化，请稍候再次运行停止。')
    write_json(STOP, {'root': str(ROOT), 'token': state['token']})
    say('正在正常停止工作区……')
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if lock.acquire():
            lock.release()
            say('工作区已停止，data 文件夹中的资料已保留。')
            return 0
        time.sleep(.3)
    raise RuntimeError('停止仍未完成。请检查启动窗口和 data/logs 中的日志。')


def stop_child(process):
    """Only use a live handle created by this launcher, never a saved PID."""
    if process is None or process.poll() is not None:
        return
    try:
        process.stdin.write(b'stop\n');process.stdin.flush()
        process.wait(timeout=20)
    except (OSError,subprocess.TimeoutExpired):
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)


def worker_serve():
    sys.path.insert(0,str(APP / 'backend'))
    from api.db import make_engine
    from team.worker import ImportWorker
    import msvcrt
    engine=make_engine();worker=ImportWorker(engine)
    def watch_parent():
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        peek=kernel.PeekNamedPipe
        peek.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong,ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong),ctypes.c_void_p]
        peek.restype=ctypes.c_bool
        handle=msvcrt.get_osfhandle(sys.stdin.fileno())
        while not worker.stop.wait(.2):
            available=ctypes.c_ulong()
            if not peek(handle,None,0,None,ctypes.byref(available),None) or available.value:
                worker.stop.set();return
    threading.Thread(target=watch_parent,daemon=True).start()
    try:
        worker.start()
        while not worker.stop.is_set():
            worker.tick();worker.stop.wait(.3)
    finally:
        worker.close();engine.dispose()


def backup():
    """Consistent custom-format DB snapshot; contains all source/result tables."""
    import hashlib
    from datetime import datetime,timezone
    configuration=read_json(DATA/'config.json')
    if configuration is None:
        raise RuntimeError('请先启动并初始化工作区，再进行备份。')
    alias=PostgresPathAlias()
    target=None
    try:
        alias.acquire()
        if not owns_running_database(configuration):
            raise RuntimeError('请先双击“启动.cmd”，待工作区就绪后再备份。')
        folder=ROOT/'backups';folder.mkdir(exist_ok=True)
        name='reconcile-team-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3)+'.dump'
        target=folder/name
        environment=os.environ.copy()
        environment.update(PGHOST='127.0.0.1',PGPORT=str(configuration['postgres_port']),
                           PGUSER='reconcile',PGPASSWORD=configuration['password'])
        with open(target,'xb') as output:
            result=subprocess.run([str(PGBIN/'pg_dump.exe'),'-Fc','--no-owner','--no-acl','-d','reconcile_team'],
                                  env=environment,cwd=PGROOT,stdout=output,stderr=subprocess.PIPE,creationflags=HIDDEN,timeout=300)
        if result.returncode:
            raise RuntimeError('备份未完成，原数据库未改变。')
        checksum=hashlib.sha256(target.read_bytes()).hexdigest()
        write_json(target.with_suffix('.json'),{'file':name,'sha256':checksum,'bytes':target.stat().st_size,
            'format':'postgresql-custom','database':'reconcile_team','created_at':datetime.now(timezone.utc).isoformat(),
            'scope':'All database tables including original files, rules, periods, cases and audit. Contains private business data.'})
        say('备份已完成：'+str(target))
        say('备份包含业务资料与账号数据，请妥善保管。恢复前应先在独立数据库验证。')
        return 0
    except Exception:
        if target is not None:target.unlink(missing_ok=True)
        raise
    finally:
        alias.release()


def restore(backup_path):
    """Restore only to a freshly created sibling; source installation is untouched."""
    import hashlib
    import shutil
    from datetime import datetime,timezone
    source=Path(backup_path).expanduser().resolve()
    manifest=read_json(source.with_suffix('.json'))
    if not source.is_file() or source.suffix.lower()!='.dump' or not manifest:
        raise RuntimeError('请选择本程序生成的 .dump 备份，并保留同名 .json 校验清单。')
    if manifest.get('format')!='postgresql-custom' or manifest.get('database')!='reconcile_team':
        raise RuntimeError('备份格式或数据库类型不兼容。')
    with open(source,'rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=manifest.get('sha256'):
            raise RuntimeError('备份校验失败，未执行恢复。')
    # Keep the sibling name short: Windows DLL loading and copying may still
    # enforce MAX_PATH even when the source installation itself starts normally.
    # A recovered copy must not accumulate the previous installation's suffix.
    name='RD-recovered-'+datetime.now(timezone.utc).strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(3)
    destination=ROOT.parent/name
    if destination.exists() or destination.resolve().parent!=ROOT.parent.resolve():
        raise RuntimeError('恢复目标必须是全新的相邻目录。')
    say('正在创建独立恢复副本，当前工作区数据保持不变……')
    destination.mkdir()
    # Fence the new copy before exposing any executable/launcher. Keep this
    # marker on every failure, including interruption during runtime copying.
    write_json(destination/'restore-pending.json',{'incomplete':True})
    for folder in ['app','runtime']:
        shutil.copytree(ROOT/folder,destination/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.log'))
    for item in ROOT.iterdir():
        if item.is_file() and item.suffix.lower() in ('.cmd','.txt'):
            shutil.copy2(item,destination/item.name)
    if (ROOT/'samples').is_dir():shutil.copytree(ROOT/'samples',destination/'samples')
    # Store the trusted, hash-checked archive inside the new copy before restoring.
    incoming=destination/'restore-input';incoming.mkdir()
    shutil.copy2(source,incoming/'snapshot.dump')
    shutil.copy2(source.with_suffix('.json'),incoming/'snapshot.json')
    executable=destination/'runtime/python/python.exe'
    script=destination/'app/scripts/team_portable_launcher.py'
    result=subprocess.run([str(executable),'-I','-X','utf8',str(script),'restore-load'],
                          cwd=destination,creationflags=HIDDEN,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=600)
    if result.returncode:
        raise RuntimeError('恢复副本未完成，请保留该目录并查看其 data/logs/restore.log；原安装和备份未修改。')
    marker=read_json(destination/'data/recovery.json')
    if not marker or marker.get('incomplete') or not marker.get('requires_confirmation'):
        raise RuntimeError('恢复副本校验未完成，未解除启动保护。')
    (destination/'restore-pending.json').unlink()
    say('恢复成功，新副本：'+str(destination))
    say('先停止原工作区，再双击新目录的“启动.cmd”，确认恢复任务后重新登录。')
    return 0


def restore_load():
    """Private child command: requires clean new data directory and fixed input."""
    import hashlib
    source=ROOT/'restore-input/snapshot.dump'
    manifest=read_json(ROOT/'restore-input/snapshot.json')
    if DATA.exists() or not source.is_file() or not manifest:
        raise RuntimeError('恢复只允许写入尚未初始化的新副本。')
    with open(source,'rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=manifest.get('sha256'):
            raise RuntimeError('恢复文件校验不一致。')
    (DATA/'logs').mkdir(parents=True)
    (DATA/'tmp').mkdir()
    write_json(DATA/'recovery.json',{'incomplete':True,'requires_confirmation':True})
    configuration=load_configuration()
    configuration['postgres_port']=choose_port(configuration['postgres_port'])
    write_json(DATA/'config.json',configuration)
    alias=PostgresPathAlias();process=None;owned=False
    log=open(DATA/'logs/restore.log','ab',buffering=0)
    try:
        alias.acquire();initialize_database(configuration,log)
        process=subprocess.Popen([str(PGBIN/'postgres.exe'),'-D',str(pg_path(PGDATA)),'-h','127.0.0.1','-p',str(configuration['postgres_port']),'-c','shared_buffers=32MB'],
                                 cwd=PGROOT,stdout=log,stderr=log,creationflags=HIDDEN)
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            if process.poll() is not None:raise RuntimeError('恢复数据库启动失败。')
            if owns_running_database(configuration):owned=True;break
            time.sleep(.25)
        if not owned:raise RuntimeError('恢复数据库启动超时。')
        create_project_database(configuration)
        environment=os.environ.copy()
        environment.update(PGHOST='127.0.0.1',PGPORT=str(configuration['postgres_port']),PGUSER='reconcile',PGPASSWORD=configuration['password'])
        with open(source,'rb') as incoming:
            result=subprocess.run([str(PGBIN/'pg_restore.exe'),'--exit-on-error','--no-owner','--no-acl','-d','reconcile_team'],
                cwd=PGROOT,env=environment,stdin=incoming,stdout=log,stderr=log,creationflags=HIDDEN,timeout=300)
        if result.returncode:raise RuntimeError('备份恢复失败，原目录未修改。')
        os.environ['DATABASE_URL']='postgresql+psycopg://reconcile:'+configuration['password']+'@127.0.0.1:'+str(configuration['postgres_port'])+'/reconcile_team'
        sys.path.insert(0,str(APP/'backend'))
        from api.db import make_engine
        from team.cli import migrate
        from team import jobs,dispatch
        from team.job_models import GlobalCapacity,Job
        from team.models import Session,AuditEvent
        from sqlalchemy import select,delete
        from sqlalchemy.orm import sessionmaker
        engine=make_engine()
        try:
            migrate(engine)
            SessionMaker=sessionmaker(engine)
            with SessionMaker.begin() as db:
                capacity=db.get(GlobalCapacity,1,with_for_update=True)
                capacity.recovery_hold=True;capacity.generation+=1
                db.execute(delete(Session))
                db.add(AuditEvent(id=secrets.token_hex(16),org_id='00000000-0000-0000-0000-000000000001',workspace_id=None,actor_id=None,
                    action='recovery.isolated_restore',resource_type='deployment',resource_id='local',request_id=secrets.token_hex(16),safe_detail={'source_sha256':manifest['sha256'],'dispatch_held':True}))
            with SessionMaker() as db:active=list(db.scalars(select(Job.id).where(Job.state.in_(['running','cancel_requested']))))
            for ident in active:
                with SessionMaker.begin() as db:jobs.recover_expired(db,ident,authorize_inputs=dispatch.authorize_inputs)
        finally:engine.dispose()
        with connect_database(configuration,'reconcile_team') as connection:
            hashes=connection.execute('SELECT id, sha256 FROM team_source_assets').fetchall()
            for ident,expected in hashes:
                content=connection.execute('SELECT content FROM team_source_assets WHERE id=%s',(ident,)).fetchone()[0]
                if hashlib.sha256(content).hexdigest()!=expected:raise RuntimeError('恢复后的来源文件校验失败。')
            sources=len(hashes)
            reports=connection.execute('SELECT id, sha256 FROM team_period_reports').fetchall()
            for ident,expected in reports:
                content=connection.execute('SELECT content FROM team_period_reports WHERE id=%s',(ident,)).fetchone()[0]
                if hashlib.sha256(content).hexdigest()!=expected:raise RuntimeError('恢复后的报告校验失败。')
        write_json(DATA/'recovery.json',{'requires_confirmation':True,'backup_sha256':manifest['sha256'],'source_files_verified':sources,'reports_verified':len(reports)})
        say('恢复校验通过；旧会话失效，任务保持暂停。')
        return 0
    finally:
        if owned:stop_database(configuration,log)
        if process is not None and process.poll() is None:
            process.terminate();process.wait(timeout=10)
        log.close();alias.release()


def main():
    parser = argparse.ArgumentParser(description='Reconcile Desk portable launcher')
    parser.add_argument('command', choices=['start', 'stop', 'serve', 'worker', 'migrate', 'backup','restore','restore-load'], nargs='?', default='start')
    parser.add_argument('backup_path',nargs='?')
    parser.add_argument('--no-browser', action='store_true')
    arguments = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('此运行包适用于 Windows 64 位系统。')
    if arguments.command == 'migrate':
        sys.path.insert(0,str(APP / 'backend'))
        from api.db import make_engine
        from team.cli import migrate
        engine=make_engine()
        try:migrate(engine)
        finally:engine.dispose()
        return 0
    if arguments.command == 'worker':
        worker_serve()
        return 0
    if arguments.command == 'backup':
        return backup()
    if arguments.command == 'restore':
        path=arguments.backup_path or input('备份 .dump 文件完整路径：').strip().strip('"')
        return restore(path)
    if arguments.command == 'restore-load':
        return restore_load()
    if arguments.command == 'serve':
        serve()
        return 0
    if arguments.command == 'stop':
        return stop()
    return start(arguments.no_browser)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        say('无法完成操作：' + (str(error) if isinstance(error,RuntimeError) else '操作失败，原数据已保留。'))
        raise SystemExit(1)
