from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from voice_pipeline.modules.text.speakability import is_pause_marker, is_speakable_text

StructuralContext = Literal[
    "quoted_dialogue",
    "quote_bridge_narration",
    "narration",
    "formatting",
    "pause_marker",
]

_NEWLINE = re.compile(r"\r\n|\r|\n")
_LEADING_BLANK_LINES = re.compile(r"\A(?:[ \t]*(?:\r\n|\r|\n))+")
_TRAILING_BLANK_LINES = re.compile(r"(?:(?:\r\n|\r|\n)[ \t]*)+\Z")
_PARAGRAPH_BREAK = re.compile(
    r"(?:\r\n|\r|\n)[ \t]*(?:(?:\r\n|\r|\n)[ \t]*)+"
)
_QUOTE_OPENERS = {"“": "”", "「": "」", "『": "』"}


@dataclass(frozen=True, slots=True)
class StructuralUnit:
    unit_id: str
    text: str
    context: StructuralContext
    speakable: bool


@dataclass(frozen=True, slots=True)
class StructuralParagraph:
    paragraph_id: str
    ordinal: int
    source_start: int
    source_end: int
    source_text: str
    structural_text: str
    units: tuple[StructuralUnit, ...]


@dataclass(frozen=True, slots=True)
class StructuralDocument:
    structural_text: str
    paragraphs: tuple[StructuralParagraph, ...]


@dataclass(frozen=True, slots=True)
class _ParagraphDraft:
    paragraph_id: str
    ordinal: int
    source_start: int
    source_end: int
    source_text: str
    structural_text: str
    document_start: int
    document_end: int


class StructuralTextCleaner:
    """Lossless-at-source, deterministic structural cleanup for director mode."""

    def clean(self, source_text: str) -> StructuralDocument:
        if not source_text.strip():
            raise ValueError("source_text must not be blank")
        raw_paragraphs = _raw_paragraphs(source_text)
        normalized = tuple(_normalize_paragraph(item[2]) for item in raw_paragraphs)
        structural_text = "\n\n".join(normalized)
        drafts: list[_ParagraphDraft] = []
        document_cursor = 0
        for ordinal, ((source_start, source_end, raw), text) in enumerate(
            zip(raw_paragraphs, normalized, strict=True)
        ):
            paragraph_id = _digest(
                f"{ordinal}:{source_start}:{source_end}:{raw}"
            )
            drafts.append(
                _ParagraphDraft(
                    paragraph_id=paragraph_id,
                    ordinal=ordinal,
                    source_start=source_start,
                    source_end=source_end,
                    source_text=raw,
                    structural_text=text,
                    document_start=document_cursor,
                    document_end=document_cursor + len(text),
                )
            )
            document_cursor += len(text) + 2

        quote_spans = _balanced_quote_spans(structural_text)
        paragraphs = tuple(
            StructuralParagraph(
                paragraph_id=draft.paragraph_id,
                ordinal=draft.ordinal,
                source_start=draft.source_start,
                source_end=draft.source_end,
                source_text=draft.source_text,
                structural_text=draft.structural_text,
                units=_paragraph_units(draft, quote_spans),
            )
            for draft in drafts
        )
        return StructuralDocument(structural_text=structural_text, paragraphs=paragraphs)


def _raw_paragraphs(source_text: str) -> tuple[tuple[int, int, str], ...]:
    start_bound = _LEADING_BLANK_LINES.match(source_text)
    start = start_bound.end() if start_bound else 0
    trailing = _TRAILING_BLANK_LINES.search(source_text)
    end = trailing.start() if trailing else len(source_text)
    if start >= end:
        raise ValueError("source_text must contain non-blank text")

    rows: list[tuple[int, int, str]] = []
    cursor = start
    for match in _PARAGRAPH_BREAK.finditer(source_text, start, end):
        if match.start() > cursor:
            raw = source_text[cursor : match.start()]
            if raw.strip():
                rows.append((cursor, match.start(), raw))
        cursor = match.end()
    if cursor < end:
        raw = source_text[cursor:end]
        if raw.strip():
            rows.append((cursor, end, raw))
    if not rows:
        raw = source_text[start:end]
        rows.append((start, end, raw))
    return tuple(rows)


