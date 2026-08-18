"""
Pure unit tests for the ??? ... ??? speaker-note stripping regex
(app/services/presentation_service.strip_speaker_notes).
"""
from app.services.presentation_service import strip_speaker_notes


def test_single_speaker_note_block_removed():
    md = "# Slide 1\nContent\n\n???\nSpeaker notes here\n???\n\n# Slide 2\n"
    out = strip_speaker_notes(md)
    assert "Speaker notes here" not in out
    assert "# Slide 1" in out and "Content" in out
    assert "# Slide 2" in out


def test_multiple_speaker_note_blocks_all_removed():
    md = (
        "# Slide 1\nA\n???\nnote A\n???\n"
        "# Slide 2\nB\n???\nnote B\n???\n"
    )
    out = strip_speaker_notes(md)
    assert "note A" not in out
    assert "note B" not in out
    assert "# Slide 1" in out and "A" in out
    assert "# Slide 2" in out and "B" in out


def test_multiline_speaker_note_body_removed():
    md = "# Slide 1\n???\nline one\nline two\nline three\n???\nafter\n"
    out = strip_speaker_notes(md)
    assert "line one" not in out
    assert "line two" not in out
    assert "after" in out


def test_no_speaker_notes_content_unchanged():
    md = "# Slide 1\nJust content, no notes at all.\n# Slide 2\nMore.\n"
    assert strip_speaker_notes(md) == md
