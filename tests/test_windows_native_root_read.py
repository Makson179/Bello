from copy import deepcopy
import json
import ntpath
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from supervisor.appserver import AppServerError
from supervisor.project_config import ProjectConfig, load_project_config, save_project_config, _runtime_updates_for_fields
from supervisor.runtime import client as runtime_client, codex, codex_permissions as permissions
from supervisor.schemas.models import BelloConfig
from supervisor.controller import BelloController
from supervisor.project_config import ProjectConfigError, project_config_path


def test_project_opt_in_is_false_and_roundtrips(tmp_path):
    assert ProjectConfig().windows_native_root_read is False
    config=ProjectConfig(windows_native_root_read=True)
    save_project_config(tmp_path,config)
    assert load_project_config(tmp_path,create=False).windows_native_root_read is True
    assert config.to_json_data()['windows_native_root_read'] is True
    assert BelloConfig(project_root=str(tmp_path),task_path='TASK.md').windows_native_root_read is False
    assert _runtime_updates_for_fields(config, fields={'windows_native_root_read'})['windows_native_root_read'] is True


@pytest.mark.parametrize('value',[1,'true',None,[],{}])
def test_run_opt_in_must_be_exact_bool(tmp_path,value):
    client=runtime_client.RuntimeClient(cwd=tmp_path)
    with pytest.raises(ValueError,match='windows_native_root_read must be a boolean'):
        client.configure_run(windows_native_root_read=value)


@pytest.fixture
def windows_paths(monkeypatch):
    monkeypatch.setattr(permissions,'Path',PureWindowsPath)
    monkeypatch.setattr(permissions,'os',SimpleNamespace(path=ntpath))


@pytest.mark.parametrize('mode',['read-only','workspace-write'])
@pytest.mark.parametrize('cwd,root',[(r'C:\work','C:\\'),(r'\\server\share\work','\\\\server\\share\\')])
def test_mapper_adds_only_exact_read_root_and_private_denies(windows_paths,mode,cwd,root):
    params={'cwd':cwd,'sandbox':mode,'runtimeWorkspaceRoots':[r'D:\deps'],'networkAccess':False}
    before=deepcopy(params)
    private=(PureWindowsPath(r'C:\controller'),PureWindowsPath(r'C:\controller\codex-home'))
    scratch=PureWindowsPath(cwd)/'scratch'
    old=permissions.native_permission_params(params,temp_dir=scratch if mode=='workspace-write' else None)
    mapped=permissions.native_permission_params(params,temp_dir=scratch if mode=='workspace-write' else None,
        windows_root_read=True,private_read_roots=private)
    fs=mapped['config']['permissions']['bello-native']['filesystem']
    prior=old['config']['permissions']['bello-native']['filesystem']
    assert fs=={**prior,root:'read',str(private[0]):'deny',str(private[1]):'deny'}
    assert {p for p,v in fs.items() if v=='write'}=={p for p,v in prior.items() if v=='write'}
    assert mapped['runtimeWorkspaceRoots']==old['runtimeWorkspaceRoots']
    assert mapped['config']['permissions']['bello-native']['network']=={'enabled':False}
    assert ':root' not in fs and params==before


def test_private_ancestor_of_workspace_or_scratch_is_rejected(windows_paths):
    for private,scratch in [('C:\\',r'C:\work\tmp'),(r'C:\work',r'C:\work\tmp'),(r'C:\state',r'C:\state\tmp')]:
        with pytest.raises(AppServerError,match='overlaps'):
            permissions.native_permission_params({'cwd':r'C:\work'},temp_dir=PureWindowsPath(scratch),
                windows_root_read=True,private_read_roots=(PureWindowsPath(private),))


@pytest.mark.asyncio
async def test_host_opt_in_cannot_be_supplied_by_thread_and_cannot_change_live(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime_client,'_IS_WINDOWS',True)
    work=tmp_path/'work';work.mkdir()
    client=runtime_client.RuntimeClient(cwd=tmp_path,state_dir=tmp_path/'state')
    await client.start()
    client._engine=AsyncMock()
    with pytest.raises(AppServerError,match='unsupported_permission_profile'):
        await client.request('thread/start',{'cwd':str(work),'model':'gpt-6-sol','windowsNativeRootRead':True})
    client._engine.assert_not_awaited()
    with pytest.raises(AppServerError,match='changes require stopping'):
        client.configure_run(windows_native_root_read=True)
    await client.stop()


