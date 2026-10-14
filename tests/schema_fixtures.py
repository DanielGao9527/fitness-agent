"""Remove later additions when constructing genuine older-schema fixtures."""


def strip_memory_v18(connection):
    strip_governance_v19(connection)
    connection.execute("DROP TRIGGER IF EXISTS coach_memory_source_change")
    connection.execute("DROP TRIGGER IF EXISTS coach_memory_source_delete")
    connection.execute("DROP TABLE coach_memory_revisions")
    connection.execute("DROP INDEX coach_memory_communication_slot")
    for field in ("source_mode", "source_revoked", "source_text_hash", "source_turn_version", "source_conversation_id", "category"):
        connection.execute(f"ALTER TABLE coach_memory_entries DROP COLUMN {field}")


def strip_governance_v19(connection):
    strip_review_v20(connection)
    for name in ("knowledge_candidate_events", "knowledge_candidates", "knowledge_feedback"):
        connection.execute(f"DROP TABLE IF EXISTS {name}")


def strip_review_v20(connection):
    for name in ("knowledge_candidate_review_change", "knowledge_answer_review_change", "knowledge_reviewer_revoke", "knowledge_reviewer_delete", "knowledge_reviewer_account_delete"):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("knowledge_review_events", "knowledge_review_gates", "knowledge_submissions", "knowledge_reviewers"):
        connection.execute(f"DROP TABLE IF EXISTS {name}")
