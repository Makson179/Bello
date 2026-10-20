import pytest

from scripts.verify_claude_context import FIXTURE_SUMMARY, FixtureState, fixture_environment, system_text
from supervisor.runtime.claude_context import SUMMARY_SYSTEM, inspect_handoff


def test_local_fixture_separates_summary_and_main_requests():
    state = FixtureState()
    summary = state.answer({"system": SUMMARY_SYSTEM})
    assert summary["type"] == "text"
    assert state.summary_requests == 1 and state.main_requests == 0
    first = state.answer({})
    second = state.answer({})
    assert first["input"] == {"path": "first"}
    assert second["input"] == {"path": "second"}
    final = state.answer({"tools": [{"name": "StructuredOutput"}],
                          "messages": [{"role": "user", "content": "fixture-second-complete"}]})
    assert final["input"] == {"status": "done"}
    assert state.main_requests == 3 and state.structured_requests == 1
    assert state.tail_preserved and state.old_content_absent


@pytest.mark.parametrize("system", [
    SUMMARY_SYSTEM,
    [{"type": "text", "text": "billing attribution"}, {"type": "text", "text": SUMMARY_SYSTEM}],
], ids=["string", "content-blocks"])
def test_fixture_recognizes_the_multiline_summary_prompt_in_any_system_shape(system):
    assert SUMMARY_SYSTEM in system_text({"system": system})
    state = FixtureState()
    assert state.answer({"system": system})["text"] == FIXTURE_SUMMARY
    assert state.summary_requests == 1 and state.main_requests == 0


def test_fixture_does_not_mistake_the_role_prompt_for_a_summary_request():
    state = FixtureState()
    assert state.answer({"system": "You are a coding agent running inside Bello."})["type"] == "tool_use"
    assert state.summary_requests == 0 and state.main_requests == 1
    assert system_text({"system": None}) == "" and system_text({}) == ""


def test_fixture_summary_is_a_plain_text_handoff_not_a_json_envelope():
    state = FixtureState()
    text = state.answer({"system": SUMMARY_SYSTEM})["text"]
    assert text == FIXTURE_SUMMARY and not text.lstrip().startswith(("{", "```"))
    # The production validator accepts it exactly as written (newline, quotes and braces included).
    assert inspect_handoff(text) == (text, "plain_text")
    assert "\n" in text and '"' in text and "{" in text


def test_fixture_uses_isolated_home_and_loopback_synthetic_auth(tmp_path):
    env = fixture_environment(tmp_path, 12345)
    assert env["HOME"] == str(tmp_path / "home")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "config")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:12345"
    assert env["ANTHROPIC_API_KEY"] == "local-fixture-not-a-credential"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] == env["ALL_PROXY"] == ""
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