@pytest.mark.asyncio
async def test_host_consent_persists_only_for_windows_codex_and_resume_matches(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime_client,'_IS_WINDOWS',True)
    work=tmp_path/'work';work.mkdir()
    client=runtime_client.RuntimeClient(cwd=tmp_path,state_dir=tmp_path/'state')
    client.configure_run(windows_native_root_read=True)
    await client.start()
    async def response(method,params,**kwargs):
        return {'thread':{'id':params['threadId']}} if method=='thread/start' else {}
    backend=SimpleNamespace(request=AsyncMock(side_effect=response))
    client._engine=AsyncMock(return_value=backend)
    try:
        result=await client.request('thread/start',{'cwd':str(work),'model':'gpt-6-sol','sandbox':'workspace-write',
                                                   'windowsNativeRootRead':False})
        host=result['thread']['id']
        assert client._threads[host]['windowsNativeRootRead'] is True
        for method in ('thread/resume','turn/start'):
            before=backend.request.await_count
            with pytest.raises(AppServerError,match='Windows root-read consent'):
                await client.request(method,{'threadId':host,'windowsNativeRootRead':False})
            assert backend.request.await_count==before
        result=await client.request('thread/start',{'cwd':str(work),'model':'claude-code/claude-sonnet-5',
                                                   'windowsNativeRootRead':True})
        assert 'windowsNativeRootRead' not in client._threads[result['thread']['id']]
    finally:await client.stop()


def backend_for(tmp_path,monkeypatch,windows=True):
    monkeypatch.setattr(codex,'_IS_WINDOWS',windows)
    monkeypatch.setattr(codex,'native_toolchain_read_paths',lambda cwd:())
    work=tmp_path/'workspace';work.mkdir()
    controller=tmp_path/'controller';controller.mkdir(mode=0o700)
    source=tmp_path/'source-codex-home';source.mkdir(mode=0o700)
    monkeypatch.setenv('CODEX_HOME',str(source))
    backend=codex.CodexBackend(state_dir=controller/'runtime',emit=AsyncMock(),private_read_roots=(controller,))
    (backend.state_dir/'codex-home').mkdir(mode=0o700)
    return backend,work,controller


def test_backend_uses_workspace_scratch_and_denies_existing_private_roots(tmp_path,monkeypatch):
    backend,work,controller=backend_for(tmp_path,monkeypatch)
    params={'cwd':str(work),'model':'gpt-6-sol','sandbox':'workspace-write','windowsNativeRootRead':True}
    mapped=backend._thread_params(params)
    fs=mapped['config']['permissions']['bello-native']['filesystem']
    scratch=backend._windows_scratch[str(work)]
    assert 'runtimeScratchRoot' not in params
    assert scratch.is_dir() and scratch.is_relative_to(work) and scratch!=work
    assert not scratch.is_relative_to(controller)
    assert fs[str(work.anchor)]=='read'
    assert fs[str(controller)]==fs[str(backend.state_dir)]==fs[str(backend.state_dir/'codex-home')]=='deny'
    assert fs[str(tmp_path/'source-codex-home')]=='deny'
    assert {p for p,v in fs.items() if v=='write'}=={str(work),str(scratch)}
    assert {fs[str(work/n)] for n in ('.git','.agents','.codex')}=={'read'}
    assert set(mapped['config']['shell_environment_policy']['set'].values())=={str(scratch)}
    assert mapped['config']['windows']['sandbox']=='elevated'
    assert 'sandbox' not in mapped
    again=backend._thread_params(params)
    assert again==mapped


