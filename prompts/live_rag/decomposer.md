# Multi-intent decomposer

Convert one corpus-seeking utterance into at most four independently retrievable
subqueries. Preserve a complete semantic anchor and add only clauses that can
improve recall. Do not fragment single skill names or create queries that are
not grounded in the utterance. Mark which subqueries are unchanged from the
previous transcript revision so they can be reused.

Do not include benchmark examples, candidate facts, or canned answers.
