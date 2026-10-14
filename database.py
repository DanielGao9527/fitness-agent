import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    day TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(user_id, client_id)
);
CREATE INDEX IF NOT EXISTS meals_user_day ON meals(user_id, day);
CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    day TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(user_id, client_id)
);
CREATE INDEX IF NOT EXISTS workouts_user_day ON workouts(user_id, day);
"""

DRAFT_SCHEMA = """
CREATE TABLE meal_drafts (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    initial_payload TEXT NOT NULL,
    day TEXT NOT NULL,
    meal_type TEXT NOT NULL,
    text TEXT NOT NULL,
    payload TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK(status IN ('needs_input', 'ready', 'committed', 'cancelled')),
    confirmation_id TEXT,
    committed_version INTEGER,
    result TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, client_id)
);
CREATE INDEX meal_drafts_user_status ON meal_drafts(user_id, status);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(21):
                raise RuntimeError("Unsupported database schema version")
            connection.execute("PRAGMA journal_mode = WAL")
            if version == 1:
                backup_path = self.path.with_name(self.path.name + ".pre-v2.bak")
                if not backup_path.exists():
                    with sqlite3.connect(backup_path) as backup:
                        connection.backup(backup)
            if version == 0:
                connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nPRAGMA user_version = 1;\nCOMMIT;")
            if version < 2:
                connection.executescript("BEGIN IMMEDIATE;\n" + DRAFT_SCHEMA + "\nPRAGMA user_version = 2;\nCOMMIT;")
            if version < 3:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v3.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE ai_usage (
                        day TEXT NOT NULL,
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        calls INTEGER NOT NULL,
                        PRIMARY KEY(day, user_id)
                    );
                    PRAGMA user_version = 3;
                    COMMIT;
                """)
            if version < 4:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v4.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE nutrition_previews (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        client_id TEXT NOT NULL,
                        input_payload TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        UNIQUE(user_id, client_id)
                    );
                    CREATE INDEX nutrition_previews_created ON nutrition_previews(created_at);
                    PRAGMA user_version = 4;
                    COMMIT;
                """)
            if version < 5:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v5.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE workout_previews (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        client_id TEXT NOT NULL,
                        input_payload TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        UNIQUE(user_id, client_id)
                    );
                    CREATE INDEX workout_previews_created ON workout_previews(created_at);
                    PRAGMA user_version = 5;
                    COMMIT;
                """)
            if version < 6:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v6.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE meal_plans (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        client_id TEXT NOT NULL,
                        day TEXT NOT NULL,
                        meal_type TEXT NOT NULL,
                        input_payload TEXT NOT NULL,
                        context_hash TEXT NOT NULL,
                        payload TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL CHECK(status IN ('generating', 'failed', 'draft', 'accepted')),
                        version INTEGER,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        accepted_at TEXT,
                        UNIQUE(user_id, client_id),
                        UNIQUE(user_id, day, meal_type, version)
                    );
                    CREATE INDEX meal_plans_user_day ON meal_plans(user_id, day);
                    PRAGMA user_version = 6;
                    COMMIT;
                """)
            if version < 7:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v7.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE training_plans (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        client_id TEXT NOT NULL,
                        day TEXT NOT NULL,
                        input_payload TEXT NOT NULL,
                        context_hash TEXT NOT NULL,
                        payload TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL CHECK(status IN ('generating', 'failed', 'draft', 'accepted')),
                        version INTEGER,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        accepted_at TEXT,
                        UNIQUE(user_id, client_id),
                        UNIQUE(user_id, day, version)
                    );
                    CREATE INDEX training_plans_user_day ON training_plans(user_id, day);
                    PRAGMA user_version = 7;
                    COMMIT;
                """)
            if version < 8:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v8.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE coach_conversations (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        client_id TEXT NOT NULL,
                        day TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '新对话',
                        version INTEGER NOT NULL DEFAULT 0,
                        intent TEXT NOT NULL DEFAULT 'unknown',
                        pending INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, client_id)
                    );
                    CREATE INDEX coach_conversations_user ON coach_conversations(user_id);
                    CREATE TABLE coach_turns (
                        id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL REFERENCES coach_conversations(id) ON DELETE CASCADE,
                        client_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        message TEXT NOT NULL,
                        response TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(conversation_id, client_id),
                        UNIQUE(conversation_id, version)
                    );
                    PRAGMA user_version = 8;
                    COMMIT;
                """)
            if version < 9:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v9.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE meal_consents (
                        session_hash TEXT PRIMARY KEY REFERENCES sessions(token_hash) ON DELETE CASCADE,
                        context_hash TEXT NOT NULL,
                        confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    PRAGMA user_version = 9;
                    COMMIT;
                """)
            if version < 10:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v10.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE coach_reviews (
                        id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL REFERENCES coach_conversations(id) ON DELETE CASCADE,
                        client_id TEXT NOT NULL,
                        base_version INTEGER NOT NULL,
                        context_hash TEXT NOT NULL,
                        payload TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL CHECK(status IN ('generating','ready','needs_input','failed','confirmed')),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(conversation_id, client_id)
                    );
                    CREATE INDEX coach_reviews_conversation ON coach_reviews(conversation_id);
                    PRAGMA user_version = 10;
                    COMMIT;
                """)
            if version < 11:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v11.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE intake_targets (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        client_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        effective_from TEXT NOT NULL,
                        kcal INTEGER CHECK(kcal IS NULL OR kcal BETWEEN 1000 AND 5000),
                        source TEXT NOT NULL,
                        context_hash TEXT NOT NULL,
                        input_payload TEXT NOT NULL,
                        confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, client_id),
                        UNIQUE(user_id, version)
                    );
                    CREATE INDEX intake_targets_user_day ON intake_targets(user_id, effective_from, version);
                    PRAGMA user_version = 11;
                    COMMIT;
                """)
            if version < 12:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v12.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE meal_intake_reviews (
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        day TEXT NOT NULL,
                        version INTEGER NOT NULL CHECK(version >= 1),
                        context_hash TEXT NOT NULL,
                        record_state TEXT CHECK(record_state IS NULL OR record_state IN ('complete','none_yet')),
                        confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(user_id, day)
                    );
                    PRAGMA user_version = 12;
                    COMMIT;
                """)
            if version < 13:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v13.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE body_measurements (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        client_id TEXT NOT NULL,
                        day TEXT NOT NULL,
                        initial_payload TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                        last_request TEXT,
                        deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, client_id)
                    );
                    CREATE UNIQUE INDEX body_measurements_user_day
                        ON body_measurements(user_id,day) WHERE deleted=0;
                    PRAGMA user_version = 13;
                    COMMIT;
                """)
            if version < 14:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v14.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE guest_accounts (
                        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                        tab_hash TEXT NOT NULL,
                        client_hash TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL
                    );
                    CREATE INDEX guest_accounts_expiry ON guest_accounts(expires_at);
                    CREATE TABLE guest_usage (
                        day TEXT NOT NULL,
                        client_hash TEXT NOT NULL,
                        calls INTEGER NOT NULL CHECK(calls >= 0),
                        PRIMARY KEY(day, client_hash)
                    );
                    PRAGMA user_version = 14;
                    COMMIT;
                """)
            if version < 15:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v15.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE coach_memory_entries (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL CHECK(kind IN ('user_preference','assistant_note')),
                        content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 200),
                        version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX coach_memory_entries_user ON coach_memory_entries(user_id, created_at, id);
                    CREATE TABLE coach_memory_digests (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        conversation_id TEXT NOT NULL REFERENCES coach_conversations(id) ON DELETE CASCADE,
                        source_versions TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 480),
                        status TEXT NOT NULL CHECK(status IN ('pending','approved')),
                        version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, conversation_id)
                    );
                    CREATE INDEX coach_memory_digests_user ON coach_memory_digests(user_id, status, created_at);
                    PRAGMA user_version = 15;
                    COMMIT;
                """)
            if version < 16:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v16.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE profile_fact_events (
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        field TEXT NOT NULL CHECK(field IN ('equipment')),
                        revision INTEGER NOT NULL CHECK(revision >= 1),
                        action TEXT NOT NULL CHECK(action IN ('set','clear')),
                        source TEXT NOT NULL CHECK(source IN ('legacy_profile','profile_edit','conversation_review')),
                        conversation_id TEXT REFERENCES coach_conversations(id) ON DELETE SET NULL,
                        turn_version INTEGER CHECK(turn_version >= 1),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(user_id,field,revision)
                    );
                    CREATE INDEX profile_fact_events_conversation ON profile_fact_events(conversation_id);
                    INSERT INTO profile_fact_events(user_id,field,revision,action,source)
                        SELECT user_id,'equipment',1,
                               CASE WHEN json_extract(payload,'$.equipment')='' THEN 'clear' ELSE 'set' END,
                               'legacy_profile'
                        FROM profiles WHERE json_type(payload,'$.equipment')='text';
                    PRAGMA user_version = 16;
                    COMMIT;
                """)
            if version < 17:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v17.bak")
                    if not backup_path.exists():
                        with sqlite3.connect(backup_path) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE coach_day_equipment (
                        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                        value TEXT NOT NULL CHECK(length(value) <= 300),
                        valid_day TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK(revision >= 1),
                        profile_revision INTEGER NOT NULL CHECK(profile_revision >= 0),
                        conversation_id TEXT REFERENCES coach_conversations(id) ON DELETE SET NULL,
                        turn_version INTEGER NOT NULL CHECK(turn_version >= 1),
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    PRAGMA user_version = 17;
                    COMMIT;
                """)
            if version < 18:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v18.bak")
                    if not backup_path.exists():
                        with closing(sqlite3.connect(backup_path)) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE coach_memory_entries ADD COLUMN category TEXT NOT NULL DEFAULT 'general_note'
                        CHECK(category IN ('general_note','communication_preference','personal_term'));
                    ALTER TABLE coach_memory_entries ADD COLUMN source_conversation_id TEXT
                        REFERENCES coach_conversations(id) ON DELETE SET NULL;
                    ALTER TABLE coach_memory_entries ADD COLUMN source_turn_version INTEGER
                        CHECK(source_turn_version IS NULL OR source_turn_version >= 1);
                    ALTER TABLE coach_memory_entries ADD COLUMN source_text_hash TEXT
                        CHECK(source_text_hash IS NULL OR length(source_text_hash) = 64);
                    ALTER TABLE coach_memory_entries ADD COLUMN source_revoked INTEGER NOT NULL DEFAULT 0
                        CHECK(source_revoked IN (0,1));
                    ALTER TABLE coach_memory_entries ADD COLUMN source_mode TEXT NOT NULL DEFAULT 'manual'
                        CHECK(source_mode IN ('manual','conversation'))
                        CHECK((source_mode='manual' AND source_revoked=0 AND source_conversation_id IS NULL
                               AND source_turn_version IS NULL AND source_text_hash IS NULL)
                              OR (source_mode='conversation' AND source_turn_version IS NOT NULL
                                  AND source_text_hash IS NOT NULL));
                    CREATE UNIQUE INDEX coach_memory_communication_slot ON coach_memory_entries(user_id)
                        WHERE category='communication_preference';
                    CREATE TABLE coach_memory_revisions (
                        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                        revision INTEGER NOT NULL CHECK(revision >= 1)
                    );
                    INSERT INTO coach_memory_revisions(user_id,revision)
                        SELECT DISTINCT user_id,1 FROM coach_memory_entries;
                    CREATE TRIGGER coach_memory_source_change AFTER UPDATE OF message ON coach_turns
                        WHEN OLD.message IS NOT NEW.message AND EXISTS (
                            SELECT 1 FROM coach_memory_entries WHERE source_conversation_id=OLD.conversation_id
                            AND source_turn_version=OLD.version AND source_mode='conversation')
                    BEGIN
                        UPDATE coach_memory_revisions SET revision=revision+1 WHERE user_id IN (
                            SELECT user_id FROM coach_memory_entries WHERE source_conversation_id=OLD.conversation_id
                            AND source_turn_version=OLD.version AND source_mode='conversation');
                        UPDATE coach_memory_entries SET source_revoked=1,version=version+1,updated_at=CURRENT_TIMESTAMP
                            WHERE source_conversation_id=OLD.conversation_id AND source_turn_version=OLD.version
                            AND source_mode='conversation';
                    END;
                    CREATE TRIGGER coach_memory_source_delete BEFORE DELETE ON coach_conversations
                        WHEN EXISTS (SELECT 1 FROM coach_memory_entries WHERE source_conversation_id=OLD.id)
                    BEGIN
                        UPDATE coach_memory_revisions SET revision=revision+1 WHERE user_id IN (
                            SELECT user_id FROM coach_memory_entries WHERE source_conversation_id=OLD.id);
                        UPDATE coach_memory_entries SET source_revoked=1,version=version+1,updated_at=CURRENT_TIMESTAMP
                            WHERE source_conversation_id=OLD.id;
                    END;
                    PRAGMA user_version = 18;
                    COMMIT;
                """)
            if version < 19:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v19.bak")
                    if not backup_path.exists():
                        with closing(sqlite3.connect(backup_path)) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE knowledge_feedback (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        client_id TEXT NOT NULL,
                        request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                        conversation_id TEXT NOT NULL,
                        turn_version INTEGER NOT NULL CHECK(turn_version >= 1),
                        answer_hash TEXT NOT NULL CHECK(length(answer_hash)=64),
                        source_snapshot TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('personal_correction','knowledge_issue')),
                        comment TEXT NOT NULL CHECK(length(comment) BETWEEN 1 AND 1000),
                        version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id,client_id),
                        UNIQUE(user_id,conversation_id,turn_version,answer_hash,kind),
                        UNIQUE(user_id,id),
                        FOREIGN KEY(conversation_id,turn_version) REFERENCES coach_turns(conversation_id,version) ON DELETE CASCADE
                    );
                    CREATE TABLE knowledge_candidates (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        feedback_id TEXT NOT NULL,
                        client_id TEXT NOT NULL,
                        request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                        content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 1000),
                        source_snapshot TEXT NOT NULL,
                        source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
                        content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
                        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','rejected')),
                        review_note TEXT NOT NULL DEFAULT '' CHECK(length(review_note) <= 300),
                        version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id,client_id),
                        UNIQUE(user_id,content_hash,source_hash),
                        FOREIGN KEY(user_id,feedback_id) REFERENCES knowledge_feedback(user_id,id) ON DELETE CASCADE
                    );
                    CREATE TABLE knowledge_candidate_events (
                        candidate_id TEXT NOT NULL REFERENCES knowledge_candidates(id) ON DELETE CASCADE,
                        version INTEGER NOT NULL CHECK(version >= 1),
                        action TEXT NOT NULL CHECK(action IN ('created','revised','rejected')),
                        content_hash TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        note TEXT NOT NULL DEFAULT '' CHECK(length(note) <= 300),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(candidate_id,version)
                    );
                    PRAGMA user_version = 19;
                    COMMIT;
                """)
            if version < 20:
                if version > 0:
                    backup_path = self.path.with_name(self.path.name + ".pre-v20.bak")
                    if not backup_path.exists():
                        with closing(sqlite3.connect(backup_path)) as backup:
                            connection.backup(backup)
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE knowledge_reviewers (
                        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                        revision INTEGER NOT NULL CHECK(revision >= 1),
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE knowledge_submissions (
                        id TEXT PRIMARY KEY,
                        candidate_id TEXT NOT NULL REFERENCES knowledge_candidates(id) ON DELETE CASCADE,
                        candidate_version INTEGER NOT NULL CHECK(candidate_version >= 1),
                        client_id TEXT NOT NULL,
                        request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                        content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 1000),
                        content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
                        source_snapshot TEXT NOT NULL,
                        source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','checked','approved','rejected','withdrawn','invalidated')),
                        version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                        gate_id TEXT,
                        reviewer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        reviewer_revision INTEGER,
                        reason TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(candidate_id,client_id)
                    );
                    CREATE UNIQUE INDEX knowledge_submission_active ON knowledge_submissions(candidate_id)
                        WHERE status IN ('pending','checked','approved');
                    CREATE TABLE knowledge_review_gates (
                        id TEXT PRIMARY KEY,
                        submission_id TEXT NOT NULL REFERENCES knowledge_submissions(id) ON DELETE CASCADE,
                        binding_hash TEXT NOT NULL CHECK(length(binding_hash)=64),
                        corpus_hash TEXT NOT NULL CHECK(length(corpus_hash)=64),
                        policy TEXT NOT NULL,
                        passed INTEGER NOT NULL CHECK(passed IN (0,1)),
                        report TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE knowledge_review_events (
                        submission_id TEXT NOT NULL REFERENCES knowledge_submissions(id) ON DELETE CASCADE,
                        version INTEGER NOT NULL CHECK(version >= 1),
                        action TEXT NOT NULL,
                        actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(submission_id,version)
                    );
                    CREATE TRIGGER knowledge_candidate_review_change AFTER UPDATE ON knowledge_candidates
                    BEGIN
                        UPDATE knowledge_submissions SET status='invalidated',reason='candidate_changed',
                            version=version+1,updated_at=CURRENT_TIMESTAMP
                            WHERE candidate_id=OLD.id AND status IN ('pending','checked','approved');
                        INSERT INTO knowledge_review_events(submission_id,version,action)
                            SELECT id,version,'candidate_changed' FROM knowledge_submissions
                            WHERE candidate_id=OLD.id AND reason='candidate_changed'
                            AND NOT EXISTS(SELECT 1 FROM knowledge_review_events e
                                WHERE e.submission_id=knowledge_submissions.id AND e.version=knowledge_submissions.version);
                    END;
                    CREATE TRIGGER knowledge_answer_review_change AFTER UPDATE OF message,response ON coach_turns
                        WHEN OLD.message IS NOT NEW.message OR OLD.response IS NOT NEW.response
                    BEGIN
                        UPDATE knowledge_submissions SET status='invalidated',reason='answer_changed',
                            version=version+1,updated_at=CURRENT_TIMESTAMP
                            WHERE status IN ('pending','checked','approved') AND candidate_id IN (
                                SELECT c.id FROM knowledge_candidates c JOIN knowledge_feedback f ON f.id=c.feedback_id
                                WHERE f.conversation_id=OLD.conversation_id AND f.turn_version=OLD.version);
                        INSERT INTO knowledge_review_events(submission_id,version,action)
                            SELECT id,version,'answer_changed' FROM knowledge_submissions WHERE reason='answer_changed'
                            AND NOT EXISTS(SELECT 1 FROM knowledge_review_events e
                                WHERE e.submission_id=knowledge_submissions.id AND e.version=knowledge_submissions.version);
                    END;
                    CREATE TRIGGER knowledge_reviewer_revoke AFTER UPDATE ON knowledge_reviewers
                        WHEN OLD.revision<>NEW.revision OR NEW.enabled=0
                    BEGIN
                        UPDATE knowledge_submissions SET status='invalidated',reason='reviewer_changed',
                            version=version+1,updated_at=CURRENT_TIMESTAMP
                            WHERE reviewer_id=OLD.user_id AND status='approved';
                        INSERT INTO knowledge_review_events(submission_id,version,action)
                            SELECT id,version,'reviewer_changed' FROM knowledge_submissions
                            WHERE reviewer_id=OLD.user_id AND reason='reviewer_changed'
                            AND NOT EXISTS(SELECT 1 FROM knowledge_review_events e
                                WHERE e.submission_id=knowledge_submissions.id AND e.version=knowledge_submissions.version);
                    END;
                    CREATE TRIGGER knowledge_reviewer_delete BEFORE DELETE ON knowledge_reviewers
                    BEGIN
                        UPDATE knowledge_submissions SET status='invalidated',reason='reviewer_changed',
                            version=version+1,updated_at=CURRENT_TIMESTAMP
                            WHERE reviewer_id=OLD.user_id AND status='approved';
                        INSERT INTO knowledge_review_events(submission_id,version,action)
                            SELECT id,version,'reviewer_changed' FROM knowledge_submissions
                            WHERE reviewer_id=OLD.user_id AND reason='reviewer_changed'
                            AND NOT EXISTS(SELECT 1 FROM knowledge_review_events e
                                WHERE e.submission_id=knowledge_submissions.id AND e.version=knowledge_submissions.version);
                    END;
                    CREATE TRIGGER knowledge_reviewer_account_delete BEFORE DELETE ON users
                    BEGIN
                        UPDATE knowledge_submissions SET status='invalidated',reason='reviewer_changed',
                            version=version+1,updated_at=CURRENT_TIMESTAMP
                            WHERE reviewer_id=OLD.id AND status='approved';
                        INSERT INTO knowledge_review_events(submission_id,version,action)
                            SELECT id,version,'reviewer_changed' FROM knowledge_submissions
                            WHERE reviewer_id=OLD.id AND reason='reviewer_changed'
                            AND NOT EXISTS(SELECT 1 FROM knowledge_review_events e
                                WHERE e.submission_id=knowledge_submissions.id AND e.version=knowledge_submissions.version);
                    END;
                    PRAGMA user_version = 20;
                    COMMIT;
                """)
