"""Scoped recall of confirmed past conversation messages.

History is evidence of what a user said, never an active plan constraint or a
replacement for the current profile and actual records.
"""
import json
import re
import unicodedata

import jieba

from rag.rag_service import STOP_WORDS


PAST_REFERENCE = re.compile(r"上次|之前|以前|前几天|前一次|曾经|说过|提过|记得")
FILLER = {"的", "了", "我", "你", "他", "她", "是", "吗", "呢", "在", "和", "也", "想", "说", "过", "这", "那", "一个", "那个"}
_TOKENIZER = jieba.Tokenizer()


def confirmed_equipment(message, *, allow_temporary=False):
    """Only an unambiguous, durable equipment statement may update the profile.

    The caller must additionally require the user's explicit review confirmation.
    A day/session-limited statement remains in that conversation alone.
    """
    if not isinstance(message, str) or (not allow_temporary and re.search(r"今天|本次|这次|今晚|明天|临时|暂时|接下来", message)):
        return None
    value = message.strip().rstrip("。！! ")
    if allow_temporary:
        value = re.sub(r"^(?:今天|本次|这次|今晚|明天|临时|暂时)", "", value)
    value = re.sub(r"^(?:我)?(?:平时|通常|一般)(?=在家|家里)", "", value)
    from services.training_context import training_command
    change = training_command(value, continuing=True)
    return change.get("equipment") if change else None


def _terms(value):
    text = unicodedata.normalize("NFKC", value).lower()
    return {word for word in _TOKENIZER.cut_for_search(text)
            if re.fullmatch(r"[a-z0-9\u3400-\u9fff]+", word)
            and word not in STOP_WORDS and word not in FILLER
            and not PAST_REFERENCE.fullmatch(word)}


def confirmation_revisions(response):
    if (response.get("source") not in ("reviewed_understanding", "applied_understanding")
            or response.get("status") not in ("reviewed", "applied")):
        return {}
    reviewed = response.get("reviewed_versions", [])
    return {version: response.get("equipment_memory_claims", {}).get(str(version),
                    response.get("equipment_memory_revisions", {}).get(str(version)))
            for version in reviewed if isinstance(version, int)}


def current_equipment_evidence(message, bound_revision, profile, revision):
    if isinstance(bound_revision, dict):
        from services.equipment_memory import scoped_evidence
        return scoped_evidence(bound_revision, profile, revision)
    claim = confirmed_equipment(message, allow_temporary=True)
    if claim is None:
        return True
    # Legacy evidence has no binding; once a versioned fact exists it needs a
    # fresh confirmation. Equal text must not revive superseded evidence.
    return ((bound_revision == revision or bound_revision is None and revision == 0)
            and (profile is None or claim == profile))


def recall_confirmed_history(connection, user_id, query, *, before_day=None,
                             before_turn_rowid=None, exclude_conversation_id=None, limit=3):
    """Return a bounded, user-owned historical excerpt with its source IDs.

    The existing 50-conversation/40-turn application bounds make this scan
    finite. A deleted conversation disappears through the existing cascade.
    """
    if not isinstance(query, str) or not 1 <= len(query) <= 2000 or not 1 <= limit <= 5:
        raise ValueError("Invalid memory search")
    terms = _terms(query)
    if not terms:
        return []
    rows = connection.execute(
        "SELECT c.id AS conversation_id,c.day,t.version,t.message,t.response,t.created_at "
        "FROM coach_conversations c JOIN coach_turns t ON t.conversation_id=c.id "
        "WHERE c.user_id=? AND (? IS NULL OR c.day<=?) AND (? IS NULL OR t.rowid<?) "
        "AND (? IS NULL OR c.id<>?) "
        "ORDER BY c.day DESC,c.updated_at DESC,t.version DESC LIMIT 2000",
        (user_id, before_day, before_day, before_turn_rowid, before_turn_rowid,
         exclude_conversation_id, exclude_conversation_id),
    ).fetchall()
    confirmed = {}
    decoded = []
    for row in rows:
        response = json.loads(row["response"])
        decoded.append((row, response))
        if response.get("source") in ("reviewed_understanding", "applied_understanding") and response.get("status") in ("reviewed", "applied"):
            # Rows are newest first. A newer confirmation binds each source once.
            target = confirmed.setdefault(row["conversation_id"], {})
            for version, revision in confirmation_revisions(response).items():
                target.setdefault(version, revision)
    matches = []
    profile_row = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    saved_profile = json.loads(profile_row[0]) if profile_row else {}
    saved_equipment = saved_profile.get("equipment")
    from services.profile_facts import equipment_revision
    revision = equipment_revision(connection, user_id)
    for row, _ in decoded:
        if row["version"] not in confirmed.get(row["conversation_id"], ()):
            continue
        if not current_equipment_evidence(row["message"], confirmed[row["conversation_id"]][row["version"]],
                                          saved_equipment, revision):
            continue
        overlap = terms & _terms(row["message"])
        if not overlap:
            continue
        matches.append((len(overlap) / len(terms), len(overlap), row))
    # Stable relevance first; for equal relevance prefer the newest evidence.
    matches.sort(key=lambda item: (item[0], item[1], item[2]["day"], item[2]["created_at"]), reverse=True)
    return [{"conversation_id": row["conversation_id"], "turn_version": row["version"],
             "day": row["day"], "excerpt": row["message"][:300],
             "status": "historical_confirmed_message"} for _, _, row in matches[:limit]]
