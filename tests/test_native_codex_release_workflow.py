"""The release archive gate consumes exact proven candidates without publishing."""
import asyncio
from pathlib import Path
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

import pytest

from tests.test_native_codex_windows_workflow import _action, _block, _field, _steps

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / '.github/workflows/native-codex-release-qualification.yml').read_text()


def _installed_proof_output():
    steps = _steps(_block(_block(TEXT, 'jobs'), 'package-install'))
    command = next(_field(step, 'run') for step in steps
                   if '.py install-local ' in (_field(step, 'run') or ''))
    outputs = re.findall(r'--output "([^"]+)"', command)
    assert len(outputs) == 1
    return outputs[0]


def _installed_selector_path():
    # Actual ubuntu-22.04 runner prefix, followed by the private worker TMPDIR
    # and the production bridge's tempfile prefix, random suffix, and filename.
    output = _installed_proof_output().replace('$RUNNER_TEMP', '/home/runner/work/_temp')
    return output + '/worker/tmp/bello-sel-12345678/selector.sock'


def test_installed_receipt_paths_leave_room_for_the_private_selector_socket():
    pathname_bytes = len(os.fsencode(_installed_selector_path()))
    assert pathname_bytes <= 103  # below both Darwin and Linux pathname limits
    assert pathname_bytes == 94
    assert _installed_proof_output() == '$RUNNER_TEMP/release-receipts/installed'
    steps = _steps(_block(_block(TEXT, 'jobs'), 'package-install'))
    receipts = next(step for step in steps if _action(step) == 'actions/upload-artifact'
                    and _field(step, 'if') == 'always()')
    paths = _field(_block(receipts, 'with'), 'path').splitlines()
    prefix = '${{ runner.temp }}/release-receipts/installed/'
    assert {path.strip() for path in paths if path.strip().startswith(prefix)} == {
        prefix + 'installed-proof.json', prefix + 'worker/report.json',
        prefix + 'worker/*/report.json', prefix + 'worker/*/*/result.json',
    }
    assert 'installed-release-proof' not in TEXT


@pytest.mark.skipif(sys.platform not in {'darwin', 'linux'},
                    reason='Windows production selector uses authenticated TCP, not AF_UNIX')
@pytest.mark.parametrize('case', ('native_maximum', 'one_byte_over', 'old_workflow', 'current_workflow'))
def test_installed_selector_real_posix_socket_boundary(case, monkeypatch):
    from supervisor.runtime.codex_distiller import CodexDistillerBridge

    maximum = 103 if sys.platform == 'darwin' else 107
    length, expected = {
        'native_maximum': (maximum, True),
        'one_byte_over': (maximum + 1, False),
        'old_workflow': (108, False),
        'current_workflow': (len(os.fsencode(_installed_selector_path())), True),
    }[case]
    suffix = '/bello-sel-12345678/selector.sock'
    # pytest's own temporary directory can already exceed AF_UNIX's limit.
    # The only removed tree is this newly created, private tempfile fixture.
    with tempfile.TemporaryDirectory(prefix='bello-sock-', dir='/tmp') as fixture:
        base = Path(fixture)
        assert base.stat().st_mode & 0o777 == 0o700
        padding = length - len(suffix) - len(os.fsencode(fixture)) - 1
        assert 0 < padding < 200
        temporary = base / ('p' * padding)
        temporary.mkdir(mode=0o700)
        raw_path = temporary / 'bello-sel-12345678' / 'selector.sock'
        raw_path.parent.mkdir(mode=0o700)
        assert len(os.fsencode(str(raw_path))) == length
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
            if expected:
                endpoint.bind(str(raw_path))
            else:
                with pytest.raises(OSError, match=r'^AF_UNIX path too long$'):
                    endpoint.bind(str(raw_path))
        if expected:
            raw_path.unlink()
        raw_path.parent.rmdir()

        for name in ('TMPDIR', 'TMP', 'TEMP'):
            monkeypatch.setenv(name, str(temporary))
        monkeypatch.setattr(tempfile, 'tempdir', None)
        directory = tempfile.TemporaryDirectory
        allocated = []

        def observe_directory(*args, **kwargs):
            result = directory(*args, **kwargs)
            allocated.append(Path(result.name) / 'selector.sock')
            return result

        monkeypatch.setattr(tempfile, 'TemporaryDirectory', observe_directory)

        class NoInference:
            async def distill(self, *args, **kwargs):
                raise AssertionError('Socket startup must not request inference')

        async def exercise_bridge():
            bridge = CodexDistillerBridge(NoInference(), base / 'state', transport='unix')
            try:
                if expected:
                    await bridge.start()
                    assert len(os.fsencode(bridge.environment['BELLO_SELECTOR_SOCKET'])) == length
                else:
                    with pytest.raises(OSError, match=r'^AF_UNIX path too long$'):
                        await bridge.start()
            finally:
                await bridge.close()
            assert len(allocated) == 1
            assert len(os.fsencode(str(allocated[0]))) == length
            assert not allocated[0].parent.exists()
            assert bridge._server is None and bridge._directory is None and bridge._socket_path is None

        asyncio.run(exercise_bridge())
        assert not any(temporary.iterdir())


