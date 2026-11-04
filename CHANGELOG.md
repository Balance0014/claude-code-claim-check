# Changelog

## 0.2.0 · 2026-11-04

- Fewer false positives, all found by hand-checking replay blocks: claim words inside Markdown table rows and slash-separated labels ("chatter/resolved/ads") no longer count, and the Chinese "没核 / 未核 / 待核" (not checked yet) passes as an honest answer.
- `replay` skips exact copies of turns that resumed sessions write into new files, and splits the blocked "done" claims by reason.
- README numbers re-measured with the new replay and broken down by reason, with a hand-checked sample.

## 0.1.0 · 2026-10-28

First public release.
