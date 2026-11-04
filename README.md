# claim-check

**A Claude Code Stop hook that sends your coding agent back when it says "fixed" without showing proof.**

[中文说明](README.zh-CN.md) · MIT · Python 3.9+, no dependencies

![claim-check blocks an unproven "is fixed" in Claude Code; the agent then runs the tests and answers with the result](docs/demo.svg)

The two agent replies that have cost me the most time are "it's fixed," when nothing had run since the last edit, and "that isn't used anywhere," after one search. Both sound final, so I moved on, and didn't find out they were wrong until something broke.

On November 4, 2026, I replayed my own Claude Code history through the hook: 3,977 turns, after dropping 1,649 duplicates (resuming a session copies its earlier turns into a new file). Of those, 404 ended with a claim like "done," "fixed," "deployed" or "tests pass," and the hook would have blocked 85. Most of the 85 weren't the case I built it for.

- 76 had no tool calls at all. Usually that's a wrap-up saying something was fixed or deployed, with no commit, test result or status code in it. The proof may be in an earlier turn, but the wrap-up is what I read, and it doesn't show any.
- 8 had tool calls, but only reads, searches, read-only commands, to-do updates or failed commands.
- 1 made the claim after a code edit with nothing run since. That's the case I built the hook for. It's probably rare here because an older version was already catching it on my machine. Your count may be higher.

I read 23 of the 85: 20 picked at random before a later rule change, when the count was 82, and the 3 that change added. Seventeen were wrap-ups that claimed finished work without citing a commit, test output or status code. Three weren't claims of finished work: a research finding (a change had been tested and made the model worse), a condition (the work would count as done once merged) and a plan for work that hadn't started. The other three could be read either way.

Separately, 17 turns ended by saying something wasn't there, and in 2 of them the agent had looked once or not at all.

When the agent tries to end its turn, the hook compares what the agent claimed with what it actually did since your last message. If the proof isn't there, it sends the agent back once and says what's missing.

```text
claim-check: you wrote "is fixed", but nothing ran successfully after the last edit.
Run the check that proves it (the tests, the script, a request to the live URL) and
show the result, or say plainly that it isn't verified yet.
```

## What it checks

| The agent says | It needs, in this turn |
| --- | --- |
| "fixed", "deployed", "tests pass", "done", "已修复", "已上线" | Something that ran **after the last code edit** and didn't fail: a shell command, any MCP tool (browser, API), a URL fetch or a subagent |
| The same, in a turn with no code edits and nothing run | Proof cited in the reply (see below). A turn that only edits docs passes. |
| "doesn't exist", "nothing calls it", "没找到" | At least two reads, searches or commands. |
| "not verified yet", "untested", "还没验证" | Nothing. An honest answer always passes. |

Bash commands on a fixed look-only list (`cd`, `cat`, `head`, `tail`, `less`, `more`, `grep`, `rg`, `ls`, `find`, `wc`, `stat`, `file`, `pwd`, `echo`, `git status`, `git diff`, `git log`, `git show`), alone or chained with `&&`, `;`, `||` or a pipe, don't count as a check. Neither does a command that failed, re-reading a file or updating a to-do list. Every other command counts, even one that only looks, such as `sed -n`, `awk`, `jq` or `git -C repo diff`. PowerShell commands always count. Editing `.md`, `.markdown`, `.rst` or `.adoc` files after the check is fine.

Cited proof means a commit hash, a pass count with no failures after it ("14 passed", but not "0 passed" or "2 passed, 3 failed"), a 2xx HTTP status ("HTTP 200", "201 OK"), "exit code 0", "CI green", or a link to a GitHub Actions run, a commit or a pull request. The Chinese equivalents (12 条测试全通过, 退出码 0, 全绿) count too.

