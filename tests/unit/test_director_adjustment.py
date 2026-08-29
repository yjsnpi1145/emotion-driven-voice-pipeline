from __future__ import annotations

import pytest

from voice_pipeline.core.director_adjustment import resolve_adjustment


@pytest.mark.parametrize(
    ("changed", "requested", "reference_valid", "expected"),
    [
        (
            {"ref_text_cn"},
            "save",
            True,
            (True, True, True, "save"),
        ),
        (
            {"emotion_vector"},
            "gsv",
            True,
            (True, True, True, "both"),
        ),
        (
            {"synthesis_text"},
            "gsv",
            True,
            (False, True, True, "gsv"),
        ),
        (
            {"speed_factor"},
            "gsv",
            False,
            (False, True, True, "both"),
        ),
        (
            {"pause_after_ms"},
            "recompose",
            True,
            (False, False, True, "recompose"),
        ),
        (
            {"synthesis_text"},
            "recompose",
            True,
            (False, True, True, "save"),
        ),
        (
            set(),
            "reference",
            True,
            (False, False, False, "reference"),
        ),
        (
            set(),
            "both",
            True,
            (False, False, False, "both"),
        ),
    ],
)
def test_resolve_adjustment_dependencies(
    changed: set[str],
    requested: str,
    reference_valid: bool,
    expected: tuple[bool, bool, bool, str],
) -> None:
    decision = resolve_adjustment(
        changed_fields=frozenset(changed),
        requested_action=requested,
        reference_valid=reference_valid,
    )

    assert (
        decision.reference_stale,
        decision.gsv_stale,
        decision.composition_stale,
        decision.effective_action,
    ) == expected
