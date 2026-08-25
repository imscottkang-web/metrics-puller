"""Tests for metrics_lib.registry - the bet-card allowlist reader.

No network; the only I/O is reading bet_card.md fixture files from disk
(mirrors the read-only-file style used across this suite).

read_registry() returns a RegistryResult with exactly three attributes:
  .entries  - one dict per usable, published video's bet card (unchanged
              shape: video_id/slug/publish_date/format).
  .warnings - ACTIONABLE problems only (a malformed date, a non-UTF-8 file,
              a video id duplicated across two cards). Something needs
              fixing; the caller may treat any warning as a red run.
  .notes    - INFORMATIONAL messages: a drafted-but-not-yet-published bet
              card (no video id yet) is a normal, healthy state, so it is a
              note naming the slug - never a warning, never a red run.
All messages are plain-English strings, meant to be printed as-is to a solo
non-engineer - never a traceback.
"""

from pathlib import Path

from metrics_lib.registry import _parse_fields, read_registry

FIXTURES_SCRIPTS_DIR = Path(__file__).parent / "fixtures" / "scripts"


def _write_bet_card(folder: Path, *, date: str, video_id_slug: str, fmt: str = "longform") -> Path:
    """Write a minimal bet_card.md under folder (created if needed). Returns its path."""
    folder.mkdir(parents=True, exist_ok=True)
    card = folder / "bet_card.md"
    card.write_text(
        "# Bet Card\n\n"
        "| Field | Content |\n"
        "|---|---|\n"
        f"| date | {date} |\n"
        f"| video id / slug | {video_id_slug} |\n"
        f"| format | {fmt} |\n",
        encoding="utf-8",
    )
    return card


def test_read_registry_returns_one_entry_for_published_video():
    result = read_registry(FIXTURES_SCRIPTS_DIR)

    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry["video_id"] == "dQw4w9WgXcQ"
    assert entry["slug"] == "seller-repairs"
    assert entry["publish_date"] == "2026-07-05"
    assert entry["format"] == "longform"
    # The drafted-not-published sibling card is a healthy state: a note, not
    # an actionable warning (warnings would turn the daily run red).
    assert result.warnings == []


def test_read_registry_skips_drafted_not_published_video():
    result = read_registry(FIXTURES_SCRIPTS_DIR)

    slugs = [entry["slug"] for entry in result.entries]
    assert "drafted-not-published" not in slugs
    # Drafted-but-not-published is informational: it goes to .notes (naming
    # the slug), never to .warnings.
    assert any("drafted-not-published" in note for note in result.notes)
    assert not any("drafted-not-published" in w for w in result.warnings)


def test_drafted_only_scripts_dir_produces_zero_warnings_and_one_note(tmp_path):
    # A registry whose only card is still drafted is a normal, healthy state:
    # the daily run must NOT go red over it, so zero warnings - one note.
    _write_bet_card(
        tmp_path / "my-next-video",
        date="<YYYY-MM-DD>",
        video_id_slug="<youtube id> / my-next-video",
    )

    result = read_registry(tmp_path)

    assert result.entries == []
    assert result.warnings == []
    assert len(result.notes) == 1
    note = result.notes[0]
    assert "my-next-video" in note  # plain English, naming the slug
    assert "not yet published" in note


def test_slug_falls_back_to_folder_name_when_slug_cell_is_empty(tmp_path):
    folder = tmp_path / "no-slug-video"
    folder.mkdir()
    (folder / "bet_card.md").write_text(
        "# Bet Card - No Slug Video\n\n"
        "| Field | Content |\n"
        "|---|---|\n"
        "| date | 2026-06-01 |\n"
        "| video id / slug | abcDEFghi01 / |\n"
        "| format | longform |\n",
        encoding="utf-8",
    )

    result = read_registry(tmp_path)

    assert result.warnings == []
    assert result.notes == []
    assert len(result.entries) == 1
    assert result.entries[0]["video_id"] == "abcDEFghi01"
    assert result.entries[0]["slug"] == "no-slug-video"
    assert result.entries[0]["publish_date"] == "2026-06-01"
    assert result.entries[0]["format"] == "longform"


def test_read_registry_on_missing_directory_returns_empty():
    result = read_registry(Path("/nonexistent/path/for/registry/test"))
    assert result.entries == []
    assert result.warnings == []
    assert result.notes == []


# --- date validation (defect 5a) -------------------------------------------


def test_non_iso_date_skips_card_and_warns_with_path_and_value(tmp_path):
    folder = tmp_path / "bad-date-video"
    card = _write_bet_card(folder, date="2026-7-5", video_id_slug="abcDEFghi02 / bad-date-video")

    result = read_registry(tmp_path)

    assert result.entries == []
    assert result.notes == []  # a bad date is actionable, not informational
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert str(card) in warning
    assert "2026-7-5" in warning
    assert "YYYY-MM-DD" in warning
    assert "video skipped" in warning


def test_slash_style_date_also_skips_card_and_warns(tmp_path):
    folder = tmp_path / "slash-date-video"
    _write_bet_card(folder, date="07/05/2026", video_id_slug="abcDEFghi03 / slash-date-video")

    result = read_registry(tmp_path)

    assert result.entries == []
    assert any("07/05/2026" in w for w in result.warnings)


