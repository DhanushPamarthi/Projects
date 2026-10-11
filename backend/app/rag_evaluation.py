"""Deterministic grading helpers for retrieval evidence (not generated answers)."""

import re


def _normalize(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def grade_retrieval(question, retrieval):
    """Grade expected evidence and citation provenance for one golden query.

    Scores measure retrieval coverage/precision only. They are not a calibrated
    probability that a generated answer is true.
    """
    hits = retrieval.get("hits") or []
    expect_no_evidence = bool(question.expect_no_evidence)
    evidence = [_normalize(value) for value in question.expected_evidence if _normalize(value)]
    hit_text = [_normalize(hit.get("chunk_text")) for hit in hits]
    found = {phrase: any(phrase in text for text in hit_text) for phrase in evidence}
    no_evidence_correct = not hits if expect_no_evidence else None
    evidence_recall = (1.0 if no_evidence_correct else 0.0) if expect_no_evidence else (
        sum(found.values()) / len(found) if found else (1.0 if hits else 0.0)
    )
    relevant_hits = sum(any(phrase in text for phrase in evidence) for text in hit_text) if evidence else len(hits)
    top_hit_relevant = (not hits) if expect_no_evidence else (
        bool(hits) and (not evidence or any(phrase in hit_text[0] for phrase in evidence))
    )
    retrieval_precision = (1.0 if not hits else 0.0) if expect_no_evidence else (
        relevant_hits / len(hits) if hits else 0.0
    )
    expected_types = set(question.expected_source_types)
    actual_types = {hit.get("source_type") for hit in hits}
    source_type_coverage = len(expected_types & actual_types) / len(expected_types) if expected_types else 1.0
    citation_integrity = (not hits) if expect_no_evidence else bool(hits) and all(
        bool(hit.get("chunk_id")) and bool(hit.get("source_reference")) and bool(hit.get("content_hash"))
        for hit in hits
    )
    passed = no_evidence_correct if expect_no_evidence else (
        bool(hits) and evidence_recall == 1.0 and top_hit_relevant and source_type_coverage == 1.0 and citation_integrity
    )
    relevance = [any(phrase in text for phrase in evidence) for text in hit_text] if evidence else [bool(text) for text in hit_text]
    first_relevant_rank = next((rank for rank, relevant in enumerate(relevance, start=1) if relevant), None)
    reciprocal_rank = 1.0 / first_relevant_rank if first_relevant_rank else 0.0
    dcg = sum((1.0 / (rank + 1)) for rank, relevant in enumerate(relevance) if relevant)
    ideal_relevant = min(sum(relevance), len(relevance))
    ideal_dcg = sum(1.0 / (rank + 1) for rank in range(ideal_relevant))
    ndcg_at_k = dcg / ideal_dcg if ideal_dcg else 0.0
    return {
        "passed": passed,
        "expect_no_evidence": expect_no_evidence,
        "no_evidence_correct": no_evidence_correct,
        "evidence_recall": round(evidence_recall, 4),
        "retrieval_precision": round(retrieval_precision, 4),
        "reciprocal_rank": round(reciprocal_rank, 4),
        "ndcg_at_k": round(ndcg_at_k, 4),
        "top_hit_relevant": top_hit_relevant,
        "source_type_coverage": round(source_type_coverage, 4),
        "citation_integrity": citation_integrity,
        "expected_evidence_hits": found,
        "hit_count": len(hits),
        "relevant_hit_count": relevant_hits,
        "expected_source_types": sorted(expected_types),
        "actual_source_types": sorted(value for value in actual_types if value),
        "retrieval_score_is_confidence": False,
    }
