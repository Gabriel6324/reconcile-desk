"""Bounded upload reception, including concurrent and disconnected senders."""
import asyncio
from api.main import Boundary

def test_concurrent_upload_rejected_and_disconnect_releases_capacity():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        async def downstream(scope, receive, send):
            body = await receive()
            calls.append(body['body'])
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'{}'})
        app = Boundary(downstream)
        scope = {'type':'http','method':'POST','path':'/api/imports',
                 'headers':[(b'x-reconcile-client',b'web')]}
        messages = []
        async def send(message): messages.append(message)
        async def stalled():
            entered.set()
            await release.wait()
            return {'type':'http.disconnect'}
        async def body(): return {'type':'http.request','body':b'x','more_body':False}
        first = asyncio.create_task(app(scope,stalled,send))
        await entered.wait()
        await app(scope,body,send)
        assert messages[0]['status']==429
        assert not calls
        release.set()
        await first
        assert not app.upload_lock.locked()
        messages.clear()
        await app(scope,body,send)
        assert messages[0]['status']==200 and calls==[b'x']
    asyncio.run(scenario())

def test_chunked_oversize_rejected_before_application():
    async def scenario():
        calls = []
        messages = []
        async def downstream(*args): calls.append(True)
        async def send(message): messages.append(message)
        async def receive(): return {'type':'http.request','body':b'x'*(1024*1024),'more_body':True}
        app = Boundary(downstream)
        await app({'type':'http','method':'POST','path':'/api/imports',
                   'headers':[(b'x-reconcile-client',b'web')]},receive,send)
        assert messages[0]['status']==413 and not calls
        assert not app.upload_lock.locked()
    asyncio.run(scenario())
