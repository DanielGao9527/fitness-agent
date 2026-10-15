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
    strip_releases_v21(connection)
    for name in ("knowledge_candidate_review_change", "knowledge_answer_review_change", "knowledge_reviewer_revoke", "knowledge_reviewer_delete", "knowledge_reviewer_account_delete"):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("knowledge_review_events", "knowledge_review_gates", "knowledge_submissions", "knowledge_reviewers"):
        connection.execute(f"DROP TABLE IF EXISTS {name}")


def strip_releases_v21(connection):
    strip_publications_v22(connection)
    for name in ("knowledge_release_event", "knowledge_release_submission_change", "knowledge_release_submission_delete",
                 "knowledge_release_reviewer_change", "knowledge_release_reviewer_delete", "knowledge_release_account_delete"):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("knowledge_release_events", "knowledge_release_members", "knowledge_releases"):
        connection.execute(f"DROP TABLE IF EXISTS {name}")


def strip_publications_v22(connection):
    strip_training_records_v23(connection)
    for name in ("knowledge_live_event", "knowledge_publication_registration", "knowledge_publication_head",
                 "knowledge_publication_delete", "knowledge_publication_reviewer_update",
                 "knowledge_publication_reviewer_delete", "knowledge_publication_account_delete"):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("knowledge_live_events", "knowledge_live", "knowledge_publications"):
        connection.execute(f"DROP TABLE IF EXISTS {name}")


def strip_training_records_v23(connection):
    for name in ("training_record_insert", "training_record_delete", "training_record_update"):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    connection.execute("DROP TABLE IF EXISTS training_record_revisions")
