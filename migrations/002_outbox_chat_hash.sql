-- Lets /forget remove sent-message records (which hold an encrypted chat pointer) for a person's private chat.
ALTER TABLE outbox ADD COLUMN chat_hash TEXT;
CREATE INDEX outbox_chat_hash_idx ON outbox (chat_hash);
