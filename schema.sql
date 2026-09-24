-- Asbuilt schema
--
-- Every table here encodes something learned from real data (see
-- research/13-project-trace-findings.md). The comments say which.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- A customer is a COMPANY with MANY mailboxes: owner's, office manager's,
-- estimating@, accounting@. Ingesting one gives a partial record.
CREATE TABLE IF NOT EXISTS mailbox (
    id          INTEGER PRIMARY KEY,
    address     TEXT NOT NULL UNIQUE,
    host        TEXT NOT NULL,
    label       TEXT,
    added_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Deduped by RFC Message-ID: the same GC email lands in every CC'd mailbox
-- and in both INBOX and Sent. Store the message once.
CREATE TABLE IF NOT EXISTS message (
    id          INTEGER PRIMARY KEY,
    message_id  TEXT UNIQUE,              -- NULL only for malformed mail
    sent_at     TEXT,                     -- ISO8601 UTC; NULL if unparseable
    from_name   TEXT,
    from_email  TEXT,
    to_emails   TEXT,                     -- comma-joined
    cc_emails   TEXT,
    subject     TEXT,
    body_text   TEXT,
    in_reply_to TEXT,                     -- thread reconstruction
    thread_refs TEXT,                     -- References header
    ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Where each copy was found. Same message, several mailboxes/folders.
CREATE TABLE IF NOT EXISTS message_location (
    message_id  INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    mailbox_id  INTEGER NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    folder      TEXT NOT NULL,
    uid         INTEGER,
    PRIMARY KEY (message_id, mailbox_id, folder)
);

-- Content-addressed by sha256. The same drawing set gets forwarded a dozen
-- times; store the bytes once. is_inline flags signature logos and tracking
-- pixels (image001.png, Outlook-*.png) which massively inflate naive counts.
CREATE TABLE IF NOT EXISTS attachment (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,
    size_bytes    INTEGER NOT NULL,
    content_type  TEXT,
    stored_path   TEXT NOT NULL,
    is_inline     INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Filename is per-send, not per-file: the same bytes arrive as
-- "Q690 Drawings.pdf" from one party and "scan001.pdf" from another.
CREATE TABLE IF NOT EXISTS message_attachment (
    message_id     INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    attachment_id  INTEGER NOT NULL REFERENCES attachment(id) ON DELETE CASCADE,
    filename       TEXT,
    PRIMARY KEY (message_id, attachment_id, filename)
);

-- Documents frequently are NOT in the email: Dropbox, WeTransfer, Drive,
-- SharePoint, Submittal Exchange. Links expire, so record the FACT of
-- delivery even when the file is unreachable, and prompt the user early.
CREATE TABLE IF NOT EXISTS shared_link (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    provider    TEXT,                     -- dropbox | wetransfer | gdrive | ...
    state       TEXT NOT NULL DEFAULT 'unfetched',  -- unfetched|saved|expired|skipped
    noticed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (message_id, url)
);

-- Projects, and the many names each one goes by. Eleven confirmed aliases
-- for a single school (Q690 / Q 690 / 690 (Q) / HS 690Q / PS 690Q /
-- "HS for Law Enforcement" / 116-25 Guy R Brewer Blvd / C000015851-LS0086 ...)
CREATE TABLE IF NOT EXISTS project (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- confirmed=0 means the system PROPOSED this alias and a human has not yet
-- agreed. Nothing silently self-confirms.
CREATE TABLE IF NOT EXISTS project_alias (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    source      TEXT,                     -- ntp | subcontract | observed | manual
    confirmed   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_id, alias)
);

-- MANY-TO-MANY, and deliberately at BOTH levels.
-- One email carried seven SOV spreadsheets for seven different schools:
-- the message touches seven projects, and each attachment belongs to exactly
-- one. A folder tree cannot express this, which is why the hand-built folder
-- system was abandoned.
CREATE TABLE IF NOT EXISTS message_project (
    message_id  INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    confidence  REAL,
    method      TEXT,                     -- alias_match | llm | manual
    confirmed   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, project_id)
);

CREATE TABLE IF NOT EXISTS attachment_project (
    attachment_id INTEGER NOT NULL REFERENCES attachment(id) ON DELETE CASCADE,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    confidence    REAL,
    method        TEXT,
    confirmed     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (attachment_id, project_id)
);

-- Per-sender boilerplate: lines that repeat across nearly everything one
-- person sends — signature blocks, corporate legal disclaimers, "Sent from my
-- iPhone", certification footers. Learned from the corpus rather than
-- hardcoded, because every mailbox has different ones and a rule written for
-- one customer strips nothing for the next.
CREATE TABLE IF NOT EXISTS sender_boilerplate (
    from_email  TEXT NOT NULL,
    line        TEXT NOT NULL,
    n_messages  INTEGER NOT NULL,     -- how many of this sender's messages carry it
    PRIMARY KEY (from_email, line)
);

CREATE INDEX IF NOT EXISTS idx_boilerplate_sender ON sender_boilerplate(from_email);

-- Extracted text content of attachments. One row per attachment (keyed on the
-- content-addressed attachment, so a PDF forwarded twelve times is extracted
-- once). status records WHY there is no text when there isn't:
--   ok          — text extracted
--   empty       — parsed fine but no text layer (scanned/image PDF → needs OCR)
--   unsupported — not a type we extract (images, dwg, ics, ...)
--   error       — parser failed (corrupt, encrypted, truncated)
CREATE TABLE IF NOT EXISTS attachment_text (
    attachment_id INTEGER PRIMARY KEY REFERENCES attachment(id) ON DELETE CASCADE,
    status        TEXT NOT NULL,
    text          TEXT,
    n_pages       INTEGER,
    n_chars       INTEGER,
    extracted_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Full-text search over document contents, joined by rowid = attachment_id.
CREATE VIRTUAL TABLE IF NOT EXISTS attachment_fts USING fts5(
    text,
    content = 'attachment_text',
    content_rowid = 'attachment_id'
);

CREATE TRIGGER IF NOT EXISTS attachment_fts_insert
AFTER INSERT ON attachment_text WHEN new.text IS NOT NULL BEGIN
    INSERT INTO attachment_fts(rowid, text) VALUES (new.attachment_id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS attachment_fts_delete
AFTER DELETE ON attachment_text WHEN old.text IS NOT NULL BEGIN
    INSERT INTO attachment_fts(attachment_fts, rowid, text)
    VALUES ('delete', old.attachment_id, old.text);
END;

-- Stage 1 of identifier resolution: every string that COULD be a job
-- identifier, found by cheap pattern matching. High recall, low precision by
-- design -- "690" also appears in dollar amounts and phone numbers. Stage 2
-- (LLM) decides which are real and which refer to the same job.
CREATE TABLE IF NOT EXISTS candidate (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER REFERENCES message(id) ON DELETE CASCADE,
    raw         TEXT NOT NULL,            -- exactly as it appeared
    normalized  TEXT NOT NULL,            -- Q690, "Q 690", "690(Q)" -> q690
    kind        TEXT NOT NULL,            -- school | sca_contract | design_no | address | ...
    source      TEXT NOT NULL,            -- subject | body | filename
    UNIQUE (message_id, normalized, kind, source)
);

CREATE INDEX IF NOT EXISTS idx_candidate_norm ON candidate(normalized);
CREATE INDEX IF NOT EXISTS idx_candidate_msg  ON candidate(message_id);

-- Counterparties. Per-sender convention learning hangs off this later:
-- "mail from this firm reliably encodes CSI codes in filenames" is a fact
-- about a party, never a global rule.
CREATE TABLE IF NOT EXISTS party (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    kind        TEXT,                     -- gc | architect | agency | supplier | person
    domain      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS party_email (
    email     TEXT PRIMARY KEY,
    party_id  INTEGER NOT NULL REFERENCES party(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Evaluation. Held permanently separate from tuning: once a message is in the
-- sample it stays there, so scores across resolver versions stay comparable.
-- ---------------------------------------------------------------------------

-- Drawn once, then frozen. Stratified so assigned and unassigned mail are
-- measured separately -- they answer different questions (precision vs recall).
CREATE TABLE IF NOT EXISTS gold_sample (
    message_id  INTEGER PRIMARY KEY REFERENCES message(id) ON DELETE CASCADE,
    stratum     TEXT NOT NULL,            -- assigned | unassigned
    drawn_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gold_label (
    message_id    INTEGER PRIMARY KEY REFERENCES message(id) ON DELETE CASCADE,
    -- For assigned mail: was the system right?
    verdict       TEXT,                   -- correct | wrong | ambiguous
    -- For all mail: what kind of thing is this?
    entity_type   TEXT,                   -- construction | service | bid |
                                          -- admin | spam | unknown
    -- Recall detail for unassigned mail: does it belong to a project the
    -- system already knows, or one it never discovered?
    belongs_to    TEXT,                   -- existing | missing | none
    correct_project_id INTEGER REFERENCES project(id) ON DELETE SET NULL,
    note          TEXT,
    labeled_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Resumability: don't re-download 10,000 messages on every run.
CREATE TABLE IF NOT EXISTS sync_state (
    mailbox_id    INTEGER NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    folder        TEXT NOT NULL,
    last_uid      INTEGER NOT NULL DEFAULT 0,
    uid_validity  INTEGER,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (mailbox_id, folder)
);

-- Full-text search over subject + body. This alone beats the status quo:
-- 105 GB of mail that currently has no organization at all.
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    subject,
    body_text,
    content = 'message',
    content_rowid = 'id'
);

CREATE TRIGGER IF NOT EXISTS message_fts_insert AFTER INSERT ON message BEGIN
    INSERT INTO message_fts(rowid, subject, body_text)
    VALUES (new.id, new.subject, new.body_text);
END;

CREATE TRIGGER IF NOT EXISTS message_fts_delete AFTER DELETE ON message BEGIN
    INSERT INTO message_fts(message_fts, rowid, subject, body_text)
    VALUES ('delete', old.id, old.subject, old.body_text);
END;

CREATE INDEX IF NOT EXISTS idx_message_sent_at   ON message(sent_at);
CREATE INDEX IF NOT EXISTS idx_message_from      ON message(from_email);
CREATE INDEX IF NOT EXISTS idx_alias_lookup      ON project_alias(alias);
CREATE INDEX IF NOT EXISTS idx_link_state        ON shared_link(state);
