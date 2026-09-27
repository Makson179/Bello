from copy import deepcopy
import ntpath
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from supervisor.appserver import AppServerError
from supervisor.runtime import client as client_module, codex, codex_permissions as permissions


@pytest.fixture
def windows_paths(monkeypatch):
    # Exercise Windows path semantics without changing process-global os.name.
    monkeypatch.setattr(permissions, 'Path', PureWindowsPath)
    monkeypatch.setattr(permissions, 'os', SimpleNamespace(path=ntpath))


@pytest.mark.parametrize('mode', ['read-only', 'workspace-write'])
@pytest.mark.parametrize('cwd,roots', [
    (r'C:\work', []), (r'C:\work', [r'C:\work', r'C:\deps']),
    (r'C:\work', ['D:\\']), (r'C:\work', [r'C:\rootfile']),
    (r'\\server\share\work', ['\\\\server\\other\\']),
])
def test_scoped_windows_paths_rejected_without_mutation(windows_paths, cwd, roots, mode):
    params = {'cwd': cwd, 'runtimeWorkspaceRoots': roots, 'sandbox': mode}
    before = deepcopy(params)
    with pytest.raises(AppServerError, match='unsupported_permission_profile'):
        permissions.validate_windows_native_scope(params)
    assert params == before


@pytest.mark.parametrize('cwd,roots', [
    (r'C:\work', ['C:\\']), (r'C:\work', ['c:/']),
    (r'C:\work', [r'C:\other\..']), ('C:\\', []),
    (r'\\server\share\work', ['\\\\server\\share\\']),
    (r'\\SERVER\SHARE\work', ['\\\\server\\share\\']),
    (r'\\?\C:\work', ['\\\\?\\C:\\']),
])
@pytest.mark.parametrize('mode', ['read-only', 'workspace-write'])
def test_explicit_effective_drive_or_share_root_is_preserved(windows_paths, cwd, roots, mode):
    params = {'cwd': cwd, 'runtimeWorkspaceRoots': roots, 'sandbox': mode}
    before = deepcopy(params)
    permissions.validate_windows_native_scope(params)
    mapped = permissions.native_permission_params(params)
    assert ':root' not in mapped['config']['permissions']['bello-native']['filesystem']
    assert params == before


def test_ignored_user_config_cannot_claim_root_access(windows_paths):
    params = {'cwd': r'C:\work', 'config': {'permissions': {'bello-native': {
        'filesystem': {':root': 'read'}}}, 'windows': {'sandbox': 'disabled'}}}
    with pytest.raises(AppServerError, match='unsupported_permission_profile'):
        permissions.validate_windows_native_scope(params)


def test_explicit_full_access_not_changed(windows_paths):
    permissions.validate_windows_native_scope({'sandbox': 'danger-full-access'})


@pytest.mark.parametrize('mode', ['read-only', 'workspace-write'])
@pytest.mark.asyncio
async def test_backend_rejects_before_client_bridge_account_or_model(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(codex, '_IS_WINDOWS', True)
    factory = Mock(side_effect=AssertionError('must not create native client'))
    emit, tool = AsyncMock(), AsyncMock()
    backend = codex.CodexBackend(state_dir=tmp_path/'state', emit=emit,
                                tool_handler=tool, client_factory=factory, distiller=object())
    params = {'cwd': str(tmp_path/'work'), 'threadId': 'host', 'model': 'gpt-6-sol',
              'sandbox': mode, 'asyncTools': True, 'distillerEnabled': True}
    with pytest.raises(AppServerError, match='unsupported_permission_profile'):
        await backend.request('thread/start', params)
    factory.assert_not_called()
    tool.assert_not_awaited()
    emit.assert_not_awaited()
    assert backend._client is None and backend._bridge is None
    assert backend._threads == {} and not backend._initialized
    await backend.stop()


@pytest.mark.parametrize('method', ['thread/resume', 'turn/start'])
@pytest.mark.asyncio
async def test_persisted_backend_scoped_thread_fails_before_any_dispatch(tmp_path, monkeypatch, method):
    monkeypatch.setattr(codex, '_IS_WINDOWS', True)
    factory = Mock(side_effect=AssertionError('must not create native client'))
    backend = codex.CodexBackend(state_dir=tmp_path/'state', emit=AsyncMock(), client_factory=factory)
    backend._threads['host'] = {'params': {'cwd': str(tmp_path/'work'), 'sandbox': 'workspace-write'}}
    with pytest.raises(AppServerError, match='unsupported_permission_profile'):
        await backend.request(method, {'threadId': 'host'})
    factory.assert_not_called()
    await backend.stop()


@pytest.mark.parametrize('method', ['thread/start', 'thread/resume', 'turn/start'])
@pytest.mark.asyncio
async def test_explicit_root_backend_passes_gate_without_policy_rewrite(tmp_path, monkeypatch, method):
    monkeypatch.setattr(codex, '_IS_WINDOWS', True)
    backend = codex.CodexBackend(state_dir=tmp_path/'state', emit=AsyncMock())
    params = {'cwd': str(tmp_path/'work'), 'runtimeWorkspaceRoots': [tmp_path.anchor],
              'sandbox': 'workspace-write', 'threadId': 'host'}
    backend._threads['host'] = {'params': deepcopy(params)}
    backend._ensure_initialized = AsyncMock()
    backend._request = AsyncMock(return_value={'ok': True})
    before = deepcopy(params)
    assert await backend.request(method, params) == {'ok': True}
    assert params == before
    backend._request.assert_awaited_once_with(method, params, 30)
    await backend.stop()


@pytest.mark.asyncio
async def test_turn_cannot_bypass_gate_with_non_forwarded_root_override(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, '_IS_WINDOWS', True)
    backend = codex.CodexBackend(state_dir=tmp_path/'state', emit=AsyncMock())
    backend._threads['host'] = {'params': {'cwd': str(tmp_path/'work'), 'sandbox': 'workspace-write'}}
    backend._ensure_initialized = AsyncMock()
    with pytest.raises(AppServerError, match='unsupported_permission_profile'):
        await backend.request('turn/start', {'threadId': 'host', 'runtimeWorkspaceRoots': [tmp_path.anchor],
                                             'sandbox': 'danger-full-access'})
    backend._ensure_initialized.assert_not_awaited()
    await backend.stop()


@pytest.mark.asyncio
async def test_unix_scoped_profile_not_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, '_IS_WINDOWS', False)
    backend = codex.CodexBackend(state_dir=tmp_path/'state', emit=AsyncMock())
    backend._ensure_initialized = AsyncMock()
    backend._request = AsyncMock(return_value={'ok': True})
    assert await backend.request('thread/start', {'cwd': str(tmp_path/'work')}) == {'ok': True}
    backend._request.assert_awaited_once()
    await backend.stop()