def test_readonly_opt_in_does_not_create_or_grant_scratch(tmp_path,monkeypatch):
    backend,work,controller=backend_for(tmp_path,monkeypatch)
    params={'cwd':str(work),'model':'gpt-6-sol','sandbox':'read-only','windowsNativeRootRead':True}
    mapped=backend._thread_params(params)
    fs=mapped['config']['permissions']['bello-native']['filesystem']
    assert not any(mode=='write' for mode in fs.values())
    assert list(work.iterdir())==[] and 'runtimeScratchRoot' not in params
    assert fs[str(controller)]=='deny'


def test_unix_opt_in_does_not_change_native_scope(tmp_path,monkeypatch):
    backend,work,_=backend_for(tmp_path,monkeypatch,windows=False)
    a={'cwd':str(work),'model':'gpt-6-sol','sandbox':'workspace-write'}
    assert backend._thread_params(a)==backend._thread_params({**a,'windowsNativeRootRead':True})
    assert not list(work.iterdir())


def test_missing_or_reparse_private_root_fails_before_thread_dispatch(tmp_path,monkeypatch):
    backend,work,_=backend_for(tmp_path,monkeypatch)
    (backend.state_dir/'codex-home').rmdir()
    with pytest.raises(FileNotFoundError):
        backend._thread_params({'cwd':str(work),'model':'gpt-6-sol','sandbox':'read-only','windowsNativeRootRead':True})
    (backend.state_dir/'codex-home').symlink_to(work,target_is_directory=True)
    with pytest.raises(AppServerError,match='real private directories'):
        backend._thread_params({'cwd':str(work),'model':'gpt-6-sol','sandbox':'read-only','windowsNativeRootRead':True})


@pytest.mark.asyncio
async def test_native_resume_cannot_change_persisted_opt_in_before_account(tmp_path,monkeypatch):
    backend,work,_=backend_for(tmp_path,monkeypatch)
    backend._threads['old']={'params':{'cwd':str(work),'windowsNativeRootRead':True}}
    backend._ensure_initialized=AsyncMock()
    for method in ('thread/resume','turn/start'):
        with pytest.raises(AppServerError,match='cannot change Windows root-read consent'):
            await backend.request(method,{'threadId':'old','windowsNativeRootRead':False})
    backend._ensure_initialized.assert_not_awaited()
    await backend.stop()


@pytest.mark.parametrize('value',[1,'true',None,[],{}])
def test_project_opt_in_rejects_nonboolean_on_load(tmp_path,value):
    save_project_config(tmp_path,ProjectConfig())
    path=project_config_path(tmp_path)
    payload=json.loads(path.read_text())
    payload['windows_native_root_read']=value
    path.write_text(json.dumps(payload))
    with pytest.raises(ProjectConfigError,match='must be true or false'):
        load_project_config(tmp_path,create=False)


@pytest.mark.parametrize('value',[True,False])
def test_controller_reads_explicit_project_policy_and_saved_policy(value):
    controller=object.__new__(BelloController)
    controller.project_config=ProjectConfig(windows_native_root_read=value)
    assert controller._windows_native_root_read_enabled() is value
    controller.project_config=None
    controller.store=SimpleNamespace(read_json=lambda *args:{'windows_native_root_read':value})
    assert controller._windows_native_root_read_enabled() is value


@pytest.mark.parametrize('value',[1,'true',None])
def test_controller_rejects_malformed_saved_policy(value):
    controller=object.__new__(BelloController)
    controller.project_config=None
    controller.store=SimpleNamespace(read_json=lambda *args:{'windows_native_root_read':value})
    with pytest.raises(ValueError,match='must be a boolean'):
        controller._windows_native_root_read_enabled()


@pytest.mark.asyncio
@pytest.mark.parametrize('value',[1,False,'true',None])
async def test_native_override_cannot_exploit_boolean_equality(tmp_path,monkeypatch,value):
    backend,work,_=backend_for(tmp_path,monkeypatch)
    backend._threads['old']={'params':{'cwd':str(work),'windowsNativeRootRead':True}}
    backend._ensure_initialized=AsyncMock()
    for method in ('thread/resume','turn/start'):
        with pytest.raises(AppServerError,match='cannot change Windows root-read consent'):
            await backend.request(method,{'threadId':'old','windowsNativeRootRead':value})
    backend._ensure_initialized.assert_not_awaited()
    await backend.stop()


