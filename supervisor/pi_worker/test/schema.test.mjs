import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { realPiSdk } from "../src/pi-sdk.mjs";

const repositoryRoot = fileURLToPath(new URL("../../../", import.meta.url));
const python = [join(repositoryRoot, ".venv", "bin", "python"), join(repositoryRoot, ".venv", "Scripts", "python.exe")]
  .find(existsSync);

test("TypeBox validates Bello's real strict schemas including $defs/$ref", { skip: !python }, () => {
  const script = `
import json
from supervisor.schemas.models import (
    AdvReportControllerDecision,
    CheapRuntimeDecision,
    CompletionReviewDecision,
    SupervisorDecision,
    openai_strict_json_schema_for_adv_report_controller_decision,
    openai_strict_json_schema_for_cheap_runtime_decision,
    openai_strict_json_schema_for_completion_review_decision,
    openai_strict_json_schema_for_supervisor_decision,
)
cases = [
    (openai_strict_json_schema_for_supervisor_decision(), SupervisorDecision(
        decision="noop", approval_decision=None, execpolicy_amendment=None, reason="ok",
        message_to_coder=None, persistent_decision=None, progress_update=None,
        clear_handoff=False, display_message=None, handoff=None,
    ).model_dump(mode="json")),
    (openai_strict_json_schema_for_completion_review_decision(), CompletionReviewDecision(
        decision="accept", reason="ok", message_to_coder=None, persistent_decision=None,
        progress_update=None, clear_handoff=False, display_message=None, handoff=None,
        wake_sequence=1, generation=0,
    ).model_dump(mode="json")),
    (openai_strict_json_schema_for_adv_report_controller_decision(), AdvReportControllerDecision(
        forward_to_coder=False, reason="no finding", report_to_coder=None,
    ).model_dump(mode="json")),
    (openai_strict_json_schema_for_cheap_runtime_decision(), CheapRuntimeDecision(
        decision="noop", reason_code="routine_progress",
    ).model_dump(mode="json")),
]
print(json.dumps(cases))
`;
  const cases = JSON.parse(execFileSync(python, ["-c", script], { cwd: repositoryRoot, encoding: "utf8" }));
  assert.equal(cases.length, 4);
  assert.equal(Boolean(cases[1][0].$defs), true);
  for (const [schema, value] of cases) {
    assert.deepEqual(realPiSdk.validateSchema(schema, value), []);
    assert.notDeepEqual(realPiSdk.validateSchema(schema, { ...value, unexpected: true }), []);
  }
});
