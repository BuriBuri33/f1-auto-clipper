from src.video import ffmpeg

SAMPLE_FILTERS_OUTPUT = """Filters:
  T.. = Timeline support
  .S. = Slice threading
  A = Audio input/output
  V = Video input/output
  N = Dynamic number and/or type of input/output
  | = Source or sink filter
  ------
 TS aap               AA->A      Apply Affine Projection algorithm.
 .. ass               V->V       Render ASS subtitles onto input video using the libass library.
 .. crop              V->V       Crop the input video.
 T. drawtext          V->V       Draw text on top of video frames using libfreetype library.
 TS gblur             V->V       Apply Gaussian Blur filter.
 TS overlay           VV->V      Overlay a video source on top of the input.
"""

SAMPLE_FILTERS_WITHOUT_TEXT = """Filters:
  T.. = Timeline support
  ------
 TS aap               AA->A      Apply Affine Projection algorithm.
 .. crop              V->V       Crop the input video.
 TS gblur             V->V       Apply Gaussian Blur filter.
 TS overlay           VV->V      Overlay a video source on top of the input.
"""


def test_parse_filter_names_finds_every_filter_after_the_header():
    names = ffmpeg.parse_filter_names(SAMPLE_FILTERS_OUTPUT)
    assert {"ass", "crop", "drawtext", "gblur", "overlay"} <= names


def test_parse_filter_names_ignores_the_legend_lines():
    names = ffmpeg.parse_filter_names(SAMPLE_FILTERS_OUTPUT)
    assert "support" not in names    # from "T.. = Timeline support"
    assert "threading" not in names  # from ".S. = Slice threading"


def test_parse_filter_names_on_empty_input():
    assert ffmpeg.parse_filter_names("") == set()


def test_missing_required_filters_reports_what_a_minimal_build_lacks(monkeypatch):
    monkeypatch.setattr(
        ffmpeg, "available_filters",
        lambda: ffmpeg.parse_filter_names(SAMPLE_FILTERS_WITHOUT_TEXT),
    )
    missing = ffmpeg.missing_required_filters()
    assert set(missing) == {"ass", "drawtext"}


def test_missing_required_filters_is_empty_when_everything_is_present(monkeypatch):
    monkeypatch.setattr(
        ffmpeg, "available_filters",
        lambda: ffmpeg.parse_filter_names(SAMPLE_FILTERS_OUTPUT),
    )
    assert ffmpeg.missing_required_filters() == []


def test_available_filters_is_empty_set_when_ffmpeg_is_not_on_path(monkeypatch):
    monkeypatch.setattr(ffmpeg, "ffmpeg_available", lambda: False)
    assert ffmpeg.available_filters() == set()
