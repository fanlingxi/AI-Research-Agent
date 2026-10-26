"""Deterministic, quote-local numeric support (not semantic entailment).

Only Arabic numbers in assertions/scopes are audited. Chinese number words are
not checked in v1; English zero--twenty are recognized only on the support side.
Identifier/citation/structural numbers are excluded on BOTH sides. Units other
than percent, semantic operand relationships, and attribution of equal values
are not inferred. Derived values never use arbitrary pairs from source quotes.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from itertools import permutations

RULE_VERSION = "numeric-support-v1"

_NUMBER = r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?|[+-]?\.[0-9]+"
_NUMBERS = re.compile(_NUMBER)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_@]+(?:[.\-/][A-Za-z0-9_@]+)*")
_STRUCTURE_PREFIX = (
    r"(?:表|图|第|公式|式|章|节|页|附录|算法|"
    r"\b(?:Tables?|Fig(?:ure)?s?\.?|Sections?|Eq(?:uation)?s?\.?|Appendix|Algorithm))"
)
_STRUCTURE = re.compile(_STRUCTURE_PREFIX + r"\s*\(?[A-Z]?[0-9]+(?:\.[0-9]+)*(?:[a-z])?\)?",
                        re.IGNORECASE)
_CITATION = re.compile(r"\[\s*[0-9]+(?:\s*[,;\-–]\s*[0-9]+)*\s*\]")
_YEAR = re.compile(r"(?:\(\s*(?:19|20)[0-9]{2}[a-z]?\s*\)|"
                   r"\bet\s+al\.,?\s*\(?\s*(?:19|20)[0-9]{2}[a-z]?)", re.IGNORECASE)
_WORDS = "zero one two three four five six seven eight nine ten eleven twelve thirteen " \
         "fourteen fifteen sixteen seventeen eighteen nineteen twenty"
_WORD_VALUES = {word: Decimal(i) for i, word in enumerate(_WORDS.split())}
_WORD_PATTERN = re.compile(r"\b(?:" + "|".join(_WORD_VALUES) + r")\b", re.IGNORECASE)
_WORD_REFERENCE = re.compile(_STRUCTURE_PREFIX + r"\s+(?:" + "|".join(_WORD_VALUES) + r")\b",
                             re.IGNORECASE)


@dataclass(frozen=True)
class Number:
    text: str
    value: Decimal
    unit: str


def numbers(text: str, *, support: bool = False) -> list[Number]:
    """Normalize typography and extract only non-exempt numerical quantities."""
    text = unicodedata.normalize("NFKC", text).replace("−", "-").replace("–", "-")
    references = [m.span() for pattern in (_STRUCTURE, _CITATION, _YEAR, _WORD_REFERENCE)
                  for m in pattern.finditer(text)]
    excluded = list(references)
    excluded.extend(m.span() for m in _IDENTIFIER.finditer(text)
                    if re.search(r"[A-Za-z@]", m.group()))

    def exempt(start, end):
        return any(start < b and end > a for a, b in excluded)

    result = []
    for match in _NUMBERS.finditer(text):
        if exempt(*match.span()):
            continue
        suffix = text[match.end():]
        unit = ("percent" if re.match(r"\s*%", suffix) else
                "points" if re.match(r"\s*(?:个\s*)?百分点", suffix) else "")
        token = match.group()
        # A hyphen between quantities is a range delimiter, not a unary minus.
        if token.startswith("-") and match.start() and text[match.start() - 1] in "0123456789%":
            token = token[1:]
        result.append(Number(token, Decimal(token.replace(",", "")), unit))
    if support:
        # Plain English words were marked as identifiers above; only skip structural
        # references here, and require a standalone word (never part of a model ID).
        for match in _WORD_PATTERN.finditer(text):
            if (any(match.start() < b and match.end() > a for a, b in references)
                    or (match.start() and text[match.start() - 1] in "-@_")
                    or (match.end() < len(text) and text[match.end()] in "-@_")):
                continue
            result.append(Number(match.group(), _WORD_VALUES[match.group().lower()], ""))
    return result


def unsupported_numbers(assertion: str, scope: str, quotes: list[str]) -> list[dict]:
    """Return unsupported occurrences, with their field and displayed number.

    Only directly supported conclusion operands seed derivation; derived values
    cannot bootstrap other unsupported numbers. Percent scaling is unit-gated.
    All arithmetic uses Decimal and ROUND_HALF_UP at the target display precision.
    """
    claims = [(field, number) for field, text in (("assertion", assertion), ("scope", scope))
              for number in numbers(text)]
    evidence = {n.value for quote in quotes for n in numbers(quote, support=True)}
    # Precision covers even long integers accepted by the bounded report schema.
    with localcontext() as context:
        context.prec = max([50] + [
            (len(v.as_tuple().digits) + abs(v.as_tuple().exponent)) * 4 + 20
            for v in [n.value for _, n in claims] + list(evidence)
        ])
        direct = [n.value in evidence or bool(n.unit and n.value in {v * 100 for v in evidence})
                  for _, n in claims]
        operands = [n.value for (_, n), supported in zip(claims, direct, strict=True)
                    if supported]
        pairs = list(permutations(operands, 2))
        missing = []
        for (field, number), supported in zip(claims, direct, strict=True):
            if supported:
                continue
            candidates = []
            for a, b in pairs:
                candidates.extend((a - b, a + b))
                if b:
                    candidates.append(a / b)
                    if number.unit == "percent":
                        candidates.append((a - b) / b * 100)
                if number.unit == "points" and 0 <= a <= 1 and 0 <= b <= 1:
                    candidates.append((a - b) * 100)
            precision = Decimal(1).scaleb(number.value.as_tuple().exponent)
            if not any(v.quantize(precision, rounding=ROUND_HALF_UP) == number.value
                       for v in candidates):
                missing.append({"field": field, "text": number.text,
                                "value": str(number.value), "unit": number.unit,
                                "reason": "not_in_support_or_supported_derivation"})
        return missing
