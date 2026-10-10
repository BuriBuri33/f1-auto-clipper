import pytest

from src.video import branding


def test_write_watermark_textfile_writes_literal_text(tmp_path):
    path = branding.write_watermark_textfile("@MyChannel", tmp_path / "w.txt")
    assert path.read_text(encoding="utf-8") == "@MyChannel"


def test_filter_clause_rejects_an_unknown_position(tmp_path):
    textfile = tmp_path / "w.txt"
    textfile.write_text("x")
    with pytest.raises(ValueError):
        branding.filter_clause(textfile, position="middle")


@pytest.mark.parametrize("position", branding.VALID_POSITIONS)
def test_filter_clause_builds_a_drawtext_stage_for_every_position(tmp_path, position):
    textfile = tmp_path / "w.txt"
    textfile.write_text("x")
    clause = branding.filter_clause(textfile, position=position, input_label="v2")
    assert clause.startswith("[v2]drawtext=")
    assert "textfile=" in clause
    assert "expansion=none" in clause


def test_top_right_anchors_from_the_right_and_top_edges(tmp_path):
    textfile = tmp_path / "w.txt"
    textfile.write_text("x")
    clause = branding.filter_clause(textfile, position="top_right", margin=40)
    assert "x=w-tw-40" in clause
    assert "y=40" in clause


def test_bottom_left_clears_the_caption_safe_zone(tmp_path):
    textfile = tmp_path / "w.txt"
    textfile.write_text("x")
    clause = branding.filter_clause(
        textfile, position="bottom_left", margin=40, caption_margin_bottom=220,
    )
    # Must sit clear of the caption margin, not just the watermark's own margin.
    assert "y=h-th-260" in clause  # max(40, 220+40)


def test_bottom_position_without_captions_uses_the_plain_margin(tmp_path):
    textfile = tmp_path / "w.txt"
    textfile.write_text("x")
    clause = branding.filter_clause(
        textfile, position="bottom_right", margin=40, caption_margin_bottom=None,
    )
    assert "y=h-th-40" in clause


def test_escape_textfile_path_quotes_and_escapes_colons():
    escaped = branding._escape_textfile_path("/tmp/a:b.txt")
    assert escaped == "'/tmp/a\\:b.txt'"
