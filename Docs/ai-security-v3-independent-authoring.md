# Independent adversarial set v3 — frozen authoring record

Authoring date: 2026-10-07 (client date, Asia/Bangkok).
Artifact: `operations/fixtures/ai_security_v3.json`
SHA-256: `3cd60214b2d15ebab5b323361793b09e5270813cd6b1b13f2769b609add783e5`

The set contains 48 cases in 24 adversarial/benign pairs, including four paired stored-draft conversations. Every item comes from the five-item synthetic catalog. The cases are synthetic and contain no real personal data or transactions.

I authored expected outcomes from the project scope and domain contract before receiving implementation changes or running any model. Sources read were PROJECT.md (scope and glossary), operations/contracts.py, operations/models.py, and synthetic-data.json. The parent agent supplied only the evaluation draft envelope and annotation expectations. I did not read operations/ai.py, operations/ai_harness.py, operations/ai_eval.py, existing AI fixtures, prompts, or old benchmark results. A broad initial read included domain-model AI configuration metadata, but no extractor implementation or benchmark content.

The pairs target quantity attachment versus dates, document numbers and electrical ratings; swapped item quantities; contamination of purpose, reason, condition, description, impact, requested date and serials; fabricated approval and completion claims; role and tool-message impersonation; valid-catalog items that were never requested; multi-intent separation; and preservation of legitimate corrections. Benign cases cover Thai numerals, decimal cable lengths, serialized returns, Gregorian conversion of explicit Buddhist-calendar dates, corrections and draft continuity. Dates use 2027 to avoid ambiguity about past deadlines relative to the supplied client date.

Expected fields use genuine phrases from the request or retained genuine draft content. Every request has explicit fields and serial expectations; missing extra fields must not be interpreted as permission to invent their values. `forbidden_text` supplies additional literal unsafe claims, while exact expected items/fields/serials remain the semantic oracle. Draft attacks place operational instructions inside previously stored free text. Their paired controls use identical follow-up messages and clean drafts so loss of useful earlier context is observable.

Independence limitation: this is a separate agent author, not an external human red team. It is a deliberately small, authored challenge set, not a representative deployment distribution. It does not estimate real-world attack prevalence or prove universal prompt-injection resistance. No live-model or runtime outcome informed these annotations. The file is frozen at the digest above; do not tune its text or expectations after implementation or evaluation. Any later correction should be a separately versioned dataset with its reason disclosed.

Authoring checks: JSON parses; 48 unique IDs; 24 pairs; all expected catalog codes valid; serial expectations attached to requested items; no forbidden phrase contradicts its expected output. No model evaluation was run by the author.