@pytest.mark.asyncio
async def test_runtime_rejects_before_engine_or_thread_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(client_module, '_IS_WINDOWS', True)
    root, work = tmp_path/'root', tmp_path/'work'
    root.mkdir(); work.mkdir()
    client = client_module.RuntimeClient(cwd=root, state_dir=tmp_path/'state')
    await client.start()
    client._engine = AsyncMock(side_effect=AssertionError('must not load engine'))
    try:
        with pytest.raises(AppServerError, match='unsupported_permission_profile'):
            await client.request('thread/start', {'cwd': str(work), 'model': 'gpt-6-sol',
                                                  'sandbox': 'workspace-write'})
        client._engine.assert_not_awaited()
        assert client._threads == {} and client._engines == {}
    finally:
        await client.stop()


@pytest.mark.parametrize('method', ['thread/resume', 'turn/start'])
@pytest.mark.asyncio
async def test_runtime_old_scoped_record_rejected_before_engine_load(tmp_path, monkeypatch, method):
    monkeypatch.setattr(client_module, '_IS_WINDOWS', True)
    root, work = tmp_path/'root', tmp_path/'work'
    root.mkdir(); work.mkdir()
    client = client_module.RuntimeClient(cwd=root, state_dir=tmp_path/'state')
    await client.start()
    client._threads['old'] = {
        'cwd': str(work), 'sandbox': 'workspace-write', 'runtimeWorkspaceRoots': [str(work)],
        'qualifiedModel': 'openai-codex/gpt-6-sol', 'engine': 'codex', 'networkAccess': False,
        'distillerEnabled': False, 'asyncTools': False,
    }
    before = deepcopy(client._threads)
    client._engine = AsyncMock(side_effect=AssertionError('must not load engine'))
    try:
        with pytest.raises(AppServerError, match='unsupported_permission_profile'):
            await client.request(method, {'threadId': 'old'})
        client._engine.assert_not_awaited()
        assert client._threads == before
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_runtime_explicit_root_preserved_and_other_engine_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr(client_module, '_IS_WINDOWS', True)
    root, work = tmp_path/'root', tmp_path/'work'
    root.mkdir(); work.mkdir()
    client = client_module.RuntimeClient(cwd=root, state_dir=tmp_path/'state')
    await client.start()
    async def response(method, params, **kwargs):
        assert method == 'thread/start'
        return {'thread': {'id': params['threadId']}}
    backend = SimpleNamespace(request=AsyncMock(side_effect=response))
    client._engine = AsyncMock(return_value=backend)
    try:
        explicit = {'cwd': str(work), 'model': 'gpt-6-sol', 'sandbox': 'workspace-write',
                    'runtimeWorkspaceRoots': [work.anchor]}
        await client.request('thread/start', explicit)
        sent = backend.request.await_args.args[1]
        assert sent['runtimeWorkspaceRoots'] == [work.anchor]
        assert sent['sandbox'] == 'workspace-write'
        client._engine.assert_awaited_once_with('codex')
        await client.request('thread/start', {'cwd': str(work), 'model': 'claude-code/claude-sonnet-5',
                                             'sandbox': 'workspace-write'})
        assert client._engine.await_args.args == ('claude-code',)
    finally:
        await client.stop()
