"""Helpers compartilhados para localizar evidências no texto extraído."""

import re
import unicodedata
from collections.abc import Mapping


def normalize_spaces(text: str) -> str:
    return " ".join(text.split())


def evidence_is_on_page(evidence: str, page: int, page_texts: Mapping[int, str]) -> bool:
    page_text = page_texts.get(page)
    return page_text is not None and normalize_spaces(evidence) in normalize_spaces(page_text)


_DOCUMENT_NUMBER_LABEL = re.compile(
    r"^(?:"
    r"documento(?:\s+(?:comercial|fiscal))?(?:\s+(?:numero|n))?"
    r"|numero\s+(?:do|de)\s+documento"
    r"|n(?:umero|o)\s+(?:"
    r"(?:(?:do|de)\s+)?documento"
    r"|(?:(?:da|de)\s+)?(?:nota\s+fiscal|nf-?e?)"
    r")"
    r"|numero\s+(?:da|de)\s+(?:nota\s+fiscal|nf-?e?)"
    r"|(?:nota\s+fiscal|nf-?e?)(?:\s+(?:numero|no|n))?"
    r"|(?:invoice|document)\s+(?:number|no)"
    r"|invoice"
    r")\s*(?::|#|=|-)\s*(?P<identified>.+)$"
)


def _normalize_label_text(text: str) -> str:
    text = text.replace("º", "o").replace("°", "o")
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    normalized = normalize_spaces(unaccented)
    return re.sub(r"\bno\.(?=\s*[:#=])", "no", normalized)


def document_number_evidence_identifies_value(value: str | None, evidence: str | None) -> bool:
    """Require the cited evidence itself to label its value as a document number.

    A matching substring alone is insufficient: the same identifier can appear
    under distinct labels such as an invoice number and a purchase-order number.
    """
    if not value or not evidence:
        return False

    normalized_value = _normalize_label_text(value)
    if not normalized_value:
        return False

    for evidence_line in evidence.splitlines():
        normalized_line = _normalize_label_text(evidence_line)
        match = _DOCUMENT_NUMBER_LABEL.match(normalized_line)
        if match is None:
            continue
        identified = match.group("identified")
        if not identified.startswith(normalized_value):
            continue
        suffix = identified[len(normalized_value) :]
        if not suffix:
            return True
        if suffix[0] in " ;|,()":
            return True
        if suffix[0] == "." and not suffix[1:].strip(" ."):
            return True
    return False
