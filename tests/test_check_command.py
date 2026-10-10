"""The `check` command's argument handling.

The gate logic itself is covered exhaustively in test_gate.py; this covers the
parsing that gets ids to it, including the leading-hyphen case that argparse
would otherwise swallow.
"""

import pytest

from src.main import _as_video_id, _shield_leading_dash_ids


@pytest.mark.parametrize("token,expected", [
    ("jZ3uOrCqUQE", "jZ3uOrCqUQE"),
    ("-QEje5NEHMs", "-QEje5NEHMs"),                       # leading hyphen
    ("_underscore", "_underscore"),            # exactly 11 chars
    ("https://www.youtube.com/watch?v=jZ3uOrCqUQE", "jZ3uOrCqUQE"),
    ("https://www.youtube.com/watch?v=jZ3uOrCqUQE&t=42s", "jZ3uOrCqUQE"),
    ("https://youtu.be/jZ3uOrCqUQE", "jZ3uOrCqUQE"),
    ("https://www.youtube.com/shorts/jZ3uOrCqUQE", "jZ3uOrCqUQE"),
])
def test_accepted_forms(token, expected):
    assert _as_video_id(token) == expected


@pytest.mark.parametrize("token", [
    "too-short", "waytoolongtobeanid", "has spaces", "",
    "_underscore1",                               # 12 chars: ids are exactly 11
    "https://www.youtube.com/@Formula1",          # a channel, not a video
])
def test_rejected_forms(token):
    assert _as_video_id(token) is None


def test_a_channel_url_is_not_mistaken_for_a_video():
    """Easy mistake to make, and silently checking the wrong thing is worse
    than an error."""
    assert _as_video_id("https://www.youtube.com/channel/UCB_qr75-ydFVKSF9Dmo6izg") is None


# --- the argparse shim ----------------------------------------------------


def test_a_separator_is_inserted_so_hyphen_ids_survive():
    assert _shield_leading_dash_ids(["check", "-QEje5NEHMs"]) == [
        "check", "--", "-QEje5NEHMs"
    ]


def test_options_before_the_ids_are_preserved():
    assert _shield_leading_dash_ids(
        ["check", "--assume-uncleared", "-QEje5NEHMs"]
    ) == ["check", "--assume-uncleared", "--", "-QEje5NEHMs"]


def test_an_explicit_separator_is_left_alone():
    argv = ["check", "--", "-QEje5NEHMs"]
    assert _shield_leading_dash_ids(argv) == argv


def test_other_commands_are_untouched():
    for argv in (["monitor", "--dry-run"], ["status"], ["retry", "--reset-count"]):
        assert _shield_leading_dash_ids(argv) == argv


def test_check_with_no_ids_is_untouched():
    assert _shield_leading_dash_ids(["check"]) == ["check"]


def test_global_options_before_the_command_still_work():
    assert _shield_leading_dash_ids(
        ["--config", "x.yaml", "check", "-QEje5NEHMs"]
    ) == ["--config", "x.yaml", "check", "--", "-QEje5NEHMs"]


def test_the_command_is_registered_and_parses():
    from src.main import build_parser

    args = build_parser().parse_args(["check", "--", "-QEje5NEHMs", "abcdefghijk"])
    assert args.videos == ["-QEje5NEHMs", "abcdefghijk"]
    assert args.assume_uncleared is False
