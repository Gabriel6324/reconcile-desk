"""Fixed trusted CPU entrypoint: no user code, database handle, or model calls."""
import os

def compute(sender, kind, payload):
    for key in ('DATABASE_URL', 'LLM_API_KEY'):
        os.environ.pop(key, None)
    try:
        if os.name != 'nt':
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
        from core import parse_file, reconcile, build_export, CoreError
        if kind == 'import':
            result = parse_file(payload['data'], payload['filename'])
        elif kind == 'reconcile':
            result = reconcile(payload['left'], payload['right'], payload['definition'])
        elif kind == 'export':
            result = build_export(payload['left'], payload['right'], payload['definition'], payload['result'])
        else:
            raise ValueError('Unknown trusted computation')
        sender.send({'ok': True, 'result': result})
    except BaseException as error:
        sender.send({'ok': False, 'code': getattr(error, 'code', 'COMPUTATION_FAILED'),
                     'message': getattr(error, 'message', '计算失败，输入和历史记录已保留。')})
    finally:
        sender.close()
