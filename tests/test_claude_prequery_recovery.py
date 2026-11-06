import asyncio
import json

import pytest

from supervisor.runtime.claude import _Command
from tests.test_runtime_claude import FakeClient, FakeFactory, TOOLS, backend, result_message, wait_completed
from tests.test_runtime_claude_context import ContextClient, ContextFactory, context_thread
from tests.test_runtime_claude_async import BatchClock, call_batch, eventually, flush_tasks, mixed_batch, output


def timed_out():
    try:
        raise TimeoutError('secret-timeout-body')
    except TimeoutError as cause:
        raise Exception('secret-control-error') from cause


@pytest.mark.parametrize('failures,status,queries', [(1,'completed',3),(2,'failed',0)])
async def test_initialize_timeout_bounded_and_same_session(tmp_path, failures, status, queries):
    class Client(ContextClient):
        async def connect(self):
            await super().connect()
            self.factory.connects += 1
            if self.factory.connects <= failures:
                timed_out()
    class Factory(ContextFactory):
        connects = 0
        def __call__(self, options):
            client = Client(self, options); self.clients.append(client); return client
    factory=Factory(); events=[]; instance=backend(tmp_path,factory,events)
    try:
        await context_thread(instance,tmp_path)
        original=instance._record('thread-1')['claudeSessionId']
        await instance.request('turn/start',{'threadId':'thread-1','turnId':'t','input':[{'type':'text','text':'work'}]})
        done=await wait_completed(events)
        assert done['params']['turn']['status']==status
        assert factory.main_queries==queries
        record=instance._record('thread-1')
        assert record['contextPreparationRetries']==1
        assert record['lastContextPreparationFailure']['phase']=='connection_validation'
        assert not record.get('contextUsageProbeRetries')
        assert factory.clients[0].queries==[] and factory.clients[0].disconnect_task is not None
        assert factory.clients[0].options.session_id==factory.clients[1].options.session_id==original
        if failures==2:
            assert len(factory.clients)==2 and record['contextState']['generation']==0
        saved=(instance.state_dir/'threads.json').read_text()
        # The complete error chain is kept (owner decision: no redaction).
        assert 'secret-timeout-body' in saved and 'secret-control-error' in saved
    finally:
        await instance.stop()


async def test_connect_and_measure_share_one_budget(tmp_path):
    class Client(ContextClient):
        async def connect(self):
            await super().connect()
            if len(self.factory.clients)==1: timed_out()
        async def get_context_usage(self): timed_out()
    class Factory(ContextFactory):
        def __call__(self,options):
            client=Client(self,options); self.clients.append(client); return client
    factory=Factory(); events=[]; instance=backend(tmp_path,factory,events)
    try:
        await context_thread(instance,tmp_path)
        await instance.request('turn/start',{'threadId':'thread-1','turnId':'t','input':[{'type':'text','text':'work'}]})
        assert (await wait_completed(events))['params']['turn']['status']=='failed'
        assert len(factory.clients)==2 and factory.main_queries==0
        assert instance._record('thread-1')['lastContextPreparationFailure']['phase']=='context_usage'
    finally: await instance.stop()


@pytest.mark.parametrize('mode',['auth','query','cleanup'])
async def test_no_retry_for_auth_query_or_unclean_close(tmp_path,mode):
    class Client(ContextClient):
        async def connect(self):
            await super().connect()
            if mode=='auth': raise ValueError('private-auth-failure')
            if mode=='cleanup': timed_out()
        async def disconnect(self):
            await super().disconnect()
            if mode=='cleanup': raise RuntimeError('private-cleanup-failure')
        async def query(self,prompt):
            if mode=='query':
                self.factory.main_queries += 1
                timed_out()
            await super().query(prompt)
    class Factory(ContextFactory):
        def __call__(self,options):
            client=Client(self,options); self.clients.append(client); return client
    factory=Factory(); events=[]; instance=backend(tmp_path,factory,events)
    try:
        await context_thread(instance,tmp_path)
        await instance.request('turn/start',{'threadId':'thread-1','turnId':'t','input':[{'type':'text','text':'work'}]})
        assert (await wait_completed(events))['params']['turn']['status']=='failed'
        assert len(factory.clients)==1 and factory.main_queries==(1 if mode=='query' else 0)
        assert not instance._record('thread-1').get('contextPreparationRetries')
    finally: await instance.stop()