def test_blank_date_is_accepted_and_not_a_warning(tmp_path):
    # A video with no publish date yet is a normal, expected state (checkpoint
    # math falls back to "weekly"), not a malformed one - it must not warn.
    folder = tmp_path / "no-date-yet"
    _write_bet_card(folder, date="", video_id_slug="abcDEFghi04 / no-date-yet")

    result = read_registry(tmp_path)

    assert result.warnings == []
    assert result.notes == []
    assert len(result.entries) == 1
    assert result.entries[0]["publish_date"] == ""


# --- encoding validation (defect 5b) ----------------------------------------


def test_non_utf8_card_skips_and_warns(tmp_path):
    folder = tmp_path / "bad-encoding-video"
    folder.mkdir()
    # A curly apostrophe saved as latin-1 is not valid UTF-8 (byte 0x92 alone
    # is not a legal UTF-8 start byte).
    raw = (
        "# Bet Card\n\n"
        "| Field | Content |\n"
        "|---|---|\n"
        "| date | 2026-06-01 |\n"
        "| video id / slug | abcDEFghi05 / bad-encoding-video |\n"
        "| title + bets | Seller\x92s Repairs |\n"
        "| format | longform |\n"
    ).encode("latin-1")
    card = folder / "bet_card.md"
    card.write_bytes(raw)

    result = read_registry(tmp_path)

    assert result.entries == []
    assert result.notes == []  # undecodable file is actionable, not informational
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert str(card) in warning
    assert "UTF-8" in warning
    assert "video skipped" in warning


# --- escaped-pipe cell parsing (defect 5c) ----------------------------------


def test_parse_fields_unescapes_pipe_without_fragmenting_the_row():
    text = (
        "| Field | Content |\n"
        "|---|---|\n"
        "| date | 2026-06-01 |\n"
        "| title + bets | Repairs Sellers Regret \\| Skipping - bets: H1 |\n"
        "| video id / slug | abcDEFghi06 / escaped-pipe-video |\n"
    )

    fields = _parse_fields(text)

    assert fields["title + bets"] == "Repairs Sellers Regret | Skipping - bets: H1"
    assert fields["date"] == "2026-06-01"
    assert fields["video id / slug"] == "abcDEFghi06 / escaped-pipe-video"


def test_read_registry_unescapes_pipe_in_video_id_slug_cell(tmp_path):
    # Exercises the same escaping mechanism through the public read_registry
    # surface: an escaped pipe inside the one cell read_registry does return
    # must survive intact instead of fragmenting the row and truncating it.
    folder = tmp_path / "escaped-pipe-slug"
    folder.mkdir()
    (folder / "bet_card.md").write_text(
        "# Bet Card\n\n"
        "| Field | Content |\n"
        "|---|---|\n"
        "| date | 2026-06-01 |\n"
        "| video id / slug | abcDEFghi07 / weird\\|slug |\n"
        "| format | longform |\n",
        encoding="utf-8",
    )

    result = read_registry(tmp_path)

    assert result.warnings == []
    assert len(result.entries) == 1
    assert result.entries[0]["video_id"] == "abcDEFghi07"
    assert result.entries[0]["slug"] == "weird|slug"


def test_read_registry_handles_card_with_escaped_pipe_in_unread_field(tmp_path):
    # Integration-level check: an escaped pipe in a field read_registry does
    # NOT return (title + bets) must not disturb parsing of the fields it does.
    folder = tmp_path / "escaped-pipe-video"
    folder.mkdir()
    (folder / "bet_card.md").write_text(
        "# Bet Card\n\n"
        "| Field | Content |\n"
        "|---|---|\n"
        "| date | 2026-06-01 |\n"
        "| video id / slug | abcDEFghi08 / escaped-pipe-video |\n"
        "| title + bets | Repairs Sellers Regret \\| Skipping - bets: H1 |\n"
        "| format | longform |\n",
        encoding="utf-8",
    )

    result = read_registry(tmp_path)

    assert result.warnings == []
    assert len(result.entries) == 1
    assert result.entries[0]["video_id"] == "abcDEFghi08"
    assert result.entries[0]["slug"] == "escaped-pipe-video"
    assert result.entries[0]["publish_date"] == "2026-06-01"


# --- duplicate video id across two cards (defect 5d) ------------------------


def test_duplicate_video_id_keeps_first_skips_second_and_warns_naming_both(tmp_path):
    _write_bet_card(tmp_path / "aaa-first", date="2026-06-01", video_id_slug="dupID123456 / aaa-first")
    _write_bet_card(tmp_path / "zzz-second", date="2026-06-02", video_id_slug="dupID123456 / zzz-second")

    result = read_registry(tmp_path)

    slugs = [entry["slug"] for entry in result.entries]
    assert slugs == ["aaa-first"]  # sorted folder order; first kept, second dropped
    assert result.notes == []  # a duplicate id is actionable, not informational
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert "aaa-first" in warning
    assert "zzz-second" in warning
    assert "dupID123456" in warning


def test_duplicate_video_id_does_not_count_a_card_skipped_for_bad_date(tmp_path):
    # The first card never becomes an entry (bad date), so it must not block
    # the second, otherwise-valid card from using the same video id.
    _write_bet_card(tmp_path / "aaa-bad-date", date="2026-7-5", video_id_slug="dupID999999 / aaa-bad-date")
    _write_bet_card(tmp_path / "zzz-good", date="2026-06-02", video_id_slug="dupID999999 / zzz-good")

    result = read_registry(tmp_path)

    slugs = [entry["slug"] for entry in result.entries]
    assert slugs == ["zzz-good"]
    assert not any("dupID999999' appears in both" in w for w in result.warnings)
    assert any("2026-7-5" in w for w in result.warnings)
