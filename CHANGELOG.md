# Changelog

## 0.2.0 · 2026-11-04

- Fewer false positives. Hand-checking blocks from the 0.1.0 replay found three kinds, now fixed: claim words inside Markdown table rows, claim words in slash-separated labels (闲聊/已解决/广告, "chatter/resolved/ads"), and 没核 / 未核 / 待核 ("not checked yet") not being read as an honest "not verified."
- `replay` skips copies of earlier turns that resuming a session writes into a new file, and splits the blocked "done" claims by reason.
- When a turn made no code edits, the block message now also suggests citing proof you already have (a commit, test result or status code) instead of running a check.
- Read-only commands chained with `cd`, `&&` or a pipe (`cd repo && git diff`) no longer count as proof, matching the README.
- Block messages are shorter and quote the claim without its bullet or trailing punctuation.
- README numbers re-measured. The 0.1.0 figures (112 of 560 "done" claims, 4 of 26 "not found") counted those copies and the false positives above. The new ones don't, are broken down by reason, and come with a hand-checked sample.

## 0.1.0 · 2026-10-28

First public release.
