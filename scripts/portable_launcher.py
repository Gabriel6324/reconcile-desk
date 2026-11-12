"""Self-contained Windows launcher for the extracted Reconcile Desk package.

Layout: app/scripts/portable_launcher.py, runtime/python, runtime/postgresql/pgsql.
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
        configuration = {'password': secrets.token_hex(32), 'postgres_port': 54329}
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
        if not connection.execute('SELECT 1 FROM pg_database WHERE datname=%s', ('reconcile',)).fetchone():
            connection.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier('reconcile')))


def stop_database(configuration, log):
    if not owns_running_database(configuration):
        return
    result = subprocess.run([str(PGBIN / 'pg_ctl.exe'), '-D', str(pg_path(PGDATA)), '-m', 'fast', '-w', '-t', '20', 'stop'],
                            cwd=PGROOT, stdout=log, stderr=log, creationflags=HIDDEN, timeout=25)
    if result.returncode:
        say('数据库停止未得到成功回执。请查看 data/logs/postgres.log。')


def is_ready(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + '/api/ready', timeout=2) as response:
            return response.status == 200 and json.load(response).get('status') == 'ready'
    except (OSError, ValueError, urllib.error.URLError):
        return False


def open_existing(no_browser):
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        state = read_json(STATE)
        if valid_state(state):
            if state.get('status') == 'ready' and is_ready(state.get('url', '')):
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
    configuration = uvicorn.Config('api.main:app', host='127.0.0.1',
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
    lock = InstanceLock()
    if not lock.acquire():
        return open_existing(no_browser)
    configuration = None
    api_process = None
    postgres_process = None
    database_owned = False
    logs = []
    token = secrets.token_hex(24)
    state = {'root': str(ROOT), 'token': token, 'status': 'starting', 'url': None}
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
        for path in (PGBIN / 'initdb.exe', APP / 'backend/api/main.py', APP / 'frontend/dist/index.html'):
            if not path.is_file():
                raise RuntimeError('运行包文件缺失。请完整解压 ZIP 后再双击启动。')
        configuration = load_configuration()
        alias.acquire()
        pg_log = open(DATA / 'logs/postgres.log', 'ab', buffering=0)
        api_log = open(DATA / 'logs/api.log', 'ab', buffering=0)
        logs.extend([pg_log, api_log])
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
        environment.update({
            'DATABASE_URL': 'postgresql+psycopg://reconcile:' + configuration['password']
                            + '@127.0.0.1:' + str(configuration['postgres_port']) + '/reconcile',
            'WEB_DIST': str(APP / 'frontend/dist'),
            'ALLOWED_ORIGINS': url + ',http://localhost:' + str(port),
            'RECONCILE_PORTABLE_WEB_PORT': str(port),
            'PYTHONNOUSERSITE': '1', 'PYTHONUTF8': '1',
            'TEMP': str(DATA / 'tmp'), 'TMP': str(DATA / 'tmp'),
        })
        api_process = subprocess.Popen([sys.executable, '-I', '-X', 'utf8', str(Path(__file__).resolve()), 'serve'],
            cwd=APP / 'backend', env=environment, stdin=subprocess.PIPE,
            stdout=api_log, stderr=api_log, creationflags=HIDDEN)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            check_stop_request()
            if api_process.poll() is not None:
                raise RuntimeError('工作区服务启动失败。请查看 data/logs/api.log。')
            if is_ready(url):
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
            if api_process.poll() is not None:
                raise RuntimeError('工作区服务意外退出。请查看 data/logs/api.log。')
            if postgres_process is not None and postgres_process.poll() is not None:
                raise RuntimeError('数据库服务意外退出。请查看 data/logs/postgres.log。')
    except KeyboardInterrupt:
        pass
    except Exception as error:
        failure = str(error)
        say('启动或运行未完成：' + failure)
    finally:
        state['status'] = 'stopping'
        write_json(STATE, state)
        if api_process is not None and api_process.poll() is None:
            try:
                api_process.stdin.write(b'stop\n')
                api_process.stdin.flush()
                api_process.wait(timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                api_process.terminate()
                try:
                    api_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    api_process.kill()
                    api_process.wait(timeout=5)
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


def main():
    parser = argparse.ArgumentParser(description='Reconcile Desk portable launcher')
    parser.add_argument('command', choices=['start', 'stop', 'serve'], nargs='?', default='start')
    parser.add_argument('--no-browser', action='store_true')
    arguments = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('此运行包适用于 Windows 64 位系统。')
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
        say('无法完成操作：' + str(error))
        raise SystemExit(1)
