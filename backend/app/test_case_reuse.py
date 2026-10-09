"""Project-scoped case matching. Scores aid review; they never establish evidence."""

import re
from difflib import SequenceMatcher


def requirement_text(requirement):
    parts = [requirement.get("title", ""), requirement.get("statement", "")]
    parts.extend(" ".join(str(item.get(key) or "") for key in ("given", "when", "then"))
                 for item in requirement.get("acceptance_criteria", []) if isinstance(item, dict))
    parts.extend(str(item.get("text") or "") for item in requirement.get("business_rules", []) if isinstance(item, dict))
    return " ".join(str(part) for part in parts)


def normalized(text):
    return " ".join(re.findall(r"[a-z0-9]+", str(text).casefold()))


def similarity(left, right):
    a, b = normalized(left), normalized(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_terms, b_terms = set(a.split()), set(b.split())
    overlap = len(a_terms & b_terms) / len(a_terms | b_terms)
    return round(0.55 * overlap + 0.45 * SequenceMatcher(None, a, b).ratio(), 3)


def case_mapping(case, requirement_id):
    """A copied case must retain a single, current FR owner."""
    return {**case, "requirement_id": requirement_id}
