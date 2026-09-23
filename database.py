import sqlite3
from contextlib import contextmanager
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
            if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13):
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
