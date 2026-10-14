"""Turn a unified diff into Hunk objects. Thin wrapper around `unidiff`."""
from __future__ import annotations

from unidiff import PatchSet

from review_agent.schemas import Hunk


def parse_diff(diff_text: str) -> list[Hunk]:
    """Parse unified-diff text into one Hunk per contiguous changed block."""
    patch = PatchSet(diff_text)
    hunks: list[Hunk] = []

    for patched_file in patch:
        if patched_file.is_removed_file:
            continue
        file_path = patched_file.path
        for hunk in patched_file:
            added_pairs = [(line.target_line_no, line.value.rstrip("\n")) for line in hunk if line.is_added]
            context_pairs = [(line.target_line_no, line.value.rstrip("\n")) for line in hunk if line.is_context]
            removed: list[str] = []
            removed_anchors: list[int] = []
            last_target_no = hunk.target_start - 1
            for line in hunk:  # unidiff yields lines in true unified order
                if line.is_removed:
                    removed.append(line.value.rstrip("\n"))
                    removed_anchors.append(last_target_no)
                elif line.target_line_no is not None:
                    last_target_no = line.target_line_no
            if not added_pairs and not removed:
                continue
            start = hunk.target_start
            end = hunk.target_start + max(hunk.target_length - 1, 0)
            hunks.append(
                Hunk(
                    file=file_path,
                    start_line=start,
                    end_line=end,
                    added_lines=[text for _, text in added_pairs],
                    added_line_numbers=[no for no, _ in added_pairs],
                    removed_lines=removed,
                    removed_anchors=removed_anchors,
                    context_lines=[text for _, text in context_pairs],
                    context_line_numbers=[no for no, _ in context_pairs],
                )
            )
    return hunks
