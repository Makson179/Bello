"""Regression coverage for the editor's extracted dependency and state boundaries."""
from __future__ import annotations

import json
import pickle
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from supervisor import config_editor as editor
from supervisor.config_editor import (
    EditorParameter,
    EditorState,
    ModelChoices,
    Theme,
    WidthUtils,
    _replace_config_field,
    _save_config_change,
    append_inline_text,
    available_model_choices,
    parameter_defs,
    render_editor,
    run_config_editor,
)
from supervisor.config_validation import (
    CatalogModel,
    ConfigIssue,
    ConfigReport,
    ModelCatalog,
)
from supervisor.project_config import (
    ProjectConfig,
    load_project_config,
    project_config_path,
    save_project_config,
)
from supervisor.runtime.models import NO_EFFORT


def test_public_state_contract_and_legacy_pickle_paths() -> None:
    state = EditorState(editing=True, edit_value="task.txt")
    assert pickle.loads(pickle.dumps(state)) == state
    assert pickle.loads(b"csupervisor.config_editor\nEditorState\n.") is EditorState
    assert pickle.loads(b"csupervisor.config_editor\nModelChoices\n.") is ModelChoices
    with pytest.raises(FrozenInstanceError):
        state.edit_value = "changed"
    changed = append_inline_text(state, "!")
    assert changed.edit_value == "task.txt!"
    assert state.edit_value == "task.txt"


def test_live_refresh_owns_replaced_maps_and_preserves_prior_catalog(tmp_path: Path, monkeypatch) -> None:
    efforts = {"stale": ("ultra",)}
    scratch = {"response": {"stale": True}}
    monkeypatch.setattr(editor, "_model_effort_catalog", efforts)
    monkeypatch.setattr(editor, "_discovery", scratch)
    calls = []
    fail = False

    class Client:
        def __init__(self, *, cwd):
            assert cwd == tmp_path

        async def start(self):
            calls.append("start")

        async def initialize(self):
            calls.append("initialize")

        async def request(self, method, params):
            calls.append((method, params))
            if fail:
                raise RuntimeError("sensitive provider payload must not reach the UI")
            return {"data": [{
                "id": "gpt-5.6-sol", "qualifiedId": "openai/gpt-5.6-sol", "provider": "openai",
                "supportedEfforts": [],
            }]}

        async def stop(self):
            calls.append("stop")

    def no_cache():
        pytest.fail("live discovery must not fall back to disk cache")

    monkeypatch.setattr(editor, "RuntimeClient", Client)
    monkeypatch.setattr(editor, "_available_models_from_cache", no_cache)
    choices = available_model_choices(tmp_path)
    assert choices == ("openai/gpt-5.6-sol",)
    assert efforts == {"openai/gpt-5.6-sol": ()}
    assert editor.intelligence_choices_for_model(choices[0]) == (NO_EFFORT,)
    assert scratch == {}
    assert choices.catalog is not None
    assert choices.catalog.get(choices[0]).efforts == ()
    fail = True
    failed = available_model_choices(tmp_path)
    assert failed == ()
    assert failed.catalog.discovery_error == "RuntimeError"
    assert "sensitive" not in repr(failed.catalog)
    assert efforts == scratch == {}
    assert choices.catalog.get(choices[0]).efforts == ()
    lifecycle = ["start", "initialize", ("model/list", {
        "engines": ["codex", "pi", "claude-code"], "optionalEngines": True,
    }), "stop"]
    assert calls == lifecycle * 2


def test_substituted_discovery_clears_scratch_even_on_exception(tmp_path: Path, monkeypatch) -> None:
    scratch = {}
    monkeypatch.setattr(editor, "_discovery", scratch)
    monkeypatch.setattr(editor, "_model_effort_catalog", {})

    def broken(root):
        scratch["response"] = {"sensitive": True}
        raise ValueError("substituted discovery failure")

    monkeypatch.setattr(editor, "_available_models_from_app_server", broken)
    with pytest.raises(ValueError, match="substituted discovery failure"):
        available_model_choices(tmp_path)
    assert scratch == {}


def test_rows_and_renderer_observe_late_validation_override(monkeypatch) -> None:
    report = ConfigReport((ConfigIssue(
        "error", "availability", "Access marker", "Reconnect engine.", ("coder_mod",),
    ),), verified=True)
    catalog = ModelCatalog(discovered=True)
    choices = ModelChoices((), catalog)
    config = ProjectConfig(coder_mod="gpt-5.6-sol")
    calls = []

    def validate(current, current_catalog):
        calls.append((current, current_catalog))
        return report

    monkeypatch.setattr(editor, "validate_project_config", validate)
    rows = parameter_defs(config, choices)
    index = next(index for index, row in enumerate(rows) if row.key == "coder_mod")
    assert rows[index].issue == "Reconnect engine."
    output = render_editor(config, EditorState(parameter_index=index), Path("config.json"), choices,
                           width=150, height=40)
    assert "Access marker" in output
    assert "Reconnect engine." in output
    assert "Check model access" in output
    assert calls == [(config, catalog)] * 3


