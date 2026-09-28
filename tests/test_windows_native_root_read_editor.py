from dataclasses import replace

from supervisor.config_editor import parameter_defs
from supervisor.project_config import ProjectConfig, changed_project_config_fields, load_project_config, save_project_config


def test_windows_root_read_is_discoverable_explicit_opt_in(tmp_path):
    config = ProjectConfig()
    parameter = next(item for item in parameter_defs(config) if item.key == "windows_native_root_read")
    assert parameter.label == "Windows native root read"
    assert parameter.value == "false"
    assert "Writes stay scoped" in parameter.help_text
    assert "Off by default" in parameter.help_text
    assert "other platforms and engines" in parameter.help_text
    assert {(option.field, option.value) for option in parameter.options} == {
        ("windows_native_root_read", True), ("windows_native_root_read", False)}
    selected = replace(config, windows_native_root_read=True)
    assert changed_project_config_fields(config, selected) == ("windows_native_root_read",)
    save_project_config(tmp_path, selected)
    assert load_project_config(tmp_path, create=False).windows_native_root_read is True