def test_release_gate_is_readonly_and_serial_per_branch():
    triggers = _block(TEXT, 'on')
    assert _field(_block(triggers, 'push'), 'branches') == "['codex/072-recovery-validation']"
    assert re.search(r'^  workflow_dispatch:\s*$', triggers, re.M)
    assert _field(_block(TEXT, 'permissions'), 'contents') == 'read'
    assert _field(_block(TEXT, 'permissions'), 'actions') == 'read'
    assert _field(_block(TEXT, 'concurrency'), 'cancel-in-progress') == 'false'
    assert not re.search(r'\b(?:gh release|twine upload|npm publish|git push)\b', TEXT)
    assert not re.search(r'\$\{\{\s*secrets\.', TEXT)
    assert 'danger-full-access' not in TEXT


def test_exact_source_provenance_checked_before_download_and_execution():
    steps = _steps(_block(_block(TEXT, 'jobs'), 'package-install'))
    binder = next(i for i, s in enumerate(steps) if _action(s) == 'actions/github-script')
    binding = _field(_block(steps[binder], 'with'), 'script')
    for check in ('run.id !== run_id', 'run.head_sha !== head', 'run.run_attempt !== 1',
                  "run.status !== 'completed'", "run.conclusion !== 'success'", "run.event !== 'push'",
                  "run.path !== '.github/workflows/native-codex-candidate.yml'",
                  'run.repository.full_name', 'run.head_repository.full_name',
                  'a.id !== Number(id)', 'a.name !== name', 'a.size_in_bytes !== Number(size)',
                  'a.digest !== digest', 'a.expired', 'a.workflow_run?.id !== run_id', 'a.workflow_run?.head_sha !== head'):
        assert check in binding
    assert "const head = '0bbe6e35b7bdc0224f039999c6a2e32c49b855f6'" in binding
    assert 'const run_id = 37769628940' in binding
    downloads = [(i, s) for i, s in enumerate(steps) if _action(s) == 'actions/download-artifact']
    assert len(downloads) == 2
    for i, step in downloads:
        settings = _block(step, 'with')
        assert binder < i
        assert _field(settings, 'run-id') == '37769628940'
        assert _field(settings, 'repository') == 'Makson179/Bello'
        assert _field(settings, 'artifact-ids') in ('${{ matrix.binary-id }}', '${{ matrix.proof-id }}')
        assert _field(settings, 'merge-multiple') == 'true'
    verify = next(i for i, s in enumerate(steps) if 'verify-build' in (_field(s, 'run') or ''))
    package = next(i for i, s in enumerate(steps) if '.py package ' in (_field(s, 'run') or ''))
    install = next(i for i, s in enumerate(steps) if '.py install-local ' in (_field(s, 'run') or ''))
    assert max(i for i, _ in downloads) < verify < package < install
    assert '--restore-executable-modes' in _field(steps[verify], 'run')
    assert '--qualification source-proof/candidate-proof' in _field(steps[package], 'run')
    assert '--modernbert' in _field(steps[install], 'run')
    assert 'LOCALAPPDATA' in _field(steps[install], 'run')
    for i in (verify, package, install):
        assert _field(steps[i], 'continue-on-error') is None