@pytest.mark.parametrize('value',[1,False,'true',None])
def test_host_override_cannot_exploit_boolean_equality(tmp_path,value):
    client=runtime_client.RuntimeClient(cwd=tmp_path)
    with pytest.raises(AppServerError,match='cannot change its Windows root-read consent'):
        client._validate_scope_overrides({'windowsNativeRootRead':True},{'windowsNativeRootRead':value})


def test_missing_source_home_is_not_created_or_added_as_deny(tmp_path,monkeypatch):
    backend,work,_=backend_for(tmp_path,monkeypatch)
    source=tmp_path/'source-codex-home';source.rmdir()
    mapped=backend._thread_params({'cwd':str(work),'model':'gpt-6-sol','sandbox':'read-only','windowsNativeRootRead':True})
    fs=mapped['config']['permissions']['bello-native']['filesystem']
    assert str(source) not in fs and not source.exists()


def test_source_home_ancestor_of_workspace_fails_before_scratch_creation(tmp_path,monkeypatch):
    backend,work,_=backend_for(tmp_path,monkeypatch)
    monkeypatch.setenv('CODEX_HOME',str(tmp_path))
    with pytest.raises(AppServerError,match='overlaps'):
        backend._thread_params({'cwd':str(work),'model':'gpt-6-sol','sandbox':'workspace-write','windowsNativeRootRead':True})
    assert list(work.iterdir())==[]


def test_source_home_is_exact_existing_directory_not_auth_leaf(tmp_path,monkeypatch):
    backend,work,_=backend_for(tmp_path,monkeypatch)
    source=tmp_path/'source-codex-home'
    mapped=backend._thread_params({'cwd':str(work),'model':'gpt-6-sol','sandbox':'read-only','windowsNativeRootRead':True})
    fs=mapped['config']['permissions']['bello-native']['filesystem']
    assert fs[str(source)]=='deny'
    assert str(source/'auth.json') not in fs and not (source/'auth.json').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('stored',[False,1,None])
async def test_saved_policy_must_match_exact_host_consent_before_engine(tmp_path,monkeypatch,stored):
    monkeypatch.setattr(runtime_client,'_IS_WINDOWS',True)
    client=runtime_client.RuntimeClient(cwd=tmp_path)
    client.configure_run(windows_native_root_read=True)
    await client.start()
    client._threads['old']={'qualifiedModel':'openai-codex/gpt-6-sol','engine':'codex','cwd':str(tmp_path),
                            'windowsNativeRootRead':stored,'closed':True}
    client._engine=AsyncMock()
    for method in ('thread/resume','turn/start'):
        with pytest.raises(AppServerError,match='different Windows root-read consent'):
            await client.request(method,{'threadId':'old'})
    client._engine.assert_not_awaited()
    await client.stop()


@pytest.mark.asyncio
async def test_native_start_resume_turn_keep_same_permission_contract(tmp_path,monkeypatch):
    from tests.test_runtime_codex_backend import FakeNative
    backend,work,_=backend_for(tmp_path,monkeypatch)
    backend._factory=FakeNative
    params={'threadId':'host','cwd':str(work),'model':'gpt-6-astra','sandbox':'workspace-write','windowsNativeRootRead':True}
    try:
        await backend.request('thread/start',params)
        first=next(p for m,p in backend._client.calls if m=='thread/start')
        assert backend._threads['host']['params']['windowsNativeRootRead'] is True
        assert backend._client.options['environment_overrides']['CODEX_HOME']==str(tmp_path/'source-codex-home')
        await backend.request('thread/resume',{'threadId':'host','windowsNativeRootRead':True})
        resumed=next(p for m,p in backend._client.calls if m=='thread/resume')
        assert resumed['config']['permissions']==first['config']['permissions']
        await backend.request('turn/start',{'threadId':'host','turnId':'turn','input':[],'windowsNativeRootRead':True})
        turn=next(p for m,p in backend._client.calls if m=='turn/start')
        assert 'sandboxPolicy' not in turn and 'permissions' not in turn
    finally:
        await backend.stop()
