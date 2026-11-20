"""A responsive foreign workbench must never satisfy this launcher's readiness."""
import importlib.util,json,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer

def test_portable_readiness_binds_healthy_response_to_launch_identity():
    script=Path(__file__).resolve().parents[3]/'scripts/team_portable_launcher.py'
    spec=importlib.util.spec_from_file_location('portable_readiness_test',script)
    launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
    state={'identity':'other-installation','worker':'lease_present'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data={'instance_id':state['identity']} if self.path=='/api/local-instance' else {'worker':state['worker']}
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(data).encode())
        def log_message(self,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        assert not launcher.is_ready(url,None)
        assert not launcher.is_ready(url,'expected-installation')
        state['identity']='expected-installation'
        assert launcher.is_ready(url,'expected-installation')
        state['worker']='offline'
        assert not launcher.is_ready(url,'expected-installation')
    finally:server.shutdown();server.server_close();thread.join(timeout=5)
