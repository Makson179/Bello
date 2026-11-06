"""Offline configuration contracts for the opt-in Goal adversary checkpoint."""

from __future__ import annotations

import asyncio
import json
from contextlib import chdir
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from pydantic import ValidationError

import supervisor.main as main_module
from supervisor.config_editor import parameter_defs, select_current
from supervisor.config_editor_types import EditorState
from supervisor.main import _resolve_run_settings, cli
from supervisor.project_config import (
    ProjectConfig,
    ProjectConfigError,
    _config_from_payload,
    changed_project_config_fields,
    load_project_config,
    project_config_path,
    save_project_config,
    sync_runtime_config_fields,
)
from supervisor.schemas import BelloConfig, BelloStatus


FIELD = "goal_adversary_after_returns"


class GoalCheckpointConfigTests(unittest.TestCase):
    def test_old_configs_and_goal_defaults_keep_checkpoint_disabled(self):
        for payload in ({}, {"goal": False}, {"goal": True}):
            with self.subTest(payload=payload):
                project = _config_from_payload(payload, path=Path("fixture.json"))
                state = BelloConfig(project_root="fixture", task_path="TASK.md", **payload)
                settings = _resolve_run_settings(project_config=project)
                self.assertEqual(getattr(project, FIELD), 0)
                self.assertEqual(getattr(state, FIELD), 0)
                self.assertEqual(getattr(settings, FIELD), 0)
                self.assertFalse(settings.adversary)
                self.assertEqual(project.completion_returns_before_adversary, 1)
                self.assertEqual(project.completion_returns_after_adversary, 0)
                rows = {row.key: row for row in parameter_defs(project)}
                if project.goal:
                    self.assertEqual(rows[FIELD].value, "0")
                    self.assertEqual(rows["completion_returns_before_adversary"].value, "Unlimited (goal)")
                else:
                    self.assertNotIn(FIELD, rows)

    def test_checkpoint_requires_a_strict_non_negative_integer(self):
        for value in (True, False, -1, 1.0, "3", "unlimited", None, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ProjectConfigError, FIELD):
                    ProjectConfig(**{FIELD: value})
                with self.assertRaisesRegex(ProjectConfigError, FIELD):
                    _config_from_payload({FIELD: value}, path=Path("fixture.json"))
                with self.assertRaises(ValidationError):
                    BelloConfig(project_root="fixture", task_path="TASK.md", **{FIELD: value})
                if value is not None:  # None is the run-settings sentinel for no override.
                    with self.assertRaisesRegex(RuntimeError, FIELD):
                        _resolve_run_settings(project_config=ProjectConfig(), **{FIELD: value})

    def test_checkpoint_roundtrips_and_syncs_without_resetting_runtime_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for value in (0, 1, 3, 12):
                with self.subTest(value=value):
                    config = ProjectConfig(goal=True, adversary=True, adversary_runs=2,
                                           goal_adversary_after_returns=value)
                    save_project_config(root, config)
                    loaded = load_project_config(root, create=False)
                    self.assertEqual(loaded, config)
                    self.assertEqual(config.to_json_data()[FIELD], value)
            path = project_config_path(root)
            payload = json.loads(path.read_text())
            payload.update(generation=7, restart_count=2, coder_thread_id="existing-thread")
            path.write_text(json.dumps(payload))
            updated = replace(config, goal_adversary_after_returns=3)
            changed = changed_project_config_fields(config, updated)
            self.assertEqual(changed, (FIELD,))
            sync_runtime_config_fields(root, updated, changed)
            saved = json.loads(path.read_text())
            self.assertEqual(saved[FIELD], 3)
            self.assertEqual(saved["generation"], 7)
            self.assertEqual(saved["restart_count"], 2)
            self.assertEqual(saved["coder_thread_id"], "existing-thread")

    def test_positive_checkpoint_never_enables_goal_adversary_or_changes_pass_count(self):
        for goal in (False, True):
            for adversary in (False, True):
                for count in (0, 1, 2):
                    with self.subTest(goal=goal, adversary=adversary, count=count):
                        config = ProjectConfig(goal=goal, adversary=adversary, adversary_runs=count,
                                               goal_adversary_after_returns=3)
                        settings = _resolve_run_settings(project_config=config)
                        self.assertIs(settings.goal, goal)
                        self.assertIs(settings.adversary, adversary)
                        self.assertEqual(settings.adversary_runs, count)
                        self.assertEqual(settings.goal_adversary_after_returns, 3)

    def test_editor_explains_active_and_dormant_values_and_edits_only_checkpoint(self):
        for goal, adversary, count in ((False, True, 1), (True, False, 1), (True, True, 0), (True, True, 2)):
            with self.subTest(goal=goal, adversary=adversary, count=count):
                config = ProjectConfig(goal=goal, adversary=adversary, adversary_runs=count,
                                       goal_adversary_after_returns=3)
                parameters = parameter_defs(config)
                rows = {row.key: row for row in parameters}
                row = rows[FIELD]
                active = goal and adversary and count > 0
                self.assertEqual(row.value, "3" if active else "3 (inactive)")
                self.assertEqual(row.edit_kind, "non_negative_int")
                self.assertIn("Inactive unless goal and adversary", row.help_text)
                if active:
                    self.assertEqual(rows["completion_returns_before_adversary"].value, "3 (goal checkpoint)")
                    self.assertEqual(rows["completion_returns_after_adversary"].value, "Unlimited (goal)")
                state = EditorState(parameter_index=list(rows).index(FIELD))
                unchanged, editing, _ = select_current(config, state)
                self.assertEqual(unchanged, config)
                self.assertEqual(editing.edit_value, "3")
                for invalid in ("-1", "true", "3.5", "unlimited"):
                    rejected, error, _ = select_current(config, replace(editing, edit_value=invalid))
                    self.assertEqual(rejected, config)
                    self.assertIsNotNone(error.edit_error)
                updated, _, _ = select_current(config, replace(editing, edit_value="0"))
                self.assertEqual(updated, replace(config, goal_adversary_after_returns=0))

    def test_cli_preserves_saved_value_and_allows_explicit_zero(self):
        cases = (([], 3, True, True, 2),
                 (["--goal-adversary-after-returns", "0"], 0, True, True, 2),
                 (["--goal-adversary-after-returns", "5"], 5, True, True, 2),
                 (["--no-goal"], 3, False, True, 2),
                 (["--adversary", "false"], 3, True, False, 2),
                 (["--adversary-runs", "0"], 3, True, False, 0))
        runner = CliRunner()
        with TemporaryDirectory() as directory, chdir(directory):
            config = ProjectConfig(goal=True, adversary=True, adversary_runs=2,
                                   goal_adversary_after_returns=3)
            save_project_config(Path.cwd(), config)
            captured = []

            async def fake_run(settings):
                captured.append(settings)
                return 0

            with patch.object(main_module, "_startup_update_gate"), \
                    patch.object(main_module, "_run_bello", fake_run), \
                    patch.object(main_module, "_run_async_cleanly", asyncio.run):
                for args, expected, goal, adversary, count in cases:
                    with self.subTest(args=args):
                        result = runner.invoke(cli, args)
                        self.assertEqual(result.exit_code, 0, result.output)
                        settings = captured[-1]
                        self.assertEqual(settings.goal_adversary_after_returns, expected)
                        self.assertIs(settings.goal, goal)
                        self.assertIs(settings.adversary, adversary)
                        self.assertEqual(settings.adversary_runs, count)
                for value in ("-1", "true", "3.5", "unlimited"):
                    result = runner.invoke(cli, ["--goal-adversary-after-returns", value])
                    self.assertEqual(result.exit_code, 2, result.output)
            self.assertEqual(load_project_config(Path.cwd(), create=False), config)

    def test_cli_help_explains_checkpoint_and_run_passes_it_to_controller(self):
        help_text = " ".join(CliRunner().invoke(cli, ["--help"]).output.split())
        self.assertIn("--goal-adversary-after-returns", help_text)
        self.assertIn("0 disables the checkpoint", help_text)
        self.assertIn("omitted preserves the saved value", help_text)
        captured = {}

        class FakeController:
            def __init__(self, _root, **kwargs):
                captured.update(kwargs)
                self.store = SimpleNamespace(get_bello_config=lambda: SimpleNamespace(status=BelloStatus.COMPLETE))

            async def run(self):
                pass

        project = ProjectConfig(goal=True, adversary=True, goal_adversary_after_returns=3)
        settings = _resolve_run_settings(project_config=project)
        with patch.object(main_module, "BelloController", FakeController), \
                patch.object(main_module, "load_project_config", return_value=project):
            self.assertEqual(asyncio.run(main_module._run_bello(settings)), 0)
        self.assertEqual(captured[FIELD], 3)

    def test_config_output_identifies_active_and_dormant_checkpoint(self):
        runner = CliRunner()
        for goal, adversary, count, active in ((True, True, 1, True), (False, True, 1, False),
                                              (True, False, 1, False), (True, True, 0, False)):
            with self.subTest(goal=goal, adversary=adversary, count=count):
                config = ProjectConfig(goal=goal, adversary=adversary, adversary_runs=count,
                                       goal_adversary_after_returns=3)
                with TemporaryDirectory() as directory, chdir(directory), \
                        patch.object(main_module, "_startup_update_gate"), \
                        patch.object(main_module, "run_config_editor", return_value=config):
                    result = runner.invoke(cli, ["config"])
                self.assertEqual(result.exit_code, 0, result.output)
                expected = "goal-adversary-after-returns: 3"
                if not active:
                    expected += " (inactive: requires goal and adversary)"
                self.assertIn(expected, result.output.splitlines())


if __name__ == "__main__":
    unittest.main()
