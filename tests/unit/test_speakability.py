from __future__ import annotations

import pytest

from voice_pipeline.modules.text.speakability import is_pause_marker, is_speakable_text


@pytest.mark.parametrize("value", ["", " \n\t", "“……”", "——", "』", "?!，。"])
def test_punctuation_only_text_is_never_speakable(value: str) -> None:
    assert is_speakable_text(value) is False


@pytest.mark.parametrize("value", ["……", " ... ", "——", "！？", "\n～\t"])
def test_pause_marker_accepts_only_pause_symbols(value: str) -> None:
    assert is_pause_marker(value) is True


@pytest.mark.parametrize("value", ["祥子，为什么——", "Your Majesty", "第10章", "日本語", "한글"])
def test_unicode_letters_and_numbers_are_speakable(value: str) -> None:
    assert is_speakable_text(value) is True
    assert is_pause_marker(value) is False

