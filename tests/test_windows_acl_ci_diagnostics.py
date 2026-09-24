"""CI ACL comparisons accept only documented DACL-defaulted normalization."""
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
@pytest.mark.parametrize(
    ("expected_control", "actual_control", "mutation", "mismatch"),
    [
        pytest.param(0x8004, 0x8004, "none", None, id="identical"),
        pytest.param(0x800E, 0x800E, "none", None, id="identical-defaulted"),
        pytest.param(0x8004, 0x8404, "none", "control", id="inheritance-flag-changed"),
        pytest.param(0x8004, 0x8004, "owner", "owner", id="owner-changed"),
        pytest.param(0x8004, 0x8004, "group", "group", id="group-changed"),
        pytest.param(0x8004, 0x8004, "revision", "revision", id="revision-changed"),
        pytest.param(0x8004, 0x8004, "aces", "aces", id="ace-bytes-changed"),
        pytest.param(0x800E, 0x8006, "none", None, id="defaulted-cleared"),
        pytest.param(0x940E, 0x9406, "none", None, id="defaulted-cleared-other-flags-preserved"),
        pytest.param(0x800E, 0x8006, "owner", "owner", id="defaulted-cleared-owner-changed"),
        pytest.param(0x800E, 0x8006, "group", "group", id="defaulted-cleared-group-changed"),
        pytest.param(0x800E, 0x8006, "revision", "revision", id="defaulted-cleared-revision-changed"),
        pytest.param(0x800E, 0x8006, "aces", "aces", id="defaulted-cleared-ace-bytes-changed"),
        pytest.param(0x800E, 0x8006, "ace_order", "aces", id="defaulted-cleared-ace-order-changed"),
        pytest.param(0x8006, 0x800E, "none", "control", id="defaulted-set"),
        pytest.param(0x800A, 0x8002, "none", "control", id="defaulted-cleared-dacl-absent"),
        pytest.param(0x800E, 0x8002, "none", "control", id="defaulted-cleared-dacl-removed"),
        pytest.param(0x800A, 0x8006, "none", "control", id="defaulted-cleared-dacl-added"),
        pytest.param(0x800E, 0x8406, "none", "control", id="defaulted-cleared-other-flag-added"),
        pytest.param(0x940E, 0x8406, "none", "control", id="defaulted-cleared-protection-removed"),
        pytest.param(0x800E, 0x8004, "none", "control", id="defaulted-cleared-group-defaulted-removed"),
    ],
)
def test_real_powershell_acl_comparison_reports_without_changing_input(
    tmp_path: Path, expected_control: int, actual_control: int, mutation: str, mismatch: str | None,
):
    # Load only these pure assertion/diagnostic functions from the script's AST.
    # Do not execute its host-prepare, host-remove or Get-Acl body.
    probe = r'''
param([string]$Source, [string]$Output, [string]$Mutation, [int]$ExpectedControl, [int]$ActualControl)
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
$expected = @{
    owner = "owner"; group = "group"; control = $ExpectedControl; revision = 2
    aces = @(@{ binary = "AA==" }, @{ binary = "AQ==" })
}
$actual = $expected | ConvertTo-Json -Depth 8 | ConvertFrom-Json -AsHashtable
$actual.control = $ActualControl
switch ($Mutation) {
    "owner" { $actual.owner = "changed-owner" }
    "group" { $actual.group = "changed-group" }
    "revision" { $actual.revision = 4 }
    "aces" { $actual.aces[0].binary = "Ag==" }
    "ace_order" { $actual.aces = @($actual.aces[1], $actual.aces[0]) }
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
         str(SCRIPT), str(report), mutation, str(expected_control), str(actual_control)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["expectedUnchanged"] and observed["actualUnchanged"]
    diagnostic_path = Path(str(report) + ".mismatch.json")
    if mismatch is None:
        assert observed["message"] is None
        assert not diagnostic_path.exists()
        return
    assert "host setup changed unrelated" in observed["message"]
    assert "target=D:\\" in observed["message"]
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8-sig"))
    assert diagnostic["field"] == mismatch
    assert diagnostic["expected"]["control"] == expected_control
    assert diagnostic["actual"]["control"] == actual_control
    assert diagnostic["expectedControl"]["hex"] == f"0x{expected_control:04X}"
    assert diagnostic["actualControl"]["hex"] == f"0x{actual_control:04X}"
    assert diagnostic["changedControl"]["value"] == expected_control ^ actual_control
    assert diagnostic["target"] == "D:\\"
    if expected_control == 0x8004 and actual_control == 0x8404:
        assert diagnostic["changedControl"]["hex"] == "0x0400"
        assert "DiscretionaryAclAutoInherited" in diagnostic["changedControl"]["flags"]
        assert "0x8004" in observed["message"] and "0x8404" in observed["message"]
