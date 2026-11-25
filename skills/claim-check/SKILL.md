---
name: claim-check
description: Use before telling the user that work is done, fixed, deployed or passing, or that something does not exist or is not used. Requires proof from this turn, or an honest "not verified".
---

# claim-check

Two kinds of sentence cause most of the damage when they are wrong: "it's fixed" and "it isn't there". Before you write either one, have the proof in this turn.

## Before you say it's done, fixed, deployed or passing

1. After your last code or config edit, run the thing that proves it: the tests, the build, the command, a request to the live URL.
2. Read how it ended, not just that it ran. A non-zero exit, a timeout, a denied command, "0 tests ran", a failure line in the output (even after `| tail`), or a run still going in the background means it did not pass.
3. Put the result in your answer: how many tests passed, the status code, the commit, the exit code. If something still fails, say what.
4. If you edited again after that run, run it again. A check from before the last edit proves nothing about the code now. That includes edits made with `sed -i` or a heredoc.
5. Re-reading the file you just edited is not a check.
6. If you can't run the check, say so: "changed, not verified yet". That's an acceptable answer.

## Before you say a bug is fixed

1. Show the same test failing before the fix and passing after. If you wrote the test after the fix, show it failing on the old code (`git stash`, run it, `git stash pop`), and mark a run you expect to fail with a comment such as `# should fail on the old code`. If you only have the passing run, say so.
2. Run the tests that load the code you changed, not an unrelated file that happens to be green. If you can't tell whether they reach the change, say that.

## Before you say something doesn't exist or isn't used

Look at least twice, in different ways: search for the name, then search for a likely alias or read the place it would be registered. If you only looked once, call it a guess.

## When "done" means a real fix

A real fix covers five things. Say which ones you did, and which you skipped:

1. **Cause.** You found why it broke, not only where.
2. **Prevention.** Something now stops the same failure, such as a test, a check or a guard.
3. **Same bug elsewhere.** You looked for the same bug in similar places.
4. **Verification.** You ran the real thing and saw it work.
5. **Cleanup.** No temporary files, debug output or backup copies left behind.

A workaround that stops the symptom is fine. Call it a workaround, not a fix.

## If a hook blocked you

The claim-check hook found a claim without proof. If the last test or build failed, make it pass or say what still fails. Otherwise run the check and show the result, or rewrite the sentence as "not verified yet". Citing proof you already have (a commit hash, a line like "14 passed" or "HTTP 200") only works when no code was edited in this session: after an edit, only a check that passes after it counts. Do one of these before you stop again.