async def test_cancel_between_preparations_prevents_query_and_reconnect(tmp_path):
    class Client(ContextClient):
        async def connect(self):
            await super().connect(); timed_out()
        async def disconnect(self):
            await super().disconnect()
            if not self.factory.cancelled:
                self.factory.cancelled=True
                future=asyncio.get_running_loop().create_future()
                self.factory.instance._owners['thread-1'].queue.put_nowait(_Command('interrupt',{'turnId':'t'},future))
    class Factory(ContextFactory):
        cancelled=False
        def __call__(self,options):
            client=Client(self,options); self.clients.append(client); return client
    factory=Factory();events=[];instance=backend(tmp_path,factory,events);factory.instance=instance
    try:
        await context_thread(instance,tmp_path)
        await instance.request('turn/start',{'threadId':'thread-1','turnId':'t','input':[{'type':'text','text':'work'}]})
        assert (await wait_completed(events))['params']['turn']['status']=='interrupted'
        assert len(factory.clients)==1 and factory.main_queries==0
        # No orphaned queue reader may swallow the owner's next command; stop()
        # would otherwise wait out its 10-second owner timeout.
        await asyncio.wait_for(instance.stop(),timeout=5)
    finally: await instance.stop()


async def test_async_result_survives_initialize_retry_without_tool_or_query_replay(tmp_path):
    class Client(FakeClient):
        async def connect(self):
            await super().connect()
            self.factory.connects+=1
            if self.factory.connects==2: timed_out()
        async def get_context_usage(self): return {'totalTokens':100,'maxTokens':1000000}
        async def query(self,prompt):
            await super().query(prompt)
            self.factory.queries+=1
            if self.factory.queries==2:
                await self.messages.put(result_message(session_id=self.options.resume or self.options.session_id))
    class Factory(FakeFactory):
        connects=0; queries=0
        def __call__(self,options):
            client=Client(self,options); self.clients.append(client); return client
    factory=Factory(complete_after_queries=None); events=[]; gate=asyncio.Event(); calls=[]
    async def host(request):
        path=request['arguments']['path']; calls.append(path)
        if path=='slow': await gate.wait()
        return output('evidence-'+path)
    instance=backend(tmp_path,factory,events,tool_handler=host)
    try:
        await instance.request('initialize',{})
        await instance.request('thread/start',{'threadId':'thread-1','model':'sonnet','cwd':str(tmp_path),'effort':'high','tools':TOOLS,'asyncTools':True,'noNeedForContext':{'enabled':True,'thresholdTokens':244800,'retainTokens':22000}})
        await instance.request('turn/start',{'threadId':'thread-1','turnId':'t','input':[{'type':'text','text':'work'}]})
        await eventually(lambda:factory.clients and factory.clients[0].queries)
        client=factory.clients[0];coordinator=instance._async_batches[('thread-1','t')];clock=BatchClock(coordinator)
        batch=asyncio.create_task(call_batch(instance,mixed_batch('slow'),'batch'))
        await flush_tasks();clock.advance(1);await flush_tasks();await asyncio.wait_for(batch,1)
        await client.messages.put(result_message(session_id=client.options.session_id,result='waiting'))
        await flush_tasks();gate.set()
        assert (await wait_completed(events))['params']['turn']['status']=='completed'
        assert factory.queries==2 and factory.connects==3 and calls==['fast','slow']
        assert factory.clients[1].queries==[]
        late=factory.clients[2].queries[0]
        assert json.dumps(late).count('evidence-slow')==1
        assert factory.clients[1].options.resume==factory.clients[2].options.resume==client.options.session_id
        assert instance._record('thread-1')['contextState']['generation']==0
    finally:
        gate.set(); await instance.stop()
