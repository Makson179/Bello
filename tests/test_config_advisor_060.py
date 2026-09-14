"""Portable advisor helper tests: no provider requests, authentication, or runs."""

from __future__ import annotations

import copy
import importlib.util
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SKILL = Path(__file__).resolve().parents[1] / "plugins/bello/skills/bello-config-advisor"


def module(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


MODELS = module("inspect_models")
VALIDATOR = module("validate_config")
INSPECTOR = module("inspect_config")


def entry(identity, efforts, *, fast=False, **extra):
    provider, model_id = identity.split("/", 1)
    return dict(qualifiedId=identity, provider=provider, model=model_id,
                supportedEfforts=efforts, supportsServiceTier=fast,
                supportedServiceTiers=["priority"] if fast else [], configured=True, **extra)


SOL = "openai-codex/gpt-5.6-sol"
CLAUDE = "claude-code/sonnet"
QWEN = "openrouter/qwen/Qwen3-Coder"
CATALOG = {"models": [entry(SOL, ["high", "xhigh"], fast=True),
                       entry(CLAUDE, ["high", "max"]), entry(QWEN, ["off"])]}


def policy():
    return dict(enabled=False, max_concurrent=2,
                default={"model": QWEN, "intelligence": "off"}, allowed={QWEN: ["off"]})


def config():
    result = dict(review_limit_format="explicit", task="TASK.md", speed="usual",
                  revision_coder_enabled=False, runtime_enabled=True, cheap_runtime=False,
                  log_distiller={"enabled": False, "model_path": None}, start_over=False,
                  completion_review=True, adversary=True, max_adversary_runs=1,
                  max_completion_returns_before_adversary=1,
                  max_completion_returns_after_adversary=1, clean=False, protected_path=[])
    for role in ("coder", "revision_coder", "runtime", "completion", "adversary"):
        result[f"{role}_mod"] = CLAUDE if role == "adversary" else SOL
        result[f"{role}_intelligence"] = "high"
    for field in ("multi_agent", "completion_multi_agent", "adversary_multi_agent"):
        result[field] = policy()
    return result


def validate(value, **kwargs):
    options = dict(catalog=CATALOG, allow_clean=False, allow_unlimited=False)
    options.update(kwargs)
    return VALIDATOR.validate(value, **options)


class CatalogTests(unittest.TestCase):
    def test_real_catalog_shape_dedupes_alias_rows_and_preserves_capabilities(self):
        original = entry(QWEN, ["off"], cost={"input": 0.1}, description="Example")
        duplicate = {**original, "id": QWEN}
        result = MODELS.summarize_catalog({"data": [original, duplicate, CATALOG["models"][0]]})
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["cost"], {"input": 0.1})
        self.assertEqual(result[0]["model"], "qwen/Qwen3-Coder")
        self.assertEqual(result[0]["supportedEfforts"], ["off"])
        self.assertEqual(result[1]["supportedServiceTiers"], ["priority"])

    def test_omitted_price_is_not_invented(self):
        self.assertNotIn("cost", MODELS.summarize_catalog(CATALOG)[0])

    def test_empty_configured_catalog_is_not_replaced_by_static_models(self):
        self.assertEqual(MODELS.summarize_catalog({"data": []}), [])
        self.assertTrue(validate(config(), catalog={"models": []}))

    def test_conflicting_catalog_capabilities_are_rejected(self):
        first = CATALOG["models"][0]
        with self.assertRaises(ValueError):
            MODELS.summarize_catalog({"data": [first, {**first, "supportedEfforts": ["off"]}]})

    def test_catalog_identity_and_efforts_required(self):
        for value in ({}, {"data": [dict(qualifiedId=SOL)]},
                      {"data": [{**CATALOG["models"][0], "provider": "openai"}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODELS.summarize_catalog(value)

    def test_read_only_command_and_no_auth_error_bodies(self):
        payload = {"data": CATALOG["models"], "accounts": "private",
                   "unavailableEngines": {"claude-code": "private error body"}}
        with mock.patch.object(MODELS.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(payload), "")) as run:
            result = MODELS.load_catalog(timeout_seconds=5)
        self.assertEqual(run.call_args.args[0], ["bello", "runtime", "models", "--engine", "all"])
        self.assertEqual(result["unavailableEngines"], ["claude-code"])
        self.assertNotIn("private", json.dumps(result))

    def test_failure_does_not_fall_back_or_expose_stderr(self):
        with mock.patch.object(MODELS.subprocess, "run", side_effect=subprocess.CalledProcessError(
                1, ["bello"], stderr="private")) as run:
            with self.assertRaisesRegex(RuntimeError, "exit 1") as error:
                MODELS.load_catalog(timeout_seconds=5)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("private", str(error.exception))


class AdvisorValidationTests(unittest.TestCase):
    def test_mixed_c_a_and_children_pass(self):
        candidate = config()
        for field in ("multi_agent", "completion_multi_agent", "adversary_multi_agent"):
            candidate[field]["enabled"] = True
        self.assertEqual(validate(candidate), [])

    def test_runtime_c_a_and_combined_schedules_pass(self):
        for completion, adversary, before, after, runs in (
            (False, False, 0, 0, 0), (True, False, 1, 0, 0),
            (False, True, 0, 0, 1), (True, True, 0, 0, 1), (True, True, 2, 1, 2),
        ):
            candidate = config()
            candidate.update(completion_review=completion, adversary=adversary,
                             max_completion_returns_before_adversary=before,
                             max_completion_returns_after_adversary=after,
                             max_adversary_runs=runs)
            self.assertEqual(validate(candidate), [])

    def test_no_final_review_with_revision_or_nonzero_budget_rejected(self):
        candidate = config()
        candidate.update(completion_review=False, adversary=False, max_adversary_runs=0,
                         max_completion_returns_before_adversary=0,
                         max_completion_returns_after_adversary=0, revision_coder_enabled=True)
        self.assertTrue(any("no final review" in error for error in validate(candidate)))
        candidate["revision_coder_enabled"] = False
        candidate["max_completion_returns_before_adversary"] = 1
        self.assertTrue(any("both completion return budgets=0" in error for error in validate(candidate)))

    def test_all_four_switches_are_independent(self):
        for runtime, completion, adversary, distiller in itertools.product((False, True), repeat=4):
            candidate = config()
            candidate.update(runtime_enabled=runtime, completion_review=completion, adversary=adversary,
                             max_completion_returns_before_adversary=int(completion),
                             max_completion_returns_after_adversary=0, max_adversary_runs=int(adversary),
                             log_distiller={"enabled": distiller, "model_path": str(Path("supplied/bundle").absolute())})
            with self.subTest(runtime=runtime, completion=completion, adversary=adversary, distiller=distiller):
                with mock.patch("supervisor.runtime.distiller_bundle.validate_bundle", return_value={}) as check:
                    self.assertEqual(validate(candidate), [])
                    self.assertEqual(check.call_count, int(distiller))

    def test_runtime_off_has_no_runtime_or_triage_catalog_requirement(self):
        candidate = config()
        candidate.update(runtime_enabled=False, runtime_mod="unconfigured/runtime")
        self.assertEqual(validate(candidate), [])
        candidate["cheap_runtime"] = True
        errors = validate(candidate)
        self.assertTrue(any("must be false when runtime_enabled=false" in error for error in errors))
        self.assertFalse(any("unavailable" in error for error in errors))

    def test_a_only_uses_adversary_and_revision_profiles_not_completion(self):
        candidate = config()
        candidate.update(runtime_enabled=False, completion_review=False,
                         max_completion_returns_before_adversary=0,
                         max_completion_returns_after_adversary=0,
                         completion_mod="unconfigured/completion", revision_coder_enabled=True)
        self.assertEqual(validate(candidate), [])
        candidate["adversary_intelligence"] = "xhigh"
        self.assertTrue(any("adversary_mod/adversary_intelligence" in error for error in validate(candidate)))
        candidate["adversary_intelligence"] = "high"
        candidate["revision_coder_mod"] = "unconfigured/revision"
        self.assertTrue(any("revision_coder_mod" in error for error in validate(candidate)))

    def test_a_only_validates_its_own_children(self):
        candidate = config()
        candidate.update(completion_review=False, max_completion_returns_before_adversary=0,
                         max_completion_returns_after_adversary=0)
        for field in ("completion_multi_agent", "adversary_multi_agent"):
            candidate[field] = dict(enabled=True, max_concurrent=1,
                                   default={"model": "unconfigured/child", "intelligence": "high"},
                                   allowed={"unconfigured/child": ["high"]})
        errors = validate(candidate)
        self.assertTrue(any("adversary_multi_agent" in error for error in errors))
        self.assertFalse(any("completion_multi_agent" in error for error in errors))

    def test_complete_recommendation_requires_new_root_and_nested_fields(self):
        for field in ("runtime_enabled", "log_distiller"):
            candidate = config()
            del candidate[field]
            self.assertTrue(any("missing keys" in error and field in error for error in validate(candidate)))
        for field in ("enabled", "model_path"):
            candidate = config()
            del candidate["log_distiller"][field]
            self.assertTrue(any("expected exactly enabled and model_path" in error for error in validate(candidate)))

    def test_distiller_config_shape_and_explicit_local_bundle_are_checked(self):
        for value in (None, True, {}, {"enabled": "yes", "model_path": None},
                      {"enabled": False, "model_path": []},
                      {"enabled": False, "model_path": ""},
                      {"enabled": False, "model_path": "bad\x00path"},
                      {"enabled": False, "model_path": None, "timeout": 30}):
            candidate = config()
            candidate["log_distiller"] = value
            self.assertTrue(any("log_distiller" in error for error in validate(candidate)))
        candidate = config()
        candidate["log_distiller"] = {"enabled": True, "model_path": "relative/bundle"}
        self.assertTrue(any("requires --project-root" in error for error in validate(candidate)))
        with tempfile.TemporaryDirectory() as name:
            self.assertTrue(any("unavailable or incompatible" in error for error in validate(candidate, project_root=Path(name))))

    def test_default_distiller_advice_does_not_resolve_or_download_model(self):
        candidate = config()
        candidate["log_distiller"] = {"enabled": True, "model_path": None}
        with mock.patch.dict(sys.modules, {
            "huggingface_hub": None,
            "supervisor.runtime.distiller_download": None,
            "supervisor.runtime.distiller_bundle": None,
        }):
            self.assertEqual(validate(candidate), [])

    def test_distiller_bundle_check_is_local_metadata_only(self):
        from supervisor.runtime import distiller_bundle
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bundle = root / "bundle"
            bundle.mkdir()
            for asset in distiller_bundle.REQUIRED_ASSETS:
                (bundle / asset).write_text("{}")
            (bundle / "config.json").write_text(json.dumps(dict(
                model_type="modernbert", hidden_size=768, num_hidden_layers=22)))
            (bundle / "checkpoint.pt").write_text("not real weights; validation must not load this")
            manifest = dict(format=distiller_bundle.FORMAT, architecture=distiller_bundle.ARCHITECTURE,
                            checkpoint={"file": "checkpoint.pt", "format": "torch", "sha256": "0" * 64},
                            assets_sha256={asset: "0" * 64 for asset in distiller_bundle.REQUIRED_ASSETS},
                            recipe=dict(max_length=distiller_bundle.MAX_LENGTH, overlap=distiller_bundle.OVERLAP,
                                        renderer=distiller_bundle.RENDERER, cutoff=0))
            (bundle / "manifest.json").write_text(json.dumps(manifest))
            candidate = config()
            candidate["log_distiller"] = {"enabled": True, "model_path": "bundle"}
            with mock.patch.object(distiller_bundle, "sha256", side_effect=AssertionError("no weight hashing")):
                self.assertEqual(validate(candidate, project_root=root), [])
            manifest["architecture"] = "incompatible"
            (bundle / "manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(any("incompatible" in error for error in validate(candidate, project_root=root)))

    def test_enabled_distiller_requires_bello_python_but_disabled_path_is_dormant(self):
        candidate = config()
        candidate["log_distiller"]["model_path"] = str(Path("unavailable/bundle").absolute())
        with mock.patch.dict(sys.modules, {"supervisor.runtime.distiller_bundle": None}):
            self.assertEqual(validate(candidate), [])
            candidate["log_distiller"]["enabled"] = True
            self.assertTrue(any("Bello's Python environment" in error for error in validate(candidate)))

    def test_no_catalog_no_recommendation_validation(self):
        self.assertTrue(validate(config(), catalog=None))

    def test_provider_identity_never_switched(self):
        for identity in ("gpt-5.6-sol", "openai/gpt-5.6-sol", "openrouter/unknown/model"):
            candidate = config()
            candidate["coder_mod"] = identity
            self.assertTrue(validate(candidate))

    def test_active_effort_validated_for_exact_provider(self):
        candidate = config()
        candidate["adversary_intelligence"] = "xhigh"
        self.assertTrue(any("not advertised" in error for error in validate(candidate)))

    def test_disabled_profile_needs_syntax_but_not_auth(self):
        candidate = config()
        candidate["revision_coder_mod"] = "other-provider/model"
        candidate["revision_coder_intelligence"] = "minimal"
        self.assertEqual(validate(candidate), [])
        candidate["revision_coder_enabled"] = True
        self.assertTrue(validate(candidate))

    def test_fast_requires_each_active_profile_and_child(self):
        candidate = config()
        candidate["speed"] = "fast"
        self.assertTrue(validate(candidate))  # Claude has no priority tier.
        candidate["adversary_mod"] = SOL
        self.assertEqual(validate(candidate), [])
        for field in ("multi_agent", "completion_multi_agent", "adversary_multi_agent"):
            child = copy.deepcopy(candidate)
            child[field]["enabled"] = True
            self.assertTrue(any(field in error for error in validate(child)))

    def test_each_child_pool_and_default_are_validated(self):
        for field in ("multi_agent", "completion_multi_agent", "adversary_multi_agent"):
            candidate = config()
            candidate[field]["default"]["intelligence"] = "high"
            self.assertTrue(any(field in error for error in validate(candidate)))

    def test_cheap_runtime_does_not_invent_an_openai_subscription(self):
        candidate = config()
        candidate["cheap_runtime"] = True
        self.assertTrue(any("cheap_runtime" in error for error in validate(candidate)))
        self.assertEqual(validate(candidate, triage_model=SOL), [])

    def test_unavailable_or_unconfigured_model_rejected(self):
        for flag in ("configured", "available"):
            catalog = copy.deepcopy(CATALOG)
            catalog["models"][0][flag] = False
            self.assertTrue(validate(config(), catalog=catalog))

    def test_clean_unlimited_and_unknown_fields_preserve_safety(self):
        for key, value, allow in (("clean", True, "allow_clean"),
                                 ("max_completion_returns_before_adversary", "unlimited", "allow_unlimited")):
            candidate = config()
            candidate[key] = value
            self.assertTrue(validate(candidate))
            self.assertEqual(validate(candidate, **{allow: True}), [])
        candidate = config()
        candidate["plan"] = "PLAN.md"
        self.assertTrue(validate(candidate))

    def test_malformed_profile_speed_or_capabilities_report_errors(self):
        for field, value in (("coder_mod", []), ("coder_intelligence", {}), ("speed", [])):
            candidate = config()
            candidate[field] = value
            self.assertTrue(validate(candidate))
        malformed = copy.deepcopy(CATALOG)
        malformed["models"][0]["supportedServiceTiers"] = None
        self.assertTrue(validate(config(), catalog=malformed))

    def test_task_path_remains_canonical_and_exact(self):
        for task in ("/TASK.md", "C:task.md", "C:/task.md", "../TASK.md", "./TASK.md",
                     "tasks//TASK.md", "tasks\\TASK.md", " TASK.md", "."):
            candidate = config()
            candidate["task"] = task
            self.assertTrue(validate(candidate))
        self.assertTrue(validate(config(), expected_task="OTHER.md"))
        self.assertEqual(validate(config(), expected_task="TASK.md"), [])

    def test_resolved_task_is_inside_root(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            task = root / "TASK.md"
            task.write_text("task")
            self.assertEqual(VALIDATOR._resolve_expected_task(root, task), "TASK.md")
            (root / "sub").mkdir()
            with self.assertRaises(ValueError):
                VALIDATOR._resolve_expected_task(root / "sub", task)

    def test_cli_normalize_then_validate_no_model_request(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            raw = root / "catalog.json"
            raw.write_text(json.dumps({"data": CATALOG["models"]}))
            command = [sys.executable, str(SKILL / "scripts/inspect_models.py"), "--file", str(raw)]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            raw.write_text(result.stdout)
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps(config()))
            (root / "TASK.md").write_text("Task fixture")
            command = [sys.executable, str(SKILL / "scripts/validate_config.py"), "--file", str(candidate),
                       "--catalog", str(raw), "--project-root", str(root), "--task-file", "TASK.md"]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "valid")
            invalid = config()
            invalid["coder_intelligence"] = "off"
            candidate.write_text(json.dumps(invalid))
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)


class ConfigInspectionTests(unittest.TestCase):
    def test_inspector_reports_invalid_reviewer_multi_agent_source_by_role(self):
        for field in ("completion_multi_agent", "adversary_multi_agent"):
            with tempfile.TemporaryDirectory() as name:
                root = Path(name)
                path = root / ".supervisor/config.json"
                path.parent.mkdir()
                path.write_text(json.dumps({"review_limit_format": "explicit", field: {"enabled": "yes"}}))
                result = INSPECTOR.inspect(root, include_bello_version=False, timeout_seconds=1)
            with self.subTest(field=field):
                self.assertFalse(result["source_config_valid"])
                self.assertTrue(any(error.startswith(field) for error in result["source_config_errors"]))

    def test_absent_config_uses_current_defaults_not_legacy_budgets(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            result = INSPECTOR.inspect(root, include_bello_version=False, timeout_seconds=1)
            self.assertFalse((root / ".supervisor").exists())
        current = result["current_project_config"]
        self.assertFalse(result["config_exists"])
        self.assertEqual(result["apply_guard"], "clear")
        self.assertEqual(current["max_completion_returns_before_adversary"], 1)
        self.assertEqual(current["max_completion_returns_after_adversary"], 0)
        self.assertTrue(current["runtime_enabled"])
        self.assertEqual(current["log_distiller"], {"enabled": False, "model_path": None})

    def test_sparse_explicit_config_uses_current_review_defaults(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / ".supervisor/config.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"review_limit_format": "explicit"}))
            result = INSPECTOR.inspect(root, include_bello_version=False, timeout_seconds=1)
        current = result["current_project_config"]
        self.assertEqual(current["max_completion_returns_before_adversary"], 1)
        self.assertEqual(current["max_completion_returns_after_adversary"], 0)

    def test_inspector_surfaces_invalid_source_config(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / ".supervisor/config.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"review_limit_format": "future", "multi_agent": [],
                                        "fast": "yes", "adversary": "yes", "speed": []}))
            result = INSPECTOR.inspect(root, include_bello_version=False, timeout_seconds=1)
        self.assertFalse(result["source_config_valid"])
        for field in ("review_limit_format", "multi_agent", "adversary", "speed"):
            self.assertTrue(any(field in error for error in result["source_config_errors"]))

    def test_inspector_normalizes_legacy_choices_before_effort_validation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / ".supervisor/config.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"review_limit_format": "explicit",
                                        "coder_mod": " gpt-5.6-luna ", "coder_intelligence": " ULTRA "}))
            result = INSPECTOR.inspect(root, include_bello_version=False, timeout_seconds=1)
        self.assertEqual(result["current_project_config"]["coder_mod"], "gpt-5.6-luna")
        self.assertEqual(result["current_project_config"]["coder_intelligence"], "ultra")
        self.assertFalse(result["source_config_valid"])

    def test_pid_identity_requires_bello_command_and_matching_workspace(self):
        root = Path.cwd().resolve()
        with (
            mock.patch.object(INSPECTOR, "_read_process_command", return_value="/venv/bin/python /venv/bin/bello"),
            mock.patch.object(INSPECTOR, "_read_process_cwd", return_value=root),
        ):
            self.assertEqual(INSPECTOR._pid_identity(123, root), "confirmed")
        with mock.patch.object(INSPECTOR, "_read_process_command", return_value="python tests.py"):
            self.assertEqual(INSPECTOR._pid_identity(123, root), "mismatch")
        with (
            mock.patch.object(INSPECTOR, "_read_process_command", return_value='"C:\\venv\\bello.exe" --task T'),
            mock.patch.object(INSPECTOR, "_read_process_cwd", return_value=None),
        ):
            self.assertEqual(INSPECTOR._pid_identity(123, root), "probable")
        with (
            mock.patch.object(INSPECTOR, "_read_process_command", return_value="/venv/bin/bello"),
            mock.patch.object(INSPECTOR, "_read_process_cwd", return_value=root.parent),
        ):
            self.assertEqual(INSPECTOR._pid_identity(123, root), "mismatch")

    def test_confirmed_live_identity_blocks_apply_despite_saved_terminal_status(self):
        guard, _ = INSPECTOR._apply_guard(config_exists=True, status="complete", config_path=Path("unused"),
                                         liveness="alive", identity="confirmed")
        self.assertEqual(guard, "blocked")

    def test_new_provider_profiles_roundtrip_without_lowercasing_model(self):
        payload = config()
        payload.update(coder_mod=QWEN, coder_intelligence="off")
        current = INSPECTOR._normalize(payload, config_exists=True)
        self.assertEqual(current, payload)
        self.assertEqual(INSPECTOR._source_config_errors(payload, current, config_exists=True), [])

    def test_sparse_revision_inherits_and_dormant_budgets_preserved(self):
        payload = dict(coder_mod=CLAUDE, coder_intelligence="max", completion_review=False,
                       review_limit_format="explicit", max_completion_returns_before_adversary=3)
        result = INSPECTOR._normalize(payload, config_exists=True)
        self.assertEqual(result["revision_coder_mod"], CLAUDE)
        self.assertEqual(result["revision_coder_intelligence"], "max")
        self.assertEqual(result["max_completion_returns_before_adversary"], 3)

    def test_inspector_preserves_independent_flags_and_dormant_source_values(self):
        payload = config()
        payload.update(runtime_enabled=False, completion_review=False, cheap_runtime=True,
                       log_distiller={"enabled": False, "model_path": "/not/on/this/host"})
        current = INSPECTOR._normalize(payload, config_exists=True)
        self.assertEqual(current, payload)
        self.assertEqual(INSPECTOR._source_config_errors(payload, current, config_exists=True), [])
        self.assertTrue(current["adversary"])

    def test_inspector_normalizes_sparse_distiller_without_hiding_source_errors(self):
        payload = {"log_distiller": {"enabled": True}}
        current = INSPECTOR._normalize(payload, config_exists=True)
        self.assertEqual(current["log_distiller"], {"enabled": True, "model_path": None})
        for value in ([], None, {"enabled": "yes"}, {"model_path": []}, {"unknown": 1}):
            payload = {"log_distiller": value}
            current = INSPECTOR._normalize(payload, config_exists=True)
            self.assertTrue(any("log_distiller" in error for error in
                                INSPECTOR._source_config_errors(payload, current, config_exists=True)))
        payload = {"runtime_enabled": "false"}
        current = INSPECTOR._normalize(payload, config_exists=True)
        self.assertIn("runtime_enabled must be boolean",
                      INSPECTOR._source_config_errors(payload, current, config_exists=True))

    def test_explicit_and_legacy_zero_are_distinct(self):
        self.assertEqual(INSPECTOR._normalized_review_limits({"review_limit_format": "explicit",
            "max_completion_returns_before_adversary": 0, "max_completion_returns_after_adversary": 0}), (0, 0))
        self.assertEqual(INSPECTOR._normalized_review_limits({
            "max_completion_returns_before_adversary": 0, "max_completion_returns_after_adversary": 0}),
            ("unlimited", "unlimited"))

    def test_version_target_recognizes_060_development(self):
        for value in ("0.6.0", "0.6.0.dev0", "0.6.0.dev12"):
            self.assertEqual(INSPECTOR._version_compatibility(value), "verified")
        self.assertEqual(INSPECTOR._version_compatibility("0.5.2"), "update_required")
        self.assertEqual(INSPECTOR._version_compatibility("garbage"), "unverified")

    def test_live_process_guard_and_state_are_preserved_read_only(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / ".supervisor/config.json"
            path.parent.mkdir()
            payload = {**config(), "status": "running", "runtime_protocol_version": 1,
                       "runtime_sessions": {"unchanged": True}}
            original = json.dumps(payload)
            path.write_text(original)
            with mock.patch.object(INSPECTOR, "_process_observation", return_value={
                    "liveness": "alive", "identity": "confirmed", "pid": 123}):
                result = INSPECTOR.inspect(root, include_bello_version=False, timeout_seconds=1)
            self.assertEqual(result["apply_guard"], "blocked")
            self.assertEqual(result["runtime_status"], "running")
            self.assertFalse(result["model_capabilities_checked"])
            self.assertEqual(path.read_text(), original)

    def test_reused_pid_does_not_permanently_block_completed_workspace(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / ".supervisor/config.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"review_limit_format": "explicit", "status": "complete"}))
            run_dir = root / ".codex/bello-run"
            run_dir.mkdir(parents=True)
            (run_dir / "pid").write_text(str(os.getpid()))
            with mock.patch.object(INSPECTOR, "_pid_identity", return_value="mismatch"):
                recent = INSPECTOR.inspect(root, include_bello_version=False, timeout_seconds=1)
            old = time.time() - 10
            os.utime(path, (old, old))
            with mock.patch.object(INSPECTOR, "_pid_identity", return_value="mismatch"):
                settled = INSPECTOR.inspect(root, include_bello_version=False, timeout_seconds=1)
        self.assertEqual(recent["process_observation"]["liveness"], "alive")
        self.assertEqual(recent["process_observation"]["identity"], "mismatch")
        self.assertEqual(recent["apply_guard"], "uncertain")
        self.assertEqual(settled["apply_guard"], "clear")


if __name__ == "__main__":
    unittest.main()