Claims inside code blocks, inline code, double quotes (straight, curly, 「」 or 『』), blockquotes, questions and Markdown table rows are ignored, and so is a phrase in a slash-separated list like "tests pass/fail". A "done" claim doesn't count when the same clause has a negation or a condition before it: "not", "no", "never", "nothing", an "n't" word, "if", "once", "when", "until", "unless", "before" or their Chinese equivalents ("hasn't been deployed", "once tests pass"). A "not found" claim doesn't count after a condition, which also covers "make sure" and "in case" ("If the config file doesn't exist, the script creates it"). It never blocks twice in a row, so the agent can't get stuck, and if the hook itself crashes, it lets the turn end.

## Install

**Claude Code**, as a plugin:

```text
/plugin marketplace add Balance0014/claude-code-claim-check
/plugin install claim-check@claim-check
```

Then run `/reload-plugins` if Claude Code asks, or start a new session.

**Claude Code, without the plugin system:** save `claim_check.py` somewhere and add this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{ "hooks": [{ "type": "command", "command": "python3 /path/to/claim_check.py" }] }]
  }
}
```

**Codex and other agents:** `npx skills add Balance0014/claude-code-claim-check` installs the rules as a skill, plus a checklist for what "fixed" means. The agent reads them, but nothing makes it follow them. Codex has its own hook system, and an adapter for it isn't in this release.

## See your own number

Clone the repo and run:

```sh
python3 claim_check.py replay
```

It reads your past sessions in `~/.claude/projects` and prints how many "done" and "not found" claims the hook would have blocked and, for "done" claims, why. Nothing leaves your machine.

## Audit it

Every block is written to `~/.claude/claim-check/blocks.jsonl` (set `CLAIM_CHECK_LOG` to move it). Run `python3 claim_check.py report` in a clone of this repo to print the last ten. To turn the hook off, set `CLAIM_CHECK_OFF` to any value, in the shell that starts Claude Code or under `env` in `settings.json`. If it blocks something it shouldn't, [open an issue](https://github.com/Balance0014/claude-code-claim-check/issues) with the sentence it flagged and I'll fix the rule.

## What it can't do

- It matches phrases, so "I believe this works" gets through.
- One honest "not verified" anywhere lets the whole reply through: "The login bug is fixed. I haven't tested signup." passes, and so does an unrelated negative like "Fixed. Do not test on prod."
- A negation earlier in the same clause cancels a claim, so "There were no errors and everything is deployed" gets through.
- Any 7-to-40-character string of hex letters and digits counts as a commit hash, so an ID like `a1b2c3d4` passes as proof.
- Any MCP tool call counts as a check, even one that only reads.
- A command after the edit proves that something ran. It doesn't prove the command tested the right thing.
- It only understands claims in English and Chinese.
- Markdown table rows are skipped, so a status table with "fixed" or "已修复" in a cell gets through.
- A finding that uses claim words, like "I verified that it makes the model worse," is blocked as if it were finished work. So is a condition it doesn't recognize ("after it's merged that's done") or one set off by a comma ("once it's merged, that's done").
- It tracks edits made with Claude Code's edit tools. A file changed by a shell command such as `sed -i` isn't seen as an edit, and the `sed` command itself counts as the check.
- Claude Code writes the transcript asynchronously. If the last command hasn't been written when the hook runs, a claim that was checked can be blocked.
- On Windows, if `python3` isn't on your PATH, change it to `python` in `hooks/hooks.json` or your settings.

## Why I built it

I run a one-person company on long-running Claude Code sessions and scheduled AI jobs ([how that works](https://github.com/Balance0014/one-person-ai-company); this tool is [chapter 5](https://github.com/Balance0014/one-person-ai-company/blob/main/playbook/05-definition-of-done.md) turned into a hook). In the week before the first release, the older version on my machine blocked 19 times.

I found a bug in that older version while rewriting it for the first release. It treated tool results as new messages from me, so it only counted the lookups in the agent's very last message. Every search before that was invisible, and it once blocked a "not found" the agent had already checked with several searches. This version starts a turn only at a message a person typed, and there are tests for what the rules should block and what they should let through.

---

Made by [Balance Zhao](https://github.com/Balance0014). I post new tools on [X](https://x.com/balance0014).
