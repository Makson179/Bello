"""CI-only ACL diagnostics must expose a mismatch without weakening comparison."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "native/windows-sandbox/ci/verify_host_setup.ps1"
PWSH = shutil.which("pwsh")


def test_acl_mismatch_diagnostics_preserve_strict_checks_and_are_uploaded():
    source = SCRIPT.read_text(encoding="utf-8")
    assertion = source.split("function Assert-AclEqual", 1)[1].split("\nNew-Item", 1)[0]
    assert '@("owner", "group", "control", "revision")' in assertion
    assert "$Expected[$field] -ne $Actual[$field]" in assertion
    assert "$before -cne $after" in assertion
    assert "throw \"host setup changed unrelated" in assertion
    diagnostic = source.split("function Write-AclMismatch", 1)[1].split("function Assert-AclEqual", 1)[0]
    assert 'Set-Content -LiteralPath "$Report.mismatch.json"' in diagnostic
    assert "Set-Acl" not in diagnostic and "Invoke-HostOperation" not in diagnostic
    assert "expected = $Expected; actual = $Actual" in diagnostic
    assert "-bxor" in diagnostic
    for workflow in ("tests.yml", "runtime-windows.yml"):
        content = (ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8")
        assert "bello-host-setup-*.mismatch.json" in content
        assert "bello-checkout-drive-*.mismatch.json" in content


@pytest.mark.skipif(PWSH is None, reason="PowerShell is required to execute the CI assertion functions")
@pytest.mark.parametrize("field", ["identical", "control", "owner", "group", "revision", "aces"])
def test_real_powershell_acl_comparison_reports_without_changing_input(tmp_path: Path, field: str):
    # Load only these pure assertion/diagnostic functions from the script's AST.
    # Do not execute its host-prepare, host-remove or Get-Acl body.
    probe = r'''
param([string]$Source, [string]$Output, [string]$Field)
$ErrorActionPreference = "Stop"
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @("Describe-ControlFlags", "Write-AclMismatch", "Assert-AclEqual")) {
    $definition = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $false))
    if ($definition.Count -ne 1) { throw "expected exactly one assertion function: $name" }
    . ([scriptblock]::Create($definition[0].Extent.Text))
}
$Report = $Output; $Phase = "Prepare"; $selectedDrive = "D:"
$expected = @{ owner = "owner"; group = "group"; control = 0x8004; revision = 2; aces = @(@{ binary = "AA==" }) }
$actual = $expected | ConvertTo-Json -Depth 8 | ConvertFrom-Json -AsHashtable
switch ($Field) {
    "control" { $actual.control = 0x8404 }
    "owner" { $actual.owner = "changed-owner" }
    "group" { $actual.group = "changed-group" }
    "revision" { $actual.revision = 4 }
    "aces" { $actual.aces = @(@{ binary = "AQ==" }) }
}
$expectedBefore = $expected | ConvertTo-Json -Depth 8 -Compress
$actualBefore = $actual | ConvertTo-Json -Depth 8 -Compress
$message = $null
try { Assert-AclEqual $expected $actual "D:\" }
catch { $message = $_.Exception.Message }
@{
    message = $message
    expectedUnchanged = $expectedBefore -ceq ($expected | ConvertTo-Json -Depth 8 -Compress)
    actualUnchanged = $actualBefore -ceq ($actual | ConvertTo-Json -Depth 8 -Compress)
} | ConvertTo-Json -Compress
'''
    probe_path = tmp_path / "probe.ps1"
    probe_path.write_text(probe, encoding="utf-8")
    report = tmp_path / "report.json"
    result = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe_path),
         str(SCRIPT), str(report), field],
        capture_output=True, text=True, timeout=30, check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["expectedUnchanged"] and observed["actualUnchanged"]
    diagnostic_path = Path(str(report) + ".mismatch.json")
    if field == "identical":
        assert observed["message"] is None
        assert not diagnostic_path.exists()
        return
    assert "host setup changed unrelated" in observed["message"]
    assert "target=D:\\" in observed["message"]
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8-sig"))
    assert diagnostic["field"] == field
    assert diagnostic["expected"]["control"] == 0x8004
    assert diagnostic["expectedControl"]["hex"] == "0x8004"
    assert diagnostic["target"] == "D:\\"
    if field == "control":
        assert diagnostic["actualControl"]["hex"] == "0x8404"
        assert diagnostic["changedControl"]["hex"] == "0x0400"
        assert "DiscretionaryAclAutoInherited" in diagnostic["changedControl"]["flags"]
        assert "0x8004" in observed["message"] and "0x8404" in observed["message"]