def _normalize_paragraph(value: str) -> str:
    return _NEWLINE.sub("\n", value)


def _balanced_quote_spans(text: str) -> tuple[tuple[int, int], ...]:
    stack: list[tuple[str, int]] = []
    spans: list[tuple[int, int]] = []
    for index, character in enumerate(text):
        if character == '"':
            if stack and stack[-1][0] == '"':
                _, start = stack.pop()
                if not stack:
                    spans.append((start, index + 1))
            else:
                stack.append(('"', index))
            continue
        closer = _QUOTE_OPENERS.get(character)
        if closer is not None:
            stack.append((closer, index))
            continue
        if stack and character == stack[-1][0]:
            _, start = stack.pop()
            if not stack:
                spans.append((start, index + 1))
    return tuple(spans)


def _paragraph_units(
    paragraph: _ParagraphDraft,
    quote_spans: tuple[tuple[int, int], ...],
) -> tuple[StructuralUnit, ...]:
    local_quotes: list[tuple[int, int]] = []
    for start, end in quote_spans:
        clipped_start = max(start, paragraph.document_start)
        clipped_end = min(end, paragraph.document_end)
        if clipped_start < clipped_end:
            local_quotes.append(
                (
                    clipped_start - paragraph.document_start,
                    clipped_end - paragraph.document_start,
                )
            )

    ranges: list[tuple[int, int, StructuralContext]] = []
    cursor = 0
    for index, (start, end) in enumerate(local_quotes):
        if cursor < start:
            gap = paragraph.structural_text[cursor:start]
            bridge = (
                index > 0
                and bool(gap.strip())
                and "\n" not in gap
                and "\r" not in gap
            )
            ranges.append(
                (
                    cursor,
                    start,
                    "quote_bridge_narration" if bridge else "narration",
                )
            )
        ranges.append((start, end, "quoted_dialogue"))
        cursor = end
    if cursor < len(paragraph.structural_text):
        ranges.append((cursor, len(paragraph.structural_text), "narration"))
    if not ranges:
        ranges.append((0, len(paragraph.structural_text), "narration"))

    materialized = [
        (paragraph.structural_text[start:end], context)
        for start, end, context in ranges
        if start < end
    ]
    merged = _merge_formatting(materialized)
    return tuple(
        StructuralUnit(
            unit_id=_digest(
                f"{paragraph.paragraph_id}:{ordinal}:{context}:{text}"
            ),
            text=text,
            context=context,
            speakable=is_speakable_text(text),
        )
        for ordinal, (text, context) in enumerate(merged)
    )


def _merge_formatting(
    ranges: list[tuple[str, StructuralContext]],
) -> list[tuple[str, StructuralContext]]:
    output: list[tuple[str, StructuralContext]] = []
    leading = ""
    for text, context in ranges:
        if is_speakable_text(text):
            combined = f"{leading}{text}"
            leading = ""
            output.append((combined, context))
            continue
        if is_pause_marker(text):
            if leading:
                text = f"{leading}{text}"
                leading = ""
            output.append((text, "pause_marker"))
            continue
        if output and output[-1][1] != "pause_marker":
            previous, previous_context = output[-1]
            output[-1] = (f"{previous}{text}", previous_context)
        elif output and output[-1][1] == "pause_marker":
            previous, _ = output[-1]
            output[-1] = (f"{previous}{text}", "pause_marker")
        else:
            leading += text
    if leading:
        if output:
            previous, previous_context = output[-1]
            output[-1] = (f"{previous}{leading}", previous_context)
        else:
            output.append((leading, "pause_marker"))
    return output


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

