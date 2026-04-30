from cgw.text_utils import clip_text, short_text


def test_clip_text_respects_limit() -> None:
    value, truncated = clip_text("abcdef", 4)
    assert value == "abcd"
    assert truncated is True


def test_clip_text_no_truncate_when_short() -> None:
    value, truncated = clip_text("abc", 10)
    assert value == "abc"
    assert truncated is False


def test_short_text_normalizes_whitespace_and_ellipsis() -> None:
    assert short_text("a   b\nc", limit=20) == "a b c"
    assert short_text("0123456789", limit=6) == "01234…"

