# Connector domains: mock handlers must byte-mirror the frozen scripts' semantics

The env mock world (`NOTIFYBOT_CONNECTORS_MOCK=1`, `_mock_runner`) emulates each
frozen AppleScript constant. Two real divergences slipped into FASE 3 and were
caught only by skeptical review — both would let mock-mode smokes pass behavior
that real macOS would refuse:

1. `_mock_runner` search matching name+org while the script matched name-only.
2. Send-guard mock returning "sent" for ANY exact-name hit, while the script's
   federated resolution (first email → first phone → `""` → marker) and the
   ambiguity refusal (>1 same-name candidate → marker) refuse more cases.

Rules going forward:
- A mock handler must reproduce: match semantics, resolution order, refusal
  markers, and output row shapes of the constant it keys on — not just the
  happy path.
- The `_mock_runner` catch-all is `return _Outcome(0, "0", "")` — probes pass
  TRIVIALLY for any new domain without a handler; actions silently produce
  junk instead of failing. When adding a domain: probe-classification tests +
  handler-level observability tests (add→list→send→refuse) are mandatory or
  the suite lies green.
- Mutation-proof new guards: reviewers ran each guard's deletion on a temp
  copy and confirmed exactly one failing assertion — cheap and load-bearing
  (5/5 guards were protected by exactly one test each).
- Validation runs BEFORE the consent gate (whitelist → `_build_*` → gate), so
  cap/validation tests must expect errors even on ungranted domains.
- Domain-order pins are load-bearing (`CONNECTOR_DOMAINS.index(...)`/`[n]`,
  endpoint shape sets, gated-rows loops, `bot.ts` interface key order before
  `system`) — every FASE adding domains must update ALL of them.
