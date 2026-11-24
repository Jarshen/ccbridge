CREATE TABLE IF NOT EXISTS pairing_notices (
    binding_id INTEGER NOT NULL REFERENCES bindings(id),
    recipient TEXT NOT NULL CHECK(recipient IN ('cc', 'desktop')),
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued', 'sending', 'sent', 'unknown')),
    detail TEXT,
    PRIMARY KEY(binding_id, recipient)
);
CREATE TRIGGER IF NOT EXISTS welcome_new_pair AFTER INSERT ON bindings WHEN NEW.active=1 BEGIN
    INSERT INTO pairing_notices(binding_id, recipient) VALUES (NEW.id, 'cc'), (NEW.id, 'desktop');
END;

CREATE TABLE IF NOT EXISTS auto_inboxes (
    binding_id INTEGER PRIMARY KEY REFERENCES bindings(id),
    watch_id TEXT,
    turn TEXT,
    expires_ms INTEGER NOT NULL DEFAULT 0,
    hint_pending INTEGER NOT NULL DEFAULT 0
);
DROP TRIGGER IF EXISTS auto_inbox_new_pair;
CREATE TRIGGER IF NOT EXISTS auto_inbox_on_pair AFTER INSERT ON bindings WHEN NEW.active=1 BEGIN
    INSERT INTO auto_inboxes(binding_id,hint_pending) VALUES(NEW.id,1);
END;