def test_both_windows_python_extremes_and_linux_are_real_install_gates():
    job = _block(_block(TEXT, 'jobs'), 'package-install')
    assert _field(job, 'if') == "github.ref == 'refs/heads/codex/072-recovery-validation'"
    matrix = _block(_block(job, 'strategy'), 'matrix')
    assert re.findall(r'os: ([\w.-]+)', matrix) == ['ubuntu-22.04', 'windows-2022', 'windows-2025']
    assert re.findall(r"python: '([\d.]+)'", matrix) == ['3.11', '3.11', '3.14']
    assert re.findall(r"proof-id: '(\d+)'", matrix) == ['11550105744', '11552567973', '11552567973']
    for step in _steps(job):
        if _action(step) == 'actions/checkout':
            assert _field(_block(step, 'with'), 'persist-credentials') == 'false'
    uploads = [s for s in _steps(job) if _action(s) == 'actions/upload-artifact']
    assert len(uploads) == 2
    assert _field(uploads[0], 'if') is None  # successful installed proof is mandatory
    assert _field(uploads[1], 'if') == 'always()'
    receipt_paths = _field(_block(uploads[1], 'with'), 'path')
    assert 'installed-proof.json' in receipt_paths and 'worker/report.json' in receipt_paths
    assert not any(sensitive in receipt_paths for sensitive in ('rpc.jsonl', 'provider-request', '**', 'stderr'))


def test_published_download_gate_has_no_local_archive_substitution_or_publication():
    text = (ROOT / '.github/workflows/native-codex-release-download.yml').read_text()
    assert set(re.findall(r'^  ([a-z_]+):', _block(text, 'on'), re.M)) == {'workflow_dispatch'}
    assert _field(_block(text, 'permissions'), 'contents') == 'read'
    assert _field(_block(text, 'concurrency'), 'cancel-in-progress') == 'false'
    job = _block(_block(text, 'jobs'), 'published-install')
    matrix = _block(_block(job, 'strategy'), 'matrix')
    assert re.findall(r'os: ([\w.-]+)', matrix) == ['ubuntu-22.04', 'macos-15', 'windows-2022', 'windows-2025']
    steps = _steps(job)
    assert not any(_action(step) == 'actions/download-artifact' for step in steps)
    proof = next(step for step in steps if 'install-published' in (_field(step, 'run') or ''))
    command = _field(proof, 'run')
    assert '--modernbert' in command and '--artifact' not in command
    assert '--output "$RUNNER_TEMP/published-release-proof"' in command
    assert 'LOCALAPPDATA' in command and 'runtime_parent="$RUNNER_TEMP"' in command
    assert _field(proof, 'continue-on-error') is None
    assert not re.search(r'\$\{\{\s*secrets\.', text)
    assert not re.search(r'\b(?:gh release|twine upload|git push|cargo build)\b', text)


