from src.video import metadata


def test_build_title_truncates_at_a_word_boundary():
    title = metadata.build_title("word " * 40, max_chars=20)
    assert len(title) <= 20
    assert not title.endswith("word")  # cut cleanly, not mid-word
    assert title.endswith("…")


def test_build_title_falls_back_when_hook_is_empty():
    assert metadata.build_title("", max_chars=95) == "F1 highlight clip"
    assert metadata.build_title("   ", max_chars=95) == "F1 highlight clip"


def test_build_title_leaves_a_short_hook_untouched():
    assert metadata.build_title("Verstappen overtakes at turn 1", max_chars=95) == (
        "Verstappen overtakes at turn 1"
    )


def test_build_description_always_carries_the_non_affiliation_disclaimer():
    desc = metadata.build_description("A Race", "https://youtu.be/x", "", "")
    assert metadata.DISCLAIMER in desc
    assert "not affiliated" in desc.lower()


def test_build_description_never_claims_to_be_official():
    desc = metadata.build_description(
        "Official Race Highlights", "https://youtu.be/x",
        "a dramatic overtake", "box box box",
    )
    # The disclaimer is the thing doing the work here, not word scrubbing —
    # assert it survives even when the SOURCE title itself says "Official".
    assert "Not affiliated with, endorsed by, or sponsored by Formula 1" in desc


def test_build_description_includes_the_source_link_for_attribution():
    desc = metadata.build_description("A Race", "https://youtu.be/x", "reason", "text")
    assert "https://youtu.be/x" in desc


def test_build_hashtags_is_capped_regardless_of_how_many_names_are_configured():
    many_names = ",".join("Driver%d" % i for i in range(20))
    tags = metadata.build_hashtags(many_names, max_hashtags=6)
    assert len(tags) == 6


def test_build_hashtags_includes_the_base_set_first():
    tags = metadata.build_hashtags("", max_hashtags=6)
    assert tags == list(metadata.BASE_HASHTAGS)


def test_build_hashtags_adds_vocabulary_names_without_duplicating():
    tags = metadata.build_hashtags("Verstappen, Formula1", max_hashtags=10)
    assert tags.count("Formula1") == 1
    assert "Verstappen" in tags


def test_build_hashtags_skips_non_alphanumeric_entries():
    tags = metadata.build_hashtags("Max Verstappen!, ", max_hashtags=10)
    assert "Max Verstappen!" not in tags
    assert all(t.replace(" ", "").isalnum() for t in tags)


def test_build_metadata_assembles_all_three_fields():
    result = metadata.build_metadata(
        hook="Huge crash at turn 3", reason="high drama", transcript_text="box box box",
        source_title="Race Highlights", source_url="https://youtu.be/x",
        vocabulary_hint="Leclerc", max_title_chars=95, max_hashtags=5,
    )
    assert result.title == "Huge crash at turn 3"
    assert "Leclerc" in result.hashtags
    assert metadata.DISCLAIMER in result.description
