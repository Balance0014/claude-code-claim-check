# Changelog

## 0.6.0 · 2026-11-17

Reads how each command ended, and keeps "might not prove it" out of the blocks.

- Tests and builds count only if they passed. The hook reads the exit code and the runner's own summary line, so `pytest | tail -3` printing "2 failed" is a failure even though the pipe exits 0 (pytest, unittest, Jest, Vitest, Mocha, `go test`, Cargo, TAP, `tsc`, gcc and clang, npm, make, Gradle, Maven, `xcodebuild`). A run that timed out, was denied, never started, was still running in the background, or ran zero tests isn't proof. A background run counts once its notification, `TaskOutput` or stop says how it ended.
- If the last test or build after the last edit failed, a later command that passed doesn't make up for it. A reply that says what still fails ("1 test still fails, pre-existing") passes. A run the command marks as meant to fail (`# should fail`, a mutation test) doesn't count either way.
- The session is one timeline of edits and runs, each with how it ended, instead of a yes/no carried from turn to turn. A check that failed in an earlier turn no longer counts, and one that passed counts until code is edited after it.
- Fewer false blocks: a failed or rejected edit no longer counts as an edit, and the hook waits up to a second for the transcript to catch up with the final message before judging. Both could block a claim that had been checked. When the final message makes no claim, the hook doesn't read the transcript at all.
- Code changed by a shell command counts as an edit when Claude Code records which source files it changed and the command writes (`sed -i`, `>`, `tee`, `cp`, `mv`, `patch`). Before, the `sed -i` itself counted as the check. `git pull`, `git checkout` and the like, and scripts writing data files, aren't edits.
- Notes, which never block. When a "fixed" claim's tests passed but no run of them failed before the edit, when the test files named on the command line never mention the changed file, when the coverage report shows it at 0%, when every check ran in another git repository, or when a build output ran without a rebuild, you get a one-line note. It goes to you, not the agent, and the turn ends as usual. It comes with the turn that did the work, not again on later wrap-ups. New option `notes` ("Show notes", on by default). Notes are logged with `"kind": "note"`, and `report` counts them separately.
- `replay` skips subagent transcripts, since the Stop hook doesn't run there, and prints the notes and how many "fixed" claims had a test that failed before the edit and passed after.
- On the author's current machine (676 main transcripts, 4,197 turns, 458 "done" claims), 0.5.1 blocks 1 and 0.6.0 blocks 2. The new one is real: code changed with `sed -i` after the tests, then "the tests pass." No claim 0.5.1 blocked gets through. The first draft of these rules blocked 24 more on the same replay, mostly wrongly, and each kind became a rule fix and a test. The README has the details and what the replay can't show.

## 0.5.1 · 2026-11-12

- The hook always appends blocks to `~/.claude/claim-check/blocks.jsonl`. It no longer reads `CLAIM_CHECK_LOG`, so a project env block cannot redirect the audit log. `report --log <path>` reads a different file.

## 0.5.0 · 2026-11-06

- A "not found" claim needs two different kinds of lookup. Searching the same way twice no longer counts, even in another folder or with different flags. A different pattern, a different file, or a different tool does. Reading a file and `cat` on that same file count as one. Suggested in a Reddit comment.

## 0.4.0 · 2026-11-06

- Removed `CLAIM_CHECK_OFF`. A repository setting, a shell profile, or a command the agent runs could set it, and then the Stop hook never ran. To turn the hook off, set the plugin's `enabled` option to `false`. Claude Code stores that in your user settings (and in managed settings, which win), and does not read it from the project.
- Removed `CLAIM_CHECK_LOG_ONLY` for the same reason: the project could set it and the hook would stop blocking. A trial run is the plugin's `log_only` option now.

## 0.3.1 · 2026-11-05

- `CLAIM_CHECK_LOG_ONLY`: log what the hook would have blocked without blocking, for a trial run before turning it on. Suggested in a Reddit comment.

## 0.3.0 · 2026-11-04

- A wrap-up with no code edits now passes if a check ran successfully in an earlier turn of the session and no code was edited after it. Before, only the current turn counted. On the author's history this cuts blocked "done" claims from 86 to 2 of 406. Suggested in a Reddit comment.

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
- Chinese sentences are split at 。！？ even with no space after them, so a 还没 or 如果 in the previous sentence no longer cancels a claim (注册页还没改。登录的 bug 已经修好了。 is now blocked).
- `report` skips damaged log lines and says '2 "done", 1 "not found"' instead of internal names. `replay` says when the folder it was given doesn't exist. The hook ignores a payload that isn't a JSON object.
- `replay` counts "not found" claims on their own, even in a turn already blocked for "done". `replay` and `report` get singulars right ("1 turn", "1 copy", "1 has").
- More tests: every kind of cited proof, look-alikes that aren't proof, and each "not found" phrase.
- README numbers re-measured. The 0.1.0 README's figures (112 of 560 "done" claims, 4 of 26 "not found") counted those copies and the false positives above. The new ones don't, are broken down by reason, and come with a hand-checked sample.

## 0.1.0 · 2026-10-28

First public release.