def test_model_update_observes_effort_hooks_and_keeps_provider_route(monkeypatch) -> None:
    config = ProjectConfig(coder_mod="gpt-5.6-sol", coder_intelligence="ultra")
    model = "openai/gpt-5.6-sol"
    catalog = ModelCatalog(models={model: CatalogModel(model, "pi", ("low", "high"), default_effort="low")})
    monkeypatch.setattr(editor, "intelligence_choices_for_model", lambda selected: ("low", "high"))
    calls = []

    def choose(current, supported, *, advertised_default):
        calls.append((current, supported, advertised_default))
        return "high"

    monkeypatch.setattr(editor, "choose_effort", choose)
    updated = _replace_config_field(config, "coder_mod", model, catalog=catalog)
    assert updated.coder_mod == model
    assert updated.coder_intelligence == "high"
    assert updated.runtime_mod == config.runtime_mod
    assert config.coder_mod == "gpt-5.6-sol" and config.coder_intelligence == "ultra"
    assert calls == [("ultra", ("low", "high"), "low")]


def test_terminal_and_fragment_helpers_observe_late_overrides(monkeypatch) -> None:
    monkeypatch.delenv("BELLO_CONFIG_ASCII", raising=False)
    monkeypatch.setattr(editor, "_terminal_supports_unicode", lambda: False)
    assert Theme.from_environment().symbols.top_left == "+"
    monkeypatch.setattr(editor, "OUTER_BORDER_GRADIENT", ("#123456",))
    rendered = render_editor(ProjectConfig(), EditorState(), Path("config.json"),
                             width=80, height=14, formatted=True)
    assert any("#123456" in style for style, text in rendered)
    monkeypatch.setattr(editor, "wcwidth", lambda char: 2)
    assert WidthUtils.display_width("abc") == 6
    assert editor._fragment_width([("", "abc")]) == 6


def test_input_transition_observes_late_printable_hook(monkeypatch) -> None:
    state = EditorState(editing=True, edit_value="old", edit_error="invalid")
    monkeypatch.setattr(editor, "_printable_text", lambda value: False)
    assert append_inline_text(state, "new") is state
    monkeypatch.setattr(editor, "_printable_text", lambda value: True)
    assert append_inline_text(state, "new") == replace(state, edit_value="oldnew", edit_error=None)


def test_save_boundary_preserves_untouched_runtime_fields(tmp_path: Path, monkeypatch) -> None:
    original = ProjectConfig(task="old.md", coder_mod="gpt-5.6-sol", coder_intelligence="high")
    save_project_config(tmp_path, original)
    path = project_config_path(tmp_path)
    before = json.loads(path.read_text())
    calls = []
    sync = editor.sync_runtime_config_fields

    def persist(root, current, fields):
        calls.append((root, current, fields))
        sync(root, current, fields)

    monkeypatch.setattr(editor, "sync_runtime_config_fields", persist)
    _save_config_change(tmp_path, original, original)
    assert calls == []
    updated = replace(original, task="new.md")
    _save_config_change(tmp_path, original, updated)
    assert calls == [(tmp_path, updated, ("task",))]
    after = json.loads(path.read_text())
    assert {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)} == {"task", "task_path"}
    assert load_project_config(tmp_path).task == "new.md"
    assert load_project_config(tmp_path).coder_mod == "gpt-5.6-sol"


@pytest.mark.parametrize("commit", [False, True], ids=["cancel", "commit"])
def test_interactive_session_owns_input_and_explicit_save(tmp_path: Path, monkeypatch, commit: bool) -> None:
    import prompt_toolkit
    from prompt_toolkit.keys import Keys

    config = ProjectConfig(task="old")
    saves = []
    frames = []
    monkeypatch.setattr(editor, "sys", SimpleNamespace(
        stdin=SimpleNamespace(isatty=lambda: True), stdout=SimpleNamespace(isatty=lambda: True),
    ))
    monkeypatch.setattr(editor, "load_project_config", lambda root, create: config)
    monkeypatch.setattr(editor, "available_model_choices", lambda root: ModelChoices())
    monkeypatch.setattr(editor, "_config_animations_enabled", lambda: False)
    monkeypatch.setattr(editor, "_prompt_toolkit_size", lambda get_app: (100, 30))
    monkeypatch.setattr(editor, "parameter_defs", lambda current, choices: (
        EditorParameter("task", "task", current.task, (), edit_kind="optional_text"),
    ))
    monkeypatch.setattr(editor, "sync_runtime_config_fields", lambda root, current, fields: saves.append((current, fields)))

    def render(current, state, path, choices, **kwargs):
        frames.append((current, state, kwargs["animation_frame"]))
        return [("", "editor")]

    monkeypatch.setattr(editor, "render_editor", render)

    class Application:
        def __init__(self, **kwargs):
            self.render = kwargs["layout"].container.content.text
            self.bindings = kwargs["key_bindings"].bindings
            self.exited = False
            assert kwargs["refresh_interval"] is None

        def invalidate(self):
            self.render()

        def exit(self):
            self.exited = True

        def press(self, key, data=""):
            binding = next(binding for binding in self.bindings if binding.keys == (key,))
            binding.handler(SimpleNamespace(app=self, data=data))

        def run(self):
            self.render()
            self.press(Keys.ControlM)
            self.press(Keys.Down)  # Navigation cannot leave an active text edit.
            for _ in "old":
                self.press(Keys.ControlH)
            self.press(Keys.Any, "new")
            assert frames[-1][1].edit_value == "new"
            assert saves == []
            self.press(Keys.ControlM if commit else Keys.Escape)
            self.press("q", "q")
            assert self.exited

    monkeypatch.setattr(prompt_toolkit, "Application", Application)
    result = run_config_editor(tmp_path)
    assert result.task == ("new" if commit else "old")
    assert saves == ([(result, ("task",))] if commit else [])
    assert frames[-1][1].editing is False
    assert frames[-1][1].notice == ("Saved to .supervisor/config.json." if commit else None)
    assert all(frame is None for current, state, frame in frames)
