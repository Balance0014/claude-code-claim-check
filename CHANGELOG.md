# Changelog

## 0.2.0 · 2026-11-04

- Fewer false positives. Hand-checking blocks from the 0.1.0 replay found three kinds, now fixed: claim words inside Markdown table rows, claim words in slash-separated labels ("pass/fixed/skipped", 待处理/已解决/已关闭), and 没核 / 未核 / 待核 ("not checked yet") not being read as an honest "not verified."
- `replay` skips copies of earlier turns that resuming a session writes into a new file, and splits the blocked "done" claims by reason.
- When a turn made no code edits, the block message now also suggests citing proof you already have (a commit, test result or status code) instead of running a check.
- Read-only commands chained with `cd`, `&&` or a pipe (`cd repo && git diff`) no longer count as proof, matching the README.
- Block messages are shorter and quote the claim without its bullet or trailing punctuation.
- The hook now sends its note back as `additionalContext` instead of `decision: block`. Claude Code shows it as "Stop hook feedback" rather than as a hook error, with the same protection against blocking twice in a row.
- Proof is looked for in the reply as written, so a commit hash in backticks or right before a question counts.
- Only a 2xx HTTP status counts as proof, and a pass count doesn't when failures follow it ("2 passed, 3 failed") or when it's "0 passed".
- A condition ("if the config file doesn't exist…", 如果没找到…) now cancels a "not found" claim, as it already did for "done" claims.
- The messages say what actually happened: "nothing ran successfully after the last edit" when the only run after it failed, and "only lookups, to-do updates or failed commands" when nothing tested anything.
- `report` skips damaged log lines and says "2 done, 1 not found" instead of internal names. `replay` says when the folder it was given doesn't exist. The hook ignores a payload that isn't a JSON object.
- `replay` and `report` get singulars right ("1 turn", "1 copy").
- More tests: every kind of cited proof, look-alikes that aren't proof, and each "not found" phrase.
- README numbers re-measured. The 0.1.0 figures (112 of 560 "done" claims, 4 of 26 "not found") counted those copies and the false positives above. The new ones don't, are broken down by reason, and come with a hand-checked sample.

## 0.1.0 · 2026-10-28

First public release.
