# Changelog

## 0.2.0 · 2026-11-04

- Fewer false positives. Hand-checking blocks from the 0.1.0 replay found three kinds, now fixed: claim words inside Markdown table rows, claim words in slash-separated labels ("pass/fixed/skipped", 待处理/已解决/已关闭), and 没核 / 未核 / 待核 ("not checked yet") not being read as an honest "not verified."
- `replay` skips copies of earlier turns that resuming a session writes into a new file, and splits the blocked "done" claims by reason.
- When a turn made no code edits, the block message now also suggests citing proof you already have (a commit hash, a line like "14 passed" or "HTTP 200") instead of running a check.
- Read-only commands chained with `cd`, `&&` or a pipe (`cd repo && git diff`) no longer count as proof, matching the README. A command with `$(…)` or backticks (`echo $(pytest)`) and a tool whose name only starts like a listed one (`find-unused-deps`) aren't treated as read-only.
- Block messages are shorter and quote the claim without its bullet or trailing punctuation.
- The hook now sends its note back as `additionalContext` instead of `decision: block`. Claude Code shows it as "Stop hook feedback" rather than as a hook error, with the same protection against blocking twice in a row.
- Proof is looked for in the reply as written, so a commit hash in backticks or right before a question counts.
- Only a 2xx status counts as proof ("HTTP 200", "status 200", "201 Created"), and a pass count counts only on a line with no failures, so pytest's "3 failed, 2 passed" and "0 passed" don't. A hex string that's part of a UUID isn't a commit hash, and 还不是全绿 ("not all green yet") isn't proof.
- A condition ("if the config file doesn't exist…", 如果没找到…) now cancels a "not found" claim.
- A condition cancels a "done" claim only when it opens the clause or the sentence ("once tests pass, tag it"), so "the crash when saving is fixed" is now a claim. Clauses also break at "and", "but", "so" and dashes, so "no errors, and everything is deployed" is a claim too.
- The messages say what actually happened: "nothing ran successfully after the last edit" when the only run after it failed, and "only lookups, to-do updates or failed commands" when nothing tested anything.
- `report` skips damaged log lines and says '2 "done", 1 "not found"' instead of internal names. `replay` says when the folder it was given doesn't exist. The hook ignores a payload that isn't a JSON object.
- `replay` counts "not found" claims on their own, even in a turn already blocked for "done". `replay` and `report` get singulars right ("1 turn", "1 copy", "1 has").
- More tests: every kind of cited proof, look-alikes that aren't proof, and each "not found" phrase.
- README numbers re-measured. The 0.1.0 README's figures (112 of 560 "done" claims, 4 of 26 "not found") counted those copies and the false positives above. The new ones don't, are broken down by reason, and come with a hand-checked sample.

## 0.1.0 · 2026-10-28

First public release.
