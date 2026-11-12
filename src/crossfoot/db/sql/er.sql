-- The `er` schema: the counterparty alias registry (plan §2.7).
--
-- Entity resolution is harder than the LLM work, and the registry is where that difficulty is
-- kept. Two properties matter more than anything else about it:
--
--   1. It learns ONLY from human-confirmed escalations. A registry that learned from its own
--      matcher's output would compound its own errors and make results depend on processing
--      order; one that learned from the manifest would launder ground truth into the thing
--      being measured. So `provenance` is CHECK-constrained to a single value and
--      `escalation_id` is NOT NULL: an alias with any other origin cannot be represented,
--      which is a stronger statement than "we decline to write one".
--   2. It is frozen during eval runs. That is enforced in Python (`AliasRegistry.frozen`) and
--      again by the database role the servers connect as, which holds SELECT and nothing else.
--
-- Idempotent, and free of extensions -- the CI Postgres service does not mount docker/initdb/.

CREATE SCHEMA IF NOT EXISTS er;

CREATE TABLE IF NOT EXISTS er.counterparty_alias (
    alias_norm      text    PRIMARY KEY,
    alias_raw       text    NOT NULL,
    counterparty_id integer NOT NULL,
    provenance      text    NOT NULL CHECK (provenance = 'human_confirmed'),
    escalation_id   text    NOT NULL,
    confirmed_by    text    NOT NULL,
    confirmed_at    timestamptz NOT NULL
);

COMMENT ON TABLE er.counterparty_alias IS
    'Payer-name aliases confirmed by a human reviewer. The fuzzy layer handles drift that '
    'RapidFuzz can see; this table is for the remainder -- DBA and parent-company names that '
    'no string metric relates (spec 01 §4.8a).';
COMMENT ON COLUMN er.counterparty_alias.alias_norm IS
    'crossfoot.names.normalise() applied to alias_raw. The lookup key, so that a surface form '
    'is normalised the one way before it is ever compared.';
COMMENT ON COLUMN er.counterparty_alias.provenance IS
    'CHECK-constrained to human_confirmed. An alias learned from an LLM decision or an '
    'unreviewed match is not representable here.';
COMMENT ON COLUMN er.counterparty_alias.escalation_id IS
    'The confirmed escalation this alias came out of. NOT NULL, so every row points at the '
    'review that produced it.';
