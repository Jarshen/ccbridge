CREATE TABLE IF NOT EXISTS delivery_evidence (
    mail_id TEXT PRIMARY KEY REFERENCES mail(id),
    turn_id TEXT NOT NULL,
    observed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS send_feedback (
    id TEXT PRIMARY KEY,
    binding_id INTEGER NOT NULL REFERENCES bindings(id),
    sender TEXT NOT NULL CHECK(sender IN ('cc','desktop')),
    reason TEXT NOT NULL,
    related_id TEXT,
    state TEXT NOT NULL DEFAULT 'queued',
    correction INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS pending_send_feedback ON send_feedback(binding_id,sender,state);
