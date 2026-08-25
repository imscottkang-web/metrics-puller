"""Read the video registry from scripts/<slug>/bet_card.md files.

The bet-card set is an ALLOWLIST: only a video published through this project
ever gets a bet card, so a pre-project video - or any off-protocol video with
no bet card at all - is simply never returned by read_registry; there is no
deletion or manual exclusion to maintain. See the design spec, "Which videos,
and where slugs come from":
docs/specs/2026-07-12-part-c-metrics-puller-design.md

Pure and read-only: the only I/O anywhere in this module is reading
bet_card.md files under a given directory. No network, no other imports.

A hand-edited bet card can be malformed in ways a person will actually hit:
a typo'd date, a file re-saved in the wrong encoding, or a copy-pasted video
id that already belongs to another card. None of those may crash the whole
pull - each bad card is skipped on its own, with a plain-English warning
(RegistryResult.warnings) a solo non-engineer can act on without a traceback.

Warnings are for ACTIONABLE problems only; the caller may treat any warning
as a red run. A drafted-but-not-yet-published card (no video id yet) is a
normal, healthy state - the design spec calls it "skipped with a logged
note" - so it goes to RegistryResult.notes (informational, print-only),
never to warnings.
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import re

# A "|---|---|" (or ":---:"-style aligned) markdown table divider row.
_SEPARATOR_RE = re.compile(r"^[\s:|-]+$")

# Splits a markdown-table row on "|" characters, but NOT on an escaped "\|"
# (a literal pipe inside a cell's content). The negative lookbehind means the
# match is a bare "|" not immediately preceded by a backslash.
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")

_DEFAULT_FORMAT = "longform"


def _split_row_cells(line: str) -> list[str]:
    """Split one markdown-table row into stripped cells.

    A cell may contain a literal pipe written as "\\|" so it does not end the
    cell early; such escaped pipes are not split on, and the escape is then
    removed from the cell's text (so callers see a plain "|").
    """
    return [cell.strip().replace("\\|", "|") for cell in _UNESCAPED_PIPE_RE.split(line)]


def _parse_fields(text: str) -> dict[str, str]:
    """Parse a bet-card markdown table into {field name lower: content}.

    Tolerant of extra spaces around pipes. Skips the header row
    ("| Field | Content |") and the "|---|---|" divider row. Only the two
    leading cells of each row matter; anything after is ignored. A cell may
    contain a literal pipe escaped as "\\|" (see _split_row_cells).
    """
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = _split_row_cells(line)
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        if len(cells) < 2:
            continue
        name, content = cells[0], cells[1]
        if name.lower() == "field" or _SEPARATOR_RE.match(name):
            continue
        fields[name.lower()] = content
    return fields


def _is_placeholder(value: str) -> bool:
    """True if value is empty, or still an unfilled template placeholder.

    A placeholder cell looks like "<youtube id>" or "<slug>" - i.e. it still
    contains the angle brackets from bet_card.template.md.
    """
    return not value or "<" in value or ">" in value


def _split_video_id_and_slug(content: str, folder_name: str) -> tuple[str, str]:
    """Split a "video id / slug" cell's content into (video_id, slug).

    Both sides are stripped of surrounding whitespace. If the slug half is
    empty, it falls back to the containing folder's name.
    """
    parts = content.split("/")
    video_id = parts[0].strip() if len(parts) >= 1 else ""
    slug = parts[1].strip() if len(parts) >= 2 else ""
    if not slug:
        slug = folder_name
    return video_id, slug


@dataclass(frozen=True)
class RegistryResult:
    """What read_registry found under scripts_dir. Exactly three attributes.

    entries: one {"video_id", "slug", "publish_date", "format"} dict per
      immediate subfolder whose bet card carries a real (non-placeholder)
      video id AND passes validation, in folder-name sorted order. "format"
      defaults to "longform" when the bet card has no format row.
    warnings: one plain-English string per ACTIONABLE problem - a date that
      is not valid YYYY-MM-DD, a file that is not valid UTF-8, or a video id
      that duplicates an earlier card's. Something needs fixing; the caller
      may treat any warning as a red run. Each message names the bet_card.md
      path (or the slugs involved, for a duplicate).
    notes: plain-English INFORMATIONAL messages, print-only - a bet card that
      is drafted but not yet published (no video id yet) is a normal, healthy
      state, recorded here naming the slug. Notes never turn a run red.

    Every message is safe to print as-is to a solo non-engineer - never a
    traceback.
    """

    entries: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def read_registry(scripts_dir: Path | str) -> RegistryResult:
    """Read every scripts/<slug>/bet_card.md directly under scripts_dir.

    A folder with no bet_card.md at all produces neither an entry nor a
    warning: it is simply absent. That is the allowlist behavior described in
    the design spec - only videos published through this project are ever
    tracked, so pre-project or off-protocol videos need no exclusion logic.

    Each bet card found is handled independently: a problem with one card
    (bad encoding, a bad date, a duplicate video id) skips only that card and
    records a warning; it never stops the rest of the registry from loading.
    A drafted card (no video id yet) is skipped with an informational note
    instead - see RegistryResult.
    """
    root = Path(scripts_dir)
    result = RegistryResult()
    if not root.is_dir():
        return result

    seen_video_ids: dict[str, str] = {}  # video_id -> slug of the entry keeping it

    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        bet_card = folder / "bet_card.md"
        if not bet_card.is_file():
            continue

        try:
            text = bet_card.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            result.warnings.append(
                f"{bet_card}: not readable as UTF-8 (re-save the file as UTF-8); video skipped."
            )
            continue

        fields = _parse_fields(text)
        video_id, slug = _split_video_id_and_slug(
            fields.get("video id / slug", ""), folder.name
        )

        if _is_placeholder(video_id):
            # A healthy in-between state, not a problem: informational note
            # (naming the slug), never a warning - it must not turn a run red.
            result.notes.append(
                f"'{slug}': drafted, not yet published (no video id in its bet card yet); skipped."
            )
            continue

        publish_date = fields.get("date", "").strip()
        if publish_date:
            try:
                date.fromisoformat(publish_date)
            except ValueError:
                result.warnings.append(
                    f"{bet_card}: date '{publish_date}' is not YYYY-MM-DD; video skipped until fixed."
                )
                continue

        if video_id in seen_video_ids:
            first_slug = seen_video_ids[video_id]
            result.warnings.append(
                f"video id '{video_id}' appears in both '{first_slug}' and '{slug}'; "
                f"keeping '{first_slug}', skipping '{slug}'."
            )
            continue
        seen_video_ids[video_id] = slug

        video_format = fields.get("format", "").strip() or _DEFAULT_FORMAT
        result.entries.append(
            {
                "video_id": video_id,
                "slug": slug,
                "publish_date": publish_date,
                "format": video_format,
            }
        )

    return result
