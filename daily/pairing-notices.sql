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
