CREATE TABLE IF NOT EXISTS auto_offers (
    mail_id TEXT PRIMARY KEY REFERENCES mail(id),
    binding_id INTEGER NOT NULL REFERENCES bindings(id),
    watch_id TEXT NOT NULL UNIQUE,
    turn TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS offer_to_auto_watch AFTER INSERT ON mail
WHEN NEW.source='cc' AND NEW.reply_mode='none' AND NEW.state='queued' BEGIN
    INSERT OR IGNORE INTO auto_offers(mail_id,binding_id,watch_id,turn)
        SELECT NEW.id,binding_id,watch_id,turn FROM auto_inboxes
        WHERE binding_id=NEW.binding_id AND watch_id IS NOT NULL
          AND expires_ms>(julianday('now')-2440587.5)*86400000;
END;

-- A short handoff from a real outbound Desktop tool call to its background
-- consumer. Offers contain no body and are not delivery acknowledgements.
CREATE TABLE IF NOT EXISTS turn_handoffs (
    binding_id INTEGER PRIMARY KEY REFERENCES bindings(id),
    turn TEXT NOT NULL, expires_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS burst_offers (
    mail_id TEXT PRIMARY KEY REFERENCES mail(id), binding_id INTEGER NOT NULL REFERENCES bindings(id),
    turn TEXT NOT NULL, expires_ms INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS prompt_receiver_on_first_send AFTER INSERT ON turn_handoffs BEGIN
    UPDATE auto_inboxes SET hint_pending=1 WHERE binding_id=NEW.binding_id;
END;
CREATE TRIGGER IF NOT EXISTS prompt_receiver_in_new_turn AFTER UPDATE OF turn ON turn_handoffs
WHEN NEW.turn<>OLD.turn BEGIN
    UPDATE auto_inboxes SET hint_pending=1 WHERE binding_id=NEW.binding_id;
END;
CREATE INDEX IF NOT EXISTS burst_by_turn ON burst_offers(binding_id,turn,expires_ms);
CREATE TRIGGER IF NOT EXISTS continue_after_auto_notice AFTER UPDATE OF state ON mail
WHEN OLD.state='queued' AND NEW.state='unknown' AND NEW.detail='配对收件箱待领取' BEGIN
    UPDATE turn_handoffs SET expires_ms=max(expires_ms,(unixepoch()+30)*1000)
    WHERE binding_id=NEW.binding_id AND turn=(SELECT turn FROM burst_offers WHERE mail_id=NEW.id);
END;
CREATE TRIGGER IF NOT EXISTS offer_during_handoff AFTER INSERT ON mail
WHEN NEW.source='cc' AND NEW.reply_mode='none' AND NEW.state='queued' BEGIN
    INSERT INTO burst_offers(mail_id,binding_id,turn,expires_ms)
        SELECT NEW.id,a.binding_id,CASE WHEN a.watch_id IS NOT NULL AND a.expires_ms>clock.ms THEN a.turn ELSE h.turn END,
               clock.ms+30000
        FROM auto_inboxes a LEFT JOIN turn_handoffs h ON h.binding_id=a.binding_id
        CROSS JOIN (SELECT CAST((julianday('now')-2440587.5)*86400000 AS INTEGER) AS ms) clock
        WHERE a.binding_id=NEW.binding_id AND
              ((a.watch_id IS NOT NULL AND a.expires_ms>clock.ms) OR h.expires_ms>clock.ms);
END;
CREATE TRIGGER IF NOT EXISTS wait_for_turn_handoff BEFORE UPDATE OF state ON mail
WHEN OLD.state='queued' AND NEW.state='submitting' AND EXISTS (
    SELECT 1 FROM burst_offers o JOIN auto_inboxes a ON a.binding_id=o.binding_id
    LEFT JOIN turn_handoffs h ON h.binding_id=o.binding_id AND h.turn=o.turn
    CROSS JOIN (SELECT CAST((julianday('now')-2440587.5)*86400000 AS INTEGER) AS ms) clock
    WHERE o.mail_id=OLD.id AND o.expires_ms>clock.ms AND
          ((a.turn=o.turn AND a.watch_id IS NOT NULL AND a.expires_ms>clock.ms) OR h.expires_ms>clock.ms)
) BEGIN
    SELECT RAISE(IGNORE);
END;
-- Already-running MCP servers understand a one-letter offer. Bind the oldest
-- pending letter to their next live watch, including across watch/take gaps.
CREATE TRIGGER IF NOT EXISTS attach_pending_to_watch AFTER UPDATE OF watch_id,expires_ms ON auto_inboxes
WHEN NEW.watch_id IS NOT NULL AND NEW.expires_ms>(julianday('now')-2440587.5)*86400000
 AND NOT EXISTS (SELECT 1 FROM auto_offers WHERE watch_id=NEW.watch_id) BEGIN
    INSERT INTO auto_offers(mail_id,binding_id,watch_id,turn)
        SELECT o.mail_id,o.binding_id,NEW.watch_id,o.turn FROM burst_offers o
        JOIN mail m ON m.id=o.mail_id
        WHERE o.binding_id=NEW.binding_id AND o.turn=NEW.turn AND m.state='queued'
          AND o.expires_ms>(julianday('now')-2440587.5)*86400000
        ORDER BY m.seq LIMIT 1
        ON CONFLICT(mail_id) DO UPDATE SET watch_id=excluded.watch_id WHERE auto_offers.turn=excluded.turn;
END;
CREATE TRIGGER IF NOT EXISTS wait_for_auto_watch BEFORE UPDATE OF state ON mail
WHEN OLD.state='queued' AND NEW.state='submitting' AND EXISTS (
    SELECT 1 FROM auto_offers o JOIN auto_inboxes a ON a.binding_id=o.binding_id
    WHERE o.mail_id=OLD.id AND a.watch_id=o.watch_id AND a.turn=o.turn
      AND a.expires_ms>(julianday('now')-2440587.5)*86400000
) BEGIN
    SELECT RAISE(IGNORE);
END;
