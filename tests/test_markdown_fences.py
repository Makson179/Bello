from supervisor.markdown_fences import advance_markdown_fence


def test_opening_fence_allows_up_to_three_leading_spaces() -> None:
    state, is_marker = advance_markdown_fence("   ```python", None)

    assert state == ("`", 3)
    assert is_marker is True


def test_four_leading_spaces_do_not_start_a_commonmark_fence() -> None:
    state, is_marker = advance_markdown_fence("    ```python", None)

    assert state is None
    assert is_marker is False


def test_indented_text_does_not_close_an_open_fence() -> None:
    state, is_marker = advance_markdown_fence("    ```", ("`", 3))

    assert state == ("`", 3)
    assert is_marker is False
