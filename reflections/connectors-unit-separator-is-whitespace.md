# Connectors: the row delimiter `\x1f` is Python whitespace — never `.strip()` before splitting

FASE 3 (`47422a5`) gotcha: `"\x1f".isspace()` is `True` in Python, so `str.strip()`
(not just `rstrip`) erases a row-final empty field's trailing delimiter — e.g.
contacts `get` records that legally end in an empty note field (`name\x1forg\x1f…\x1f\x1f\n`)
lost their last `\x1f` under `.strip()` and mis-chunked.

Rule for any new `_normalize` branch splitting `_DELIM` rows:
- Trim only `rstrip("\r\n")` (osascript appends the result newline; never emits `\r`).
- When rows may contain empty fields, do NOT go through `_split_delimited` — it
  drops empty items and misaligns `_chunk_rows`. Use the `split_keep` kind
  (raw `.split(_DELIM)` keeping positions) and rebuild with `_chunk_rows`.
- Marker literal compare (rc-0 stdout route) is different: single-token
  equality-after-`.strip()` is intended there and cannot false-positive.
- Byte cap `16 KB` reads stay `_truncate_bytes` (UTF-8 boundary-safe).

Existing exposure: music `status` already used RAW `.split` for the same reason —
that precedent was the clue; `split_keep` generalizes it for field-positioned rows.
