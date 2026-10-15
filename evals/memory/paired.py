"""Version-neutral comparisons; unavailable fields and missing runs are not misses."""
import hashlib
import json


def case_hash(case):
    return hashlib.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def review_template(manifest, cases):
    return {"dataset_sha256": manifest["sha256"], "independent_human_review": False,
            "reviewer": "", "reviewed_at": "", "cases": [
                {"id": case["id"], "case_sha256": case_hash(case), "decision": "pending"} for case in cases]}


def validate_review(review, manifest, cases):
    if (review.get("dataset_sha256") != manifest["sha256"]
            or review.get("independent_human_review") is not True
            or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()
            or not review.get("reviewed_at")):
        raise ValueError("Independent human label review is required before live comparison")
    rows = review.get("cases", [])
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate review rows")
    by_id = {row["id"]: row for row in rows}
    for case in cases:
        row = by_id.get(case["id"], {})
        if row.get("case_sha256") != case_hash(case) or row.get("decision") != "accepted":
            raise ValueError("Selected labels must be reviewed without changing the frozen cases")


def equipment_value(raw, modern):
    raw = raw or {}
    memory = (raw.get("equipment_memory") or {}).get("value") if modern else None
    training = (raw.get("training") or {}).get("equipment")
    # Conflicting fields must not silently select the more convenient answer.
    if memory and training and memory != training:
        return None
    return memory or training


def compare(cases, versions, *, live, review_verified):
    labels = ("baseline", "candidate")
    planned_ids = {case["id"] for case in cases}
    if not cases or len(planned_ids) != len(cases):
        raise ValueError("Nonempty unique cases are required")
    for version in versions.values():
        ids = [row["id"] for row in version.get("cases", [])]
        if len(set(ids)) != len(ids) or set(ids) - planned_ids:
            raise ValueError("Duplicate or unplanned case results")
    indexed = {label: {row["id"]: row for row in versions.get(label, {}).get("cases", [])} for label in labels}
    missing = {label: [case["id"] for case in cases if case["id"] not in indexed[label]] for label in labels}
    invalid = {label: [key for key, row in indexed[label].items()
                      if any("error_type" in step for step in row["steps"])] for label in labels}
    complete = all(versions.get(label, {}).get("status") == "completed"
                   and versions[label].get("source_unchanged")
                   and not missing[label] and not invalid[label] for label in labels)
    quality = bool(live and review_verified and complete)
    metrics, excluded, pairs = {}, [], []
    for case in cases:
        key = case["id"]
        if any(key not in indexed[label] or key in invalid[label] for label in labels):
            continue
        left, right = (indexed[label][key]["steps"] for label in labels)
        if len(left) != len(right) or any(a["op"] != b["op"] for a, b in zip(left, right)):
            raise ValueError("Case steps are not aligned")
        pairs.append(key)
        for number, (a, b) in enumerate(zip(left, right)):
            ca, cb = a.get("checks", {}), b.get("checks", {})
            for metric in sorted(ca.keys() | cb.keys()):
                if metric not in ca or metric not in cb:
                    excluded.append({"id": key, "step": number, "metric": metric, "reason": "unsupported_on_one_side"})
                    continue
                if not isinstance(ca[metric], bool) or not isinstance(cb[metric], bool):
                    raise ValueError("Scores must be explicit booleans")
                score = metrics.setdefault(metric, {"baseline_passed": 0, "candidate_passed": 0, "total": 0,
                                                     "gains": 0, "regressions": 0})
                score["total"] += 1
                score["baseline_passed"] += ca[metric]
                score["candidate_passed"] += cb[metric]
                score["gains"] += not ca[metric] and cb[metric]
                score["regressions"] += ca[metric] and not cb[metric]
    for score in metrics.values():
        score.update(baseline_rate=score["baseline_passed"] / score["total"] if quality else None,
                     candidate_rate=score["candidate_passed"] / score["total"] if quality else None,
                     delta_percentage_points=100 * (score["candidate_passed"] - score["baseline_passed"]) / score["total"] if quality else None)
    return {"quality_evaluated": quality, "complete": complete, "planned_cases": len(cases),
            "paired_case_ids": pairs, "not_run": missing, "invalid_cases": invalid, "metrics": metrics,
            "excluded_checks": excluded, "claim": "reviewed_authored_suite_only" if quality else "no_quality_claim"}
