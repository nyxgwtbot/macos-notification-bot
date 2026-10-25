# Reflection: Phase 2 finance smoke — marker phrasing, checker wrapping, report freshness

Session 2026-10-25 (Router v2 + finance plugin). Lessons that must not drift:

1. **Smoke marker bodies must be unambiguously bank-flavored.** The first
   SMOKE-FIN-2 body ("Pix recebido de R$ 200,00 de Maria Souza") was
   classified by the LLM as `category: message`, so the deterministic
   `messages` rule won (rules-first is by design!) and the finance action
   never ran. Banking wording ("transacao bancaria:", "conta corrente") makes
   the classifier produce `other`/`alert` and reach the LLM router. Do not
   "fix" this by weakening rules-first.

2. **JSONL evidence wrappers: record vs event.** Evidence readers like
   `marker_event` return the record wrapper `{"line_no", "raw", "event"}`;
   helpers that inspect fields must unwrap `.get("event")` first. The same
   convention holds in `scripts/smoke_e2e.py::evaluate_record`.

3. **Reused HTML reports can go stale.** `smoke_finance.py` must treat a
   report as reusable only when `finance.db` is *older* than the report file
   (mtime comparison); otherwise regenerate. Without this, newly ingested
   ledger rows vanish from the reused HTML and amount assertions fail.

4. **Live-run budget accounting (update of `llm-budget-smoke-idempotency.md`):**
   the finance smoke's fresh path spends ~3 classifier + ~3 router + 1
   insights call the FIRST time; the reuse path (complete routed+finance+ledger
   evidence per marker AND report newer than finance.db) spends zero. Keep
   marker bodies stable once evidence exists — rewording a marker body re-feeds
   the pipeline.

5. `bot.py report` always regenerates (1 insights LLM call per invocation);
   the smoke avoids repeated invocations via the reuse rule above.