"""Helpers compartilhados para localizar evidências no texto extraído."""

from collections.abc import Mapping


def normalize_spaces(text: str) -> str:
    return " ".join(text.split())


def evidence_is_on_page(evidence: str, page: int, page_texts: Mapping[int, str]) -> bool:
    page_text = page_texts.get(page)
    return page_text is not None and normalize_spaces(evidence) in normalize_spaces(page_text)