def test_executable_source_binding_rejects_identity_and_digest_substitution():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for the GitHub-script semantic contract')
    steps = _steps(_block(_block(TEXT, 'jobs'), 'package-install'))
    binder = next(s for s in steps if _action(s) == 'actions/github-script')
    script = _field(_block(binder, 'with'), 'script')
    # Execute the exact workflow JavaScript with data-only API fixtures; file
    # writes remain in memory and there is no network or authentication input.
    driver = r'''
const script = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const bind = new AsyncFunction('github', 'context', 'require', 'process', script);
const runId = 37769628940, head = '0bbe6e35b7bdc0224f039999c6a2e32c49b855f6';
const matrix = [
  ['linux-x64','linux','11549045823','530814955','11550105744','39201'],
  ['windows-x64','windows-2022-py3.11','11552345407','492480932','11552567973','57652'],
  ['windows-x64','windows-2022-py3.11','11552345407','492480932','11552567973','57652'],
];
const mutations = [null, 'repository', 'run-head', 'run-status', 'run-conclusion', 'run-attempt',
  'run-id', 'run-path', 'run-branch', 'run-event', 'head-repository', 'artifact-id',
  'artifact-name', 'artifact-size', 'artifact-expired', 'artifact-digest', 'artifact-run', 'artifact-head'];
(async () => {
  let tested = 0;
  for (const [target,label,bid,bs,pid,ps] of matrix) for (const mutation of mutations) {
    const env = {TARGET:target, PROOF_LABEL:label, BINARY_ID:bid, BINARY_SIZE:bs,
      PROOF_ID:pid, PROOF_SIZE:ps, BINARY_DIGEST:'sha256:'+'a'.repeat(64),
      PROOF_DIGEST:'sha256:'+'b'.repeat(64), RUNNER_TEMP:'/fixture'};
    const context = {repo:{owner:'Makson179',repo:'Bello'},sha:'c'.repeat(40)};
    const run = {id:runId,head_sha:head,run_attempt:1,status:'completed',conclusion:'success',
      event:'push',head_branch:'codex/072-recovery-validation',
      path:'.github/workflows/native-codex-candidate.yml',
      repository:{full_name:'Makson179/Bello'},head_repository:{full_name:'Makson179/Bello'}};
    const artifacts = [
      {id:Number(bid),name:`native-codex-candidate-0161-${target}-${head}-${runId}-1`,size_in_bytes:Number(bs),digest:env.BINARY_DIGEST},
      {id:Number(pid),name:`native-codex-candidate-0161-${label}-proof-${head}-${runId}-1`,size_in_bytes:Number(ps),digest:env.PROOF_DIGEST},
    ].map(a => ({...a,expired:false,workflow_run:{id:runId,head_sha:head}}));
    switch(mutation) {
      case 'repository': context.repo.owner='different'; break;
      case 'run-head': run.head_sha='d'.repeat(40); break;
      case 'run-status': run.status='in_progress'; break;
      case 'run-conclusion': run.conclusion='failure'; break;
      case 'run-attempt': run.run_attempt=2; break;
      case 'run-id': run.id++; break;
      case 'run-path': run.path='other.yml'; break;
      case 'run-branch': run.head_branch='other'; break;
      case 'run-event': run.event='pull_request'; break;
      case 'head-repository': run.head_repository.full_name='other/Bello'; break;
      case 'artifact-id': artifacts[0].id++; break;
      case 'artifact-name': artifacts[0].name+='other'; break;
      case 'artifact-size': artifacts[0].size_in_bytes++; break;
      case 'artifact-expired': artifacts[0].expired=true; break;
      case 'artifact-digest': artifacts[0].digest='sha256:'+'d'.repeat(64); break;
      case 'artifact-run': artifacts[0].workflow_run.id++; break;
      case 'artifact-head': artifacts[0].workflow_run.head_sha='d'.repeat(40); break;
    }
    let written = 0, count = 0, accepted = false;
    const github = {rest:{actions:{
      getWorkflowRun: async () => ({data:run}),
      getArtifact: async () => ({data:artifacts[count++]}),
    }}};
    const scopedRequire = name => {
      if(name === 'path') return require('path');
      if(name === 'fs') return {mkdirSync(){},writeFileSync(_path,bytes){
        const value=JSON.parse(bytes);
        if(value.source_run_id!==runId || value.source_head!==head || value.artifacts.length!==2) throw Error('bad receipt');
        written++;
      }};
      throw Error('Unexpected module');
    };
    try { await bind(github,context,scopedRequire,{env}); accepted=true; } catch (_) {}
    if(accepted !== (mutation===null) || written !== (mutation===null ? 1 : 0)) throw Error('Failed mutation: '+mutation);
    tested++;
  }
  process.stdout.write(JSON.stringify({tested}));
})().catch(e => {process.stderr.write(e.message);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', driver], input=json.dumps(script), text=True,
                            capture_output=True, timeout=30, check=True)
    assert json.loads(result.stdout) == {'tested': 54}
