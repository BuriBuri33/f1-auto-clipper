"""Real end-to-end transcription against audio with known timing.

Skipped unless FFmpeg and faster-whisper are both present, so the main suite
stays fast and dependency-light. Worth having despite the cost, because every
bug this phase actually had was invisible to the mocked tests:

* faster-whisper's unpinned PyAV dependency broke all decoding
* chunk sizes overshot the cap, because the planner used a measured average
  bitrate while the encoder used the nominal one
* chunks were planned past the end of the audio, because the duration came
  from the container rather than the audio stream
* segment end times were wrong by 4+ seconds without word timestamps

Speech is synthesised with `say`, so the ground truth is exact and no rights
question arises.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ffmpeg = shutil.which("ffmpeg")
say = shutil.which("say")

try:
    import faster_whisper  # noqa: F401
    HAVE_WHISPER = True
except ImportError:
    HAVE_WHISPER = False

pytestmark = pytest.mark.skipif(
    not (ffmpeg and say and HAVE_WHISPER),
    reason="needs ffmpeg, `say` (macOS) and faster-whisper",
)

UTTERANCES = [
    "Lights out and away we go.",
    "That is an incredible overtake into turn one.",
    "Box box box, confirm box this lap.",
]
GAP = 4.0
LEAD_IN = 2.0


def run(cmd):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == 0, result.stderr.decode()[-400:]
    return result


def duration_of(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)]).stdout
    return float(out.decode().strip())


@pytest.fixture(scope="module")
def known_audio(tmp_path_factory):
    """Build audio whose speech sits at timings we chose, and return both."""
    d = tmp_path_factory.mktemp("speech")
    parts, truth, clock = [], [], LEAD_IN

    silence = d / "gap.wav"
    run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi",
         "-t", str(GAP), "-i", "anullsrc=r=16000:cl=mono", str(silence)])
    lead = d / "lead.wav"
    run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi",
         "-t", str(LEAD_IN), "-i", "anullsrc=r=16000:cl=mono", str(lead)])

    parts.append(lead)
    for index, text in enumerate(UTTERANCES):
        aiff, wav = d / ("u%d.aiff" % index), d / ("u%d.wav" % index)
        run(["say", "-o", str(aiff), text])
        run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(aiff),
             "-ac", "1", "-ar", "16000", str(wav)])
        length = duration_of(wav)
        truth.append((clock, clock + length, text))
        clock += length
        parts.append(wav)
        if index < len(UTTERANCES) - 1:
            parts.append(silence)
            clock += GAP

    listing = d / "list.txt"
    listing.write_text("".join("file '%s'\n" % p for p in parts))
    joined = d / "known.wav"
    run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "concat",
         "-safe", "0", "-i", str(listing), "-c", "copy", str(joined)])

    media = d / "known.mp4"
    run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=320x240:r=5",
         "-i", str(joined), "-shortest", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(media)])
    return media, truth


def test_probe_reports_the_audio_duration_not_the_container(known_audio):
    """They differ, and planning from the container put chunks past the end
    of the audio, producing undecodable files."""
    from src.transcription.audio import probe_duration

    media, truth = known_audio
    audio_seconds = probe_duration(media)
    assert audio_seconds == pytest.approx(truth[-1][1], abs=0.6)
    assert audio_seconds < duration_of(media) - 1.0


def test_transcription_places_speech_at_the_right_times(known_audio, tmp_path):
    from src.transcription.transcriber import transcribe

    media, truth = known_audio
    transcript = transcribe(media, work_dir=tmp_path, backend="local",
                            local_model="tiny", language="en")

    assert transcript.backend == "local-whisper"
    assert transcript.has_word_timings, "word timings are what make ends accurate"
    assert len(transcript.segments) == len(UTTERANCES)

    for (expected_start, expected_end, _text), segment in zip(
        truth, transcript.segments
    ):
        assert segment.start == pytest.approx(expected_start, abs=1.0)
        assert segment.end == pytest.approx(expected_end, abs=1.0)


def test_chunked_transcription_re_times_correctly_across_real_boundaries(
    known_audio, tmp_path
):
    """The failure this guards against is silent: a forgotten offset yields a
    fluent transcript in which everything after the boundary is misplaced."""
    from src.transcription import audio as audio_module
    from src.transcription.local_backend import LocalWhisperTranscriber
    from src.transcription.types import normalise, resegment_from_words

    media, truth = known_audio
    seconds = audio_module.probe_duration(media)
    cap = 50_000           # small enough to force several boundaries

    chunks = audio_module.prepare_chunks(
        media, tmp_path, max_bytes=cap, duration=seconds,
        bitrate_kbps=32, overlap=4.0,
    )
    assert len(chunks) > 1, "the cap must actually force a split"
    for chunk in chunks:
        assert chunk.path.stat().st_size <= cap
    assert chunks[-1].window_end == pytest.approx(seconds, abs=0.01)

    backend = LocalWhisperTranscriber(model_size="tiny", language="en")
    per_chunk = [backend.transcribe_file(c.path)[0] for c in chunks]
    merged = normalise(
        audio_module.merge_chunk_segments(chunks, per_chunk),
        source_duration=seconds,
    )
    segments = resegment_from_words(merged)

    texts = [s.text for s in segments]
    assert len(texts) == len(set(texts)), "overlap must not duplicate speech"

    for expected_start, expected_end, _text in truth:
        hits = [s for s in segments
                if s.start < expected_end + 1.5 and s.end > expected_start - 1.5]
        assert hits, "nothing found near %.2f-%.2f" % (expected_start, expected_end)


def test_the_stored_transcript_round_trips(known_audio, tmp_path):
    from src.transcription.transcriber import transcribe
    from src.transcription.types import Transcript

    media, _truth = known_audio
    original = transcribe(media, work_dir=tmp_path, backend="local",
                          local_model="tiny", language="en")
    path = original.write(tmp_path / "t.json")
    loaded = Transcript.read(path)

    assert len(loaded.segments) == len(original.segments)
    assert loaded.has_word_timings == original.has_word_timings
    assert loaded.segments[0].text == original.segments[0].text
