from __future__ import annotations

import pytest

from voice_pipeline.modules.text.structural_cleaner import StructuralTextCleaner


def test_cleaner_splits_dialogue_bridge_dialogue_exactly() -> None:
    source = "“我的初吻……”她慌乱地摆弄着手指，目光四处乱飘，“祥子，为什么——”"

    document = StructuralTextCleaner().clean(source)

    assert document.structural_text == source
    assert [
        (unit.text, unit.context) for unit in document.paragraphs[0].units
    ] == [
        ("“我的初吻……”", "quoted_dialogue"),
        ("她慌乱地摆弄着手指，目光四处乱飘，", "quote_bridge_narration"),
        ("“祥子，为什么——”", "quoted_dialogue"),
    ]
    assert "".join(unit.text for unit in document.paragraphs[0].units) == source


@pytest.mark.parametrize(
    ("source", "quoted"),
    [
        ("前「日本語」后", "「日本語」"),
        ("前『二重引用』后", "『二重引用』"),
        ('before "English speech" after', '"English speech"'),
    ],
)
def test_cleaner_supports_chinese_japanese_and_ascii_quotes(
    source: str, quoted: str
) -> None:
    document = StructuralTextCleaner().clean(source)

    matches = [
        unit.text
        for paragraph in document.paragraphs
        for unit in paragraph.units
        if unit.context == "quoted_dialogue"
    ]
    assert matches == [quoted]
    assert document.structural_text == source


def test_cleaner_scans_a_quote_across_the_old_chunk_boundary() -> None:
    source = "旁白。" + "“" + ("很长的对白。" * 430) + "”" + "结尾。"
    assert source.index("”") > 2400

    document = StructuralTextCleaner().clean(source)

    quoted = [
        unit
        for paragraph in document.paragraphs
        for unit in paragraph.units
        if unit.context == "quoted_dialogue"
    ]
    assert len(quoted) == 1
    assert quoted[0].text.startswith("“")
    assert quoted[0].text.endswith("”")
    assert "".join(
        unit.text for paragraph in document.paragraphs for unit in paragraph.units
    ) == source


def test_cleaner_normalizes_newlines_but_retains_raw_paragraph_ranges() -> None:
    source = "\r\n甲。\r\n\r\n\r\n乙。\r\n"

    document = StructuralTextCleaner().clean(source)

    assert document.structural_text == "甲。\n\n乙。"
    assert [paragraph.source_text for paragraph in document.paragraphs] == ["甲。", "乙。"]
    assert [
        source[paragraph.source_start : paragraph.source_end]
        for paragraph in document.paragraphs
    ] == ["甲。", "乙。"]


def test_cleaner_retains_an_isolated_pause_paragraph_without_making_it_speakable() -> None:
    document = StructuralTextCleaner().clean("第一段。\n\n……\n\n第二段。")

    pause = document.paragraphs[1]
    assert pause.structural_text == "……"
    assert [(unit.text, unit.context) for unit in pause.units] == [("……", "pause_marker")]
    assert pause.units[0].speakable is False


def test_cleaner_is_stable_and_never_emits_formatting_only_units_between_text() -> None:
    source = "他说。\n”\n下一句。"
    cleaner = StructuralTextCleaner()

    first = cleaner.clean(source)
    second = cleaner.clean(source)

    assert first == second
    assert all(
        unit.speakable or unit.context == "pause_marker"
        for paragraph in first.paragraphs
        for unit in paragraph.units
    )
    assert "".join(
        unit.text for paragraph in first.paragraphs for unit in paragraph.units
    ) == first.structural_text
