from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from voice_pipeline.models.director import DirectorAdjustmentAction

_REFERENCE_FIELDS = frozenset({"ref_text_cn", "emotion_vector"})
_GSV_FIELDS = frozenset({"synthesis_text", "speed_factor"})
_COMPOSITION_FIELDS = frozenset({"pause_after_ms"})


@dataclass(frozen=True)
class AdjustmentDecision:
    reference_stale: bool
    gsv_stale: bool
    composition_stale: bool
    requested_action: DirectorAdjustmentAction
    effective_action: DirectorAdjustmentAction


def resolve_adjustment(
    *,
    changed_fields: frozenset[str],
    requested_action: str,
    reference_valid: bool,
) -> AdjustmentDecision:
    if requested_action not in {"save", "reference", "gsv", "both", "recompose"}:
        raise ValueError(f"unknown Director adjustment action: {requested_action}")
    requested = cast(DirectorAdjustmentAction, requested_action)
    reference_stale = bool(changed_fields & _REFERENCE_FIELDS)
    gsv_stale = reference_stale or bool(changed_fields & _GSV_FIELDS)
    composition_stale = gsv_stale or bool(changed_fields & _COMPOSITION_FIELDS)
    effective = requested
    if requested == "gsv" and (reference_stale or not reference_valid):
        effective = "both"
    elif requested == "recompose" and gsv_stale:
        effective = "save"
    return AdjustmentDecision(
        reference_stale=reference_stale,
        gsv_stale=gsv_stale,
        composition_stale=composition_stale,
        requested_action=requested,
        effective_action=effective,
    )
