#!/usr/bin/env python3
"""claim-check: stop a coding agent from saying "done" (or "not found") without proof.

Claude Code runs this as a Stop hook when the agent tries to end its turn. It reads the
session transcript, looks at what the agent claimed in its final message, and checks what
the agent actually did:

  * "fixed / deployed / tests pass / done" needs something that ran after the last code
    edit and finished without failing: a shell command, an MCP tool, a URL fetch or a
    subagent. It can be in any earlier turn of the session, as long as no code was edited
    after it. Reading the file you just edited is not proof. A command that exited non-zero,
    timed out, was denied, was still running in the background, or ran zero tests is not
    proof either. If the last test or build after the edit failed, a later unrelated command
    doesn't make up for it. With no edits and nothing run, the reply can cite proof instead.
  * "doesn't exist / not found / nothing calls it" needs at least two lookups, and the
    second has to be a different kind of search. The same pattern again, in another
    folder or with different flags, is still one.

Missing proof: it blocks once, says exactly what is missing, and the agent either runs
the check or says plainly that it has not verified. Saying "not verified" always passes.
It never blocks twice in a row and never blocks because of its own errors. Every block
is appended to a log so you can audit false positives.

Some things are worth a look but are not proof of a false claim, so they never block:
tests that passed after a fix without ever failing before it, tests pointed at files
that never mention the changed file, a check run in a different project from the edits,
a build output run without a rebuild, a coverage report showing the changed file at 0%.
Those are shown to you (not to the agent) as a one-line note.

Turn it off with the plugin's own `enabled` setting, in your user settings. There is no
environment variable for that, and a project settings file is not read.

Usage:
  claim_check.py                      Stop hook mode (reads the hook payload on stdin)
  claim_check.py check FILE.jsonl     Judge the last turn of a transcript, print the verdict
  claim_check.py replay [DIR]         Judge every turn in past transcripts (default
                                      ~/.claude/projects) and print how many claims came without proof
  claim_check.py report [--log FILE]  Summarize the block log. The hook always writes
                                      ~/.claude/claim-check/blocks.jsonl; --log only
                                      chooses the file this command reads

Standard library only. Python 3.9+.
"""
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

# Not from the environment: a project env block can set variables, and this file is the audit log.
LOG = os.path.expanduser('~/.claude/claim-check/blocks.jsonl')

EDIT_TOOLS = {'Edit', 'MultiEdit', 'Write', 'NotebookEdit'}
READ_TOOLS = {'Read', 'Grep', 'Glob', 'LS'}
# What can prove a claim: running something. Shell commands, MCP tools (browsers, APIs), fetching a URL, a subagent.
# Bookkeeping such as TodoWrite proves nothing.
RUN_TOOLS = {'Bash', 'PowerShell', 'WebFetch', 'Agent', 'Task'}
SHELL_TOOLS = {'Bash', 'PowerShell'}
# Reading a background command's output, or stopping it, is not a new run: it settles the run that started it.
POLL_TOOLS = {'BashOutput', 'TaskOutput'}
STOP_TOOLS = {'KillShell', 'KillBash', 'TaskStop'}
# What counts as looking something up: reading, searching, or running a command.
LOOKUP_TOOLS = READ_TOOLS | RUN_TOOLS | {'WebSearch', 'NotebookRead'}


# MCP tools that only keep the session's books (Claude Code desktop's task list and wrap-up, to-do lists).
# They run after the work, so counting them as a check would let every unverified edit through.
BOOKKEEPING = re.compile(r'^mcp__ccd_|(?:^|_)(?:todos?|update_plan|wrap_up|mark_chapter)(?:_|$)', re.I)
# An MCP tool that writes a file (a filesystem server's write_file or edit_file): an edit, not a check.
MCP_WRITE = re.compile(r'(?:^|_)(?:write|edit|replace|patch|insert|append|create)(?:_|$)', re.I)


def runs(name):
    return name in RUN_TOOLS or (name.startswith('mcp__') and not BOOKKEEPING.search(name))


def looks(name):
    return name in LOOKUP_TOOLS or (name.startswith('mcp__') and not BOOKKEEPING.search(name))


def mcp_write_path(name, inp):
    """The file an MCP tool writes, or '' if it doesn't look like a file write."""
    if not name.startswith('mcp__') or not MCP_WRITE.search(name.rsplit('__', 1)[-1]):
        return ''
    return str(inp.get('path') or inp.get('file_path') or inp.get('filePath') or inp.get('filename') or '')
# Updating notes or docs after a check doesn't undo the check.
DOC_FILE = re.compile(r'\.(?:md|markdown|rst|adoc)$', re.I)
# Shell commands that only look at things: a lookup, never proof. Chained or piped, every part has to be one.
READ_ONLY_CMD = re.compile(r'^\s*(?:cd|cat|head|tail|less|more|grep|rg|ls|find|wc|stat|file|pwd|echo|git\s+(?:status|diff|log|show))(?=\s|$)')


def read_only(command):
    if '$(' in command or '`' in command:
        return False                                            # echo $(pytest) runs pytest
    command = re.sub(r"'[^']*'|\"[^\"]*\"", "''", command)        # a | inside quotes is not a pipe
    parts = [p for p in re.split(r'&&|\|\||[;|\n]', command) if p.strip()]
    return bool(parts) and all(READ_ONLY_CMD.match(p) for p in parts)
# A status report with no tool calls passes if it cites its proof.
PROOF = re.compile(
    r'(?<![\w-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}(?![\w-])'   # commit (letters and digits, so not a date; not part of a UUID)
    r'|\b(?:HTTP(?:/[\d.]+)?|status(?:\s+code)?)\s*[=:]?\s*2\d\d\b|\b2\d\d\s+(?:OK|Created|Accepted|No Content)\b'
    r'|\bexit(?: code| status)?\s*[=:]?\s*0\b|\bCI\s+(?:is\s+)?(?:green|passed)|https?://\S+/(?:actions/runs|commit|pull)/\S+'
    r'|(?<!不是)(?<!没有)(?<![没不])全绿|退出码\s*0', re.I)
# A pass count is proof only on a line with no failures and no "0 passed" (pytest prints "3 failed, 2 passed").
PASS_COUNT = re.compile(r'\b[1-9]\d*\s+(?:tests?\s+)?(?:passed|passing)\b|\b[1-9]\d*\s+tests?\b(?:(?!fail)[^.\n]){0,20}\b(?:pass|ok|green)'
                        r'|[1-9]\d*\s*(?:条|项|个)\s*(?:测试)?\s*(?:全)?(?:过|通过)', re.I)
FAILURES = re.compile(r'\b[1-9]\d*\s+(?:tests?\s+)?(?:fail(?:s|ed|ing|ures?)?|errors?)\b|\b0\s+(?:tests?\s+)?pass'
                      r'|[1-9]\d*\s*(?:条|项|个)\s*(?:测试)?\s*失败|(?<!\d)0\s*(?:条|项|个)\s*(?:测试)?\s*(?:全)?(?:过|通过)', re.I)


def has_proof(text):
    return bool(PROOF.search(text)) or any(PASS_COUNT.search(l) and not FAILURES.search(l) for l in text.split('\n'))

# "I did it" claims. Phrases, not single words, so "the fixed header" or "done()" don't count.
DONE = [
    r"\b(?:is|are|it's|that's|now|been|i've|i have|we've|we have|all|everything(?: is)?)\s+(?:now\s+)?"
    r"(?:fixed|resolved|deployed|shipped|live|done|complete|completed|working|in place|verified)\b",
    r"\b(?:i|we)\s+(?:fixed|deployed|shipped|verified|resolved|completed)\b",
    r"\btests?\s+(?:now\s+)?(?:pass|passes|passed|passing|are green|is green)\b",
    r"\bworks now\b|\bnow works\b|\bshould (?:now )?(?:work|pass|be fixed)\b",
    r"^\s*(?:[-*]\s*)?(?:✅\s*)?(?:done|fixed|deployed|shipped|all set|all done)(?:\s*(?:[.!:—-]|$)|\s+and\b)",
    # Chinese: "已验证的格式" is an adjective, "装修好了" contains "修好了"; neither is a claim.
    r"(?:已经?(?:完成|修复|修好|上线|部署|解决|恢复|接通|生效|验证)|(?<![装维])修好了|部署好了|做完了|测试(?:都|全部?)?通过|全部通过|搞定了)(?![的地得、]|成功的)",
]
# "It isn't there" claims.
ABSENT = [
    r"\b(?:does not|doesn't|did not|didn't|no longer)\s+exists?\b",
    r"\bno longer (?:used|referenced|called|imported|needed)\b",
    r"\b(?:is|are)\s*(?:not|n't)\s+(?:used|referenced|called|imported|running) anywhere\b",
    r"\bnot (?:found|used|referenced|called) anywhere\b",
    r"\bnothing (?:calls|uses|references|imports)\b",
    r"\b(?:there is|there's|there are) no (?:caller|callers|usage|usages|reference|references)\b",
    # Chinese: only first-person findings. "不存在" and "找不到" are too common in explanations to count.
    r"没找到|没搜到|没人调用|没有调用|没在跑|不在跑|没有用到|没用到",
]
# A "not found" phrase has its own negation, so only a condition cancels it ("if the file doesn't exist, …").
CONDITION = re.compile(r"\b(?:if|once|when|until|unless|before|make sure|in case)\b|如果|一旦|等到|假如|要是|万一|确保", re.I)
# A "done" claim is cancelled by a negation earlier in its clause ("hasn't been deployed"), or by a condition
# that opens the clause or the sentence ("once tests pass, it's done"). A condition inside a noun phrase
# doesn't cancel it: "the crash when saving is fixed" is still a claim.
NEGATION = re.compile(r"\b(?:not|never|nothing|no)\b|n't\b|没有|还没|尚未|并未|未能", re.I)
OPENS_WITH_CONDITION = re.compile(r"\s*(?:(?:and|but|so|then)\s+)?(?:if|once|when|until|unless|before|after|make sure|in case)\b"
                                  r"|\s*(?:如果|一旦|等到|假如|要是|万一|确保)", re.I)
CLAUSE_SEP = re.compile(r"[,，;；:：—–]|\s-\s|\b(?:and|but|so)\b|并且|而且|但是|所以", re.I)
LIST_SEP = re.compile(r'[/／|]')
# Honest uncertainty always passes.
HEDGE = (r"\b(?:not|haven't|have not|didn't|did not|couldn't|could not)\s+(?:yet\s+)?"
         r"(?:verified|verify|tested|test|checked|check|run (?:it|them|this|the tests?|the check))\b"
         r"|\b(?:not|haven't|didn't|did not)\s+re-?(?:run|ran|checked|verified|tested)\b"
         r"|\bunverified\b|\buntested\b|未验证|没验证|未验|没验|没测|未测试|没重新验|没复核|没核|未核|待核")


def _text(entry):
    c = (entry.get('message') or {}).get('content')
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return '\n'.join(i.get('text', '') for i in c if isinstance(i, dict) and i.get('type') == 'text')
    return ''


def is_human(entry):
    """A message you typed: a user entry with text, not a tool result and not an injected note."""
    if entry.get('type') != 'user' or entry.get('isMeta') or entry.get('isCompactSummary'):
        return False
    text = _text(entry).lstrip()
    if not text or text.startswith(('<task-notification>', '<system-reminder>')):
        return False                # background-task notices and injected notes, not you
    c = (entry.get('message') or {}).get('content')
    return isinstance(c, str) or any(isinstance(i, dict) and i.get('type') == 'text' for i in c or [])


def read_from(path, pos=0):
    """The entries from byte `pos` on, and where the next read starts. A half-written last line waits."""
    out = []
    with open(path, 'rb') as f:
        f.seek(pos)
        for line in f:
            try:
                e = json.loads(line.decode('utf-8', errors='replace'))
            except ValueError:
                if not line.endswith(b'\n'):
                    break               # still being written: read it next time
                e = None
            pos += len(line)
            if isinstance(e, dict):
                out.append(e)
    return out, pos


def load(path):
    return read_from(path)[0]


def turns(entries):
    """Split a transcript into turns: each starts at a message you typed."""
    cur = None
    for e in entries:
        if is_human(e):
            if cur is not None:
                yield cur
            cur = [e]
        elif cur is not None:
            cur.append(e)
    if cur is not None:
        yield cur


def strip_quoted(text):
    """Drop code, quotes and blockquotes: a claim inside quotes is someone else's words."""
    text = re.sub(r'```.*?```', ' ', text, flags=re.S)
    text = re.sub(r'`[^`\n]*`', ' ', text)
    text = re.sub(r'"[^"\n]*"|“[^”\n]*”|「[^」\n]*」|『[^』\n]*』', ' ', text)
    text = '\n'.join(l for l in text.split('\n') if not l.lstrip().startswith(('>', '|')))   # quotes, table rows
    return re.sub(r'[^。！？!?.,，;；\n]*[?？]', ' ', text)      # questions are not claims


def parse(turn):
    """What the agent did in this turn and what it said at the end."""
    actions, errors, final = [], set(), []
    for e in turn[1:]:
        c = (e.get('message') or {}).get('content')
        if e.get('type') == 'assistant' and isinstance(c, list):
            for i in c:
                if not isinstance(i, dict):
                    continue
                if i.get('type') == 'tool_use':
                    name = i.get('name') or ''
                    inp = i.get('input') if isinstance(i.get('input'), dict) else {}
                    path = str(inp.get('file_path') or '')
                    if name in EDIT_TOOLS and DOC_FILE.search(path):
                        name = 'EditDoc'
                    elif name == 'Bash' and read_only(str(inp.get('command') or '')):
                        name = 'BashRead'
                    # The input is what tells two searches of the same kind apart.
                    # Edits don't need it, and a Write can carry the whole file.
                    actions.append((i.get('id'), name, inp if looks(name) or name == 'BashRead' else None))
                    final = []          # text before a tool call is narration, not the answer
                elif i.get('type') == 'text':
                    final.append(i.get('text', ''))
        elif e.get('type') == 'user' and isinstance(c, list):
            for i in c:
                if isinstance(i, dict) and i.get('type') == 'tool_result' and i.get('is_error'):
                    errors.add(i.get('tool_use_id'))
    raw = '\n'.join(final)
    return actions, errors, strip_quoted(raw), raw


# ---------------------------------------------------------------------------
# The session's timeline: what was edited, what ran, and how each run ended.
#
# Claude Code's transcript format is internal and changes between versions. Everything below reads
# what is there and falls back to the plain is_error flag when a field is missing.

# Tests and builds are checks whose result can be read from their own summary line, not only the exit code.
# Linters and type checkers (eslint, ruff, flake8, pylint, mypy, go vet) are ordinary commands: they exit
# non-zero on any warning, and a lint warning after green tests doesn't make "the tests pass" false.
# Each pattern is matched at the start of one step of a command (after `cd …;`, `X=1`, `npx`, `uv run`),
# so `git add test_x.py` or a heredoc that mentions pytest is not a test run.
TEST_CMD = re.compile(r"""(?ix)^(?:
    (?:\S*/)?(?:pytest|py\.test|tox|nox|nosetests|jest|vitest|mocha|ava|karma|rspec|phpunit|ctest|bats)(?![\w-])
  | python\d?(?:\.\d+)?\s+(?:-\S+\s+)*-m\s+(?:pytest|unittest|nose2?|tox)\b
  | python\d?(?:\.\d+)?\s+(?:-\S+\s+)*(?:\S*/)?(?:test_[\w-]*|[\w-]*_test|tests?|run_?tests?)\.py\b
  | (?:node|bun|tsx|ts-node|deno\s+run|bash|sh|zsh)\s+(?:-\S+\s+)*(?:\S*/)?(?:test_[\w-]*|[\w-]*[_-]test|tests?|run_?tests?|[\w-]+\.(?:test|spec))\.(?:[cm]?[jt]sx?|sh)\b
  | (?:\./)?(?:\S*/)?(?:run_?tests?|tests?)\.sh\b
  | (?:go|cargo|swift|dotnet|mix|deno|bun|flutter|dart|zig)\s+test\b
  | node\s+--test\b
  | (?:npm|pnpm|yarn|bun)\s+(?:(?:--filter|-F|--prefix|-C|--dir|--cwd|-w|--workspace)(?:=|\s+)\S+\s+|--?[\w-]+\s+)*(?:run\s+)?test(?::[\w:-]+)?(?![\w-])
  | python\d?(?:\.\d+)?\s+(?:-\S+\s+)*(?:\S*/)?manage\.py\s+test\b
  | (?:playwright\s+test|cypress\s+run)\b
  | (?:\./)?(?:mvnw?|gradlew?)\b.*\b(?:test|check|verify)\b
  | make\s+(?:(?:-C|-f|--directory|--file)(?:=|\s+)\S+\s+|-\S+\s+)*(?:test|tests|check)\b
)""")
BUILD_CMD = re.compile(r"""(?ix)^(?:
    (?:\S*/)?(?:tsc|webpack|esbuild|rollup|xcodebuild|msbuild|ninja|swiftc|javac|rustc)(?![\w-])
  | (?:vite|next|nuxt|astro|turbo)\s+build\b
  | (?:go|cargo|swift|dotnet|bazel)\s+(?:build|check)\b
  | (?:npm|pnpm|yarn|bun)\s+(?:(?:--filter|-F|--prefix|-C|--dir|--cwd|-w|--workspace)(?:=|\s+)\S+\s+)*(?:run\s+)?(?:build|typecheck|type-check|compile)(?![\w:-])
  | (?:\./)?(?:mvnw?|gradlew?)\b.*\b(?:compile|package|build|assemble)\b
  | make(?:\s+(?:(?:-C|-f|--directory|--file)(?:=|\s+)\S+|-\S+))*(?:\s+(?:all|build|compile))?\s*$
  | python\d?(?:\.\d+)?\s+(?:-\S+\s+)*-m\s+(?:py_compile|compileall)\b | node\s+--check\b | bash\s+-n\b
)""")
# A watcher never finishes, so it's not a check. `--watchAll=false` turns it off; `npm test -w pkg` is a workspace.
WATCH = re.compile(r'--watch(?:All)?(?![\w-]|=(?:false|0)\b)|^(?:\S*/)?(?:vitest|jest|mocha|tsc)\b.*(?<![\w-])-w(?![\w-])')
_WRAP = re.compile(r'^(?:[A-Za-z_]\w*=\S*\s+|(?:env|time|nice|sudo|exec|command|npx|bunx|pnpx|xvfb-run)\s+|timeout\s+\S+\s+'
                   r'|(?:uv|poetry|pipenv|pdm|hatch)\s+run\s+(?:--?[\w-]+(?:=\S+)?\s+)*|bundle\s+exec\s+'
                   r'|(?:yarn|pnpm|bun)\s+(?:exec\s+|dlx\s+)?(?=(?:jest|vitest|mocha|playwright|cypress|ava|karma|tsc)\b)'
                   r'|docker(?:-compose|\s+compose)?\s+(?:exec|run)\s+(?:-[\w-]+\s+)*\S+\s+'
                   r'|(?:if|elif|while|until|then|do|else)\s+|!\s+)+')
SHELL_C = re.compile(r"""(?<![\w-])(?:ba|z)?sh\s+-\w*c\s+(['"])(.*?)\1""", re.S)    # bash -c "cd x && pytest"
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)


def _steps(command):
    """The steps of a shell command (split at ; && || and newlines), each as its first piped program.
    Heredoc bodies and quoted strings are blanked first: they are data, not commands."""
    text = SHELL_C.sub(lambda m: m.group(2), HEREDOC.sub('<<EOF', command or ''))
    masked = re.sub(r"'[^']*'|\"[^\"]*\"", "''", text)
    out = []
    for seg in re.split(r'&&|\|\||[;\n]', masked):
        first = _WRAP.sub('', seg.split('|')[0].strip())
        if first:
            out.append(first)
    return out


def _step_kind(step):
    if WATCH.search(step):
        return 'other'
    if TEST_CMD.match(step):
        return 'test'
    if BUILD_CMD.match(step):
        return 'build'
    return 'other'


def check_kind(command):
    """'test', 'build' or 'other', from the command line."""
    kinds = {_step_kind(s) for s in _steps(command)}
    return 'test' if 'test' in kinds else 'build' if 'build' in kinds else 'other'


def _ends_with_check(command):
    """The command's exit code is the check's own: the test or build is its last step."""
    steps = _steps(command)
    return bool(steps) and _step_kind(steps[-1]) != 'other'


def _segments(command):
    """The steps of a shell command in order, as (text, text with quoted strings blanked). Pipes stay in."""
    text = SHELL_C.sub(lambda m: m.group(2), HEREDOC.sub('<<EOF', command or ''))
    masked = re.sub(r"'[^']*'|\"[^\"]*\"", lambda m: m.group(0)[0] + ' ' * (len(m.group(0)) - 2) + m.group(0)[-1], text)
    out, start = [], 0
    for m in list(re.finditer(r'&&|\|\||[;\n]', masked)) + [None]:
        end = m.start() if m else len(text)
        if text[start:end].strip():
            out.append((text[start:end].strip(), masked[start:end].strip()))
        start = m.end() if m else end
    return out


def _write_order(command):
    """'edit_last' if the command's last step that writes files comes after the last step that runs something
    (a test, a build, a script), 'run_last' if a run comes after it, None if no step writes."""
    last_write = last_run = -1
    for k, (text, masked) in enumerate(_segments(command)):
        first = _WRAP.sub('', masked.split('|')[0].strip())
        kind = _step_kind(first)
        if kind == 'other' and (WRITES_SHELL.search(masked) or WRITES_CODE.search(text)):
            last_write = k
        elif kind != 'other' or RUNNER.match(first):
            last_run = k
    if last_write < 0:
        return None
    return 'run_last' if last_run > last_write else 'edit_last'


def _intent(command):
    """What a command says about itself: its # comments and what it echoes."""
    text = HEREDOC.sub('<<EOF', command or '')
    masked = re.sub(r"'[^']*'|\"[^\"]*\"", lambda m: 'x' * len(m.group(0)), text)
    parts = [text[m.end():].split('\n', 1)[0] for m in re.finditer(r'(?:^|\s)#', masked)]
    parts += re.findall(r'\b(?:echo|printf)\s+([^;&|\n]*)', text)
    return '\n'.join(parts)


def expects_fail(command):
    """The command says this run should fail: a comment or an echo says so, it runs the old code, or it's a mutation tool."""
    return bool(EXPECT_FAIL.search(_intent(command)) or OLD_CODE.search(command or '') or MUTATION.search(command or ''))


def _reruns_a_test(command):
    """The same test runs twice in one command (before and after swapping something in): red, then green.
    Two different suites (`pytest tests/unit; pytest tests/integration`) are not that."""
    keys = [check_key(s) for s in _steps(command) if _step_kind(s) == 'test']
    keys = [k for k in keys if k]
    return len(set(keys)) < len(keys)


# A command that writes files. Claude Code's record of files changed during a command can also catch
# another session or a `git pull` changing them, so a change counts as this agent's edit only if the
# command itself writes, doesn't sync with git, and the file is source code: scripts write ledgers,
# caches and state files (JSON, YAML, text) all the time.
WRITES_SHELL = re.compile(r"""(?x)\bsed\s+(?:-\S+\s+)*-i|\bperl\s+-\w*i|(?<![0-9&>])>>?\s*(?!/dev/null|&)[\w./~"'$-]|\btee\s
    |(?:^|\|\s*|\b(?:sudo|then|do|xargs)\s+)(?:cp|mv|install|ln)\s|\bpatch\s|\bgit\s+apply\b""")
WRITES_CODE = re.compile(r"""\bwrite_(?:text|bytes)\(|\.write\(|open\([^)\n]*['"][wax]\+?b?['"]|json\.dump\(|writeFileSync|fs\.(?:promises\.)?writeFile""")
# Running a program after writing it: an interpreter, a local script, a request. Not `git add` or `ls`.
RUNNER = re.compile(r'^(?:python\d?(?:\.\d+)?|node|bun|deno|ruby|php|perl|go\s+run|cargo\s+run|bash|sh|zsh|\./\S+|curl|wget|http)(?:\s|$)')
GIT_SYNC = re.compile(r'\bgit\s+(?:-C\s+\S+\s+)?(?:pull|merge|rebase|checkout|switch|reset|restore|stash|cherry-pick|revert|clone|am|worktree|submodule)\b')
SOURCE = re.compile(r'\.(?:py|pyi|js|mjs|cjs|jsx|ts|tsx|mts|cts|go|rs|java|kt|kts|scala|swift|m|mm|c|cc|cpp|cxx|h|hpp|cs|rb|php|pl|lua|r|dart'
                    r'|ex|exs|erl|hs|clj|sh|bash|zsh|ps1|sql|html?|css|scss|sass|less|vue|svelte|astro)$', re.I)


EXIT = re.compile(r'^(?:Error:\s*)?Exit code (-?\d+)')
# The command never really ran: denied, rejected, interrupted, or the runner itself was missing.
NOT_RUN = re.compile(r"Permission (?:for|to) .{0,300}?denied|doesn't want to proceed|User rejected|^\s*<tool_use_error>|"
                     r"\[Request interrupted|is temporarily unavailable|command not found|"
                     r"is not recognized as an internal or external command|No module named \S*(?:pytest|unittest)|"
                     r"no test specified|Missing script", re.I)
# Said in the command itself: this run is supposed to fail (a mutation test, the bug reproduced on the old code).
# Matched only in the command's own `# comment` and in what it echoes, never in file or test names.
EXPECT_FAIL = re.compile(r'expect(?:ed|s)?\s+(?:it\s+|them\s+)?(?:to\s+)?fail|should\s+fail|must\s+fail|on\s+purpose|deliberately'
                         r'|\bmutant|mutation|必红|应该?红|预期(?:失败|红)|故意|应该?失败', re.I)
# Running the tests on the old code: stash the change (or check out the old file) first. Red there is the point.
OLD_CODE = re.compile(r'\bgit\s+(?:-C\s+\S+\s+)?(?:stash(?:\s+(?:push|save)\b|\s*(?=&&|;|\n|\||$))|checkout\s+\S+\s+--\s'
                      r'|restore\s+(?:-\S+\s+)*--source\b|worktree\s+add\b|show\s+\S+:\S+|apply\s+(?:-\S+\s+)*(?:-R|--reverse)\b)'
                      r'|\bpatch\s+(?:-\S+\s+)*(?:-R|--reverse)\b')
MUTATION = re.compile(r'(?:^|[\s;&|])(?:mutmut|stryker|mutpy|cosmic-ray|pitest)\b')
TIMED_OUT = re.compile(r'timed out|did not complete within', re.I)
BACKGROUND = re.compile(r'running in background with ID:\s*(\w+)|^\s*Async agent launched|moved to the background', re.I)
# What a test or build prints when it failed, even if a pipe (`| tail`) or `|| true` turned the exit code into 0.
OUT_FAIL = re.compile(r'''(?imx)
    ^=+\s.*\b[1-9]\d*\s+(?:failed|errors?)\b                 # pytest summary bar
  | ^\s*[1-9]\d*\s+failed\b.*\bin\s+[\d.]+m?s\b                # pytest -q
  | ^(?:FAILED|ERROR)\s+\S+::                                  # pytest short summary
  | ^FAILED\s+\((?:failures|errors)=                           # unittest
  | ^Tests?:\s+.*\b[1-9]\d*\s+failed\b | ^Test\s+Suites:\s+.*\b[1-9]\d*\s+failed\b     # jest
  | ^\s*Tests?\s+[1-9]\d*\s+failed\b                           # vitest
  | ^\s*[1-9]\d*\s+failing\b                                   # mocha
  | ^---\s+FAIL:|^FAIL\s+\S                                    # go, jest
  | ^test\s+result:\s+FAILED                                   # cargo
  | ^not\s+ok\s+\d+(?!.*\#\s*(?:TODO|SKIP))|^\#\s+fail\s+[1-9]                         # TAP: node --test, bats
  | ^error\[E\d+\]: | ^error:\s+(?:could\s+not\s+compile|aborting\s+due\s+to|linking\s+with)    # cargo, rustc
  | \berror\s+TS\d+: | ^\S+:\d+:\d+:\s+(?:fatal\s+)?error: | ^Found\s+[1-9]\d*\s+errors?\b
  | ^npm\s+ERR!|^npm\s+error\s|\bELIFECYCLE\b|^error\s+Command\s+failed
  | ^make(?:\[\d+\])?:\s+\*\*\*.*\bError\s+\d+
  | ^BUILD\s+FAILED|^\[ERROR\]\s+BUILD\s+FAILURE|^FAILURE:\s+Build\s+failed|^\*\*\s+(?:BUILD|TEST)\s+FAILED\s+\*\*
''')
# "3 failed" counts; "run 43 fail 0" (a label, then its number) doesn't.
FAIL_COUNT = re.compile(r'\b[1-9]\d*\s+(?:tests?\s+)?(?:fail(?:s|ed|ing|ures?)?|errors?)\b(?!\s*[:=]?\s*\d)'
                        r'|[1-9]\d*\s*(?:条|项|个)\s*(?:测试)?\s*失败', re.I)
NO_TESTS = re.compile(r'(?im)\bcollected 0 items\b|\bno tests ran\b|^Ran 0 tests?\b|^NO TESTS RAN\b|\bNo tests found\b|^\s*0 passing\b'
                      r'|^running 0 tests\b|\bno tests to run\b|\bno test files\b|^#\s+tests\s+0\b')
SOME_TESTS = re.compile(r'(?im)\b[1-9]\d*\s+(?:tests?\s+)?(?:passed|passing)\b|^Ran\s+[1-9]\d*\s+tests?\b|^ok\s+\S+\s+[\d.]+s'
                        r'|^test result: ok\. [1-9]|^#\s+pass\s+[1-9]|(?:^|:\s*)OK(?:\s+\(\w+=\d+\))?\s*$')
TRACEBACK = re.compile(r'Traceback \(most recent call last\):')
TRACEBACK_END = re.compile(r'^\w+(?:\.\w+)*(?:Error|Exception)\b')


def _result_text(item):
    c = item.get('content')
    if isinstance(c, list):
        return '\n'.join(str(i.get('text', '')) for i in c if isinstance(i, dict))
    return c if isinstance(c, str) else ''


def _short(text, n=70):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    return text if len(text) <= n else text[:n - 1] + '…'


def _summary(kind, text, last_decides=False):
    """('fail' or 'pass', line) from the summary lines a test runner or build printed, or None.
    A runner's failure line anywhere wins, so one failing suite of several isn't hidden by the next one
    passing. When the command runs the old code on purpose (red, then green), the last summary decides.
    A bare count like "3 failed" is weaker, since logs print such lines too: it decides only as the last one."""
    strong = last = None
    for line in (text or '').rstrip().split('\n')[-2000:]:
        if OUT_FAIL.search(line):
            last = ('fail', line)
            strong = strong or last
        elif kind == 'test' and FAIL_COUNT.search(line):
            last = ('fail', line)
        elif SOME_TESTS.search(line):
            last = ('pass', line)
    return last if last_decides else (strong or last)


def read_output(kind, text, last_decides=False):
    """(state, why) for a command that exited 0, from what it printed: 'pass', 'fail' or 'empty' (ran no tests)."""
    lines = (text or '').rstrip().split('\n')
    tail = '\n'.join(lines[-80:])
    if kind != 'other':
        found = _summary(kind, text, last_decides)
        if found and found[0] == 'fail':
            return 'fail', 'printed a failure (' + _short(found[1], 60) + ')'
        if kind == 'test' and not found and NO_TESTS.search(tail) and not SOME_TESTS.search(text):
            return 'empty', 'ran no tests'
        if found:
            return 'pass', ''
    last = lines[-1].strip() if lines else ''
    if TRACEBACK.search(tail) and TRACEBACK_END.match(last):
        return 'fail', 'ended with a Python traceback (' + _short(last, 50) + ')'
    return 'pass', ''


def _failed(text):
    """(state, why) for a tool result marked as an error."""
    head = (text or '').strip()[:400]
    m = EXIT.match(head)
    if TIMED_OUT.search(head) and not m:
        return 'timeout', 'timed out'
    if NOT_RUN.search(head) or (m and m.group(1) in ('126', '127')):
        return 'not_run', 'did not run (' + _short(re.sub(r'^(?:Error:\s*)?Exit code \d+\s*', '', head), 50) + ')'
    if m:
        return 'fail', 'exited with code ' + m.group(1)
    return 'fail', 'returned an error'


def _tag(block, name):
    m = re.search(r'<%s>(.*?)</%s>' % (name, name), block, re.S)
    return m.group(1).strip() if m else ''


def _read_tail(path, size=262144):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            return f.read().decode('utf-8', 'replace')
    except (OSError, ValueError):
        return None


def is_code(path):
    """An edit that can change what the code does. Docs and notes don't."""
    return bool(path) and not DOC_FILE.search(str(path))


def _mark_expected(ev, output, expect=True):
    """A run meant to show red. Ending red, it doesn't count as the last check. Red then green inside it is both."""
    if ev['state'] != 'pass':
        ev['expected'] = expect
    else:
        found = _summary(ev['kind'], output)
        ev['red_inside'] = bool(found and found[0] == 'fail')


class Evidence:
    """Everything the session did, in order: edits, and runs with how each one ended.

    Built from the whole transcript, so a check from an earlier turn is seen. Each event is a dict:
    k: 'edit' | 'run' | 'look'; turn; id; name; cwd; for edits paths, code, ok; for runs cmd, kind,
    state ('open' = no result yet, 'pass', 'fail', 'timeout', 'not_run', 'pending', 'empty') and why.
    """

    def __init__(self, seeded=False):
        self.events, self.by_id, self.tasks, self.waiting = [], {}, {}, {}
        self.turn = 0
        if seeded:              # judge(..., earlier=True): a check passed in an earlier turn
            self.events.append({'k': 'run', 'turn': -1, 'id': None, 'name': 'earlier', 'cwd': None, 'cmd': '',
                                'kind': 'other', 'state': 'pass', 'why': '', 'out': ''})

    def next_turn(self):
        self.turn += 1

    def feed_turn(self, turn):
        self.next_turn()
        for e in turn or []:
            self.feed(e)

    def feed(self, entry):
        c = (entry.get('message') or {}).get('content')
        if entry.get('type') == 'assistant' and isinstance(c, list):
            for i in c:
                if isinstance(i, dict) and i.get('type') == 'tool_use':
                    self._use(i, entry)
        elif entry.get('type') == 'user':
            if isinstance(c, str):
                self._notice(c)
            elif isinstance(c, list):
                results = [i for i in c if isinstance(i, dict) and i.get('type') == 'tool_result']
                extra = entry.get('toolUseResult') if len(results) == 1 else None
                for i in c:
                    if not isinstance(i, dict):
                        continue
                    if i.get('type') == 'tool_result':
                        self._result(i, extra if isinstance(extra, dict) else {})
                    elif i.get('type') == 'text':
                        self._notice(i.get('text', ''))

    def _use(self, item, entry):
        name = item.get('name') or ''
        inp = item.get('input') if isinstance(item.get('input'), dict) else {}
        uid = item.get('id')
        base = {'turn': self.turn, 'id': uid, 'name': name, 'cwd': entry.get('cwd')}
        task = str(inp.get('task_id') or inp.get('bash_id') or inp.get('shell_id') or '')
        if name in POLL_TOOLS or name in STOP_TOOLS:
            self.waiting[uid] = ('poll' if name in POLL_TOOLS else 'stop', task)
            return
        if name in EDIT_TOOLS or mcp_write_path(name, inp):
            path = str(inp.get('file_path') or inp.get('notebook_path') or '') if name in EDIT_TOOLS else mcp_write_path(name, inp)
            ev = dict(base, k='edit', paths=[path], code=is_code(path), ok=None)
        elif runs(name):
            cmd = str(inp.get('command') or '') if name in SHELL_TOOLS else ''
            look = name == 'Bash' and read_only(cmd)       # kept only to notice a file it wrote (cat > f)
            ev = dict(base, k='look' if look else 'run', cmd=cmd, kind=check_kind(cmd), state='open', why='', out='')
        else:
            return
        self.events.append(ev)
        if uid:
            self.by_id[uid] = ev

    def _result(self, item, extra):
        uid = item.get('tool_use_id')
        err = bool(item.get('is_error'))
        text = _result_text(item)
        if uid in self.waiting:
            what, task = self.waiting.pop(uid)
            self._task_result(task, text, what, err)
            return
        ev = self.by_id.get(uid)
        if ev is None or ev.get('settled'):
            return
        ev['settled'] = True
        if ev['k'] == 'edit':
            ev['ok'] = not err                         # a failed or rejected edit changed nothing
            return
        # Claude Code records the files a shell command changed (sed -i, a heredoc, git checkout). It's this
        # agent's edit only if a step of the command writes, it doesn't sync with git, and the file is source code.
        diff = extra.get('bashEditDiff')
        if ev['name'] == 'Bash' and isinstance(diff, dict) and not GIT_SYNC.search(ev['cmd']):
            source = [str(f.get('filePath')) for f in diff.get('files') or []
                      if isinstance(f, dict) and f.get('filePath') and SOURCE.search(str(f.get('filePath')))]
            order = _write_order(ev['cmd']) if source else None
            if order == 'edit_last' or (order and ev['k'] == 'look'):
                ev.update(k='edit', paths=source, code=True, ok=True, via='shell')
                return
            if order == 'run_last':                    # `sed -i … && pytest`: the edit, then the run that checks it
                k = next(i for i, e in enumerate(self.events) if e is ev)
                self.events.insert(k, {'turn': ev['turn'], 'id': None, 'name': 'Bash', 'cwd': ev['cwd'], 'k': 'edit',
                                       'paths': source, 'code': True, 'ok': True, 'via': 'shell'})
        if ev['k'] != 'run':
            return
        expect = ev['kind'] != 'other' and expects_fail(ev['cmd'])
        last_decides = expect or (ev['kind'] == 'test' and _reruns_a_test(ev['cmd']))
        if err:
            ev['state'], ev['why'] = _failed(text)
            # `pytest; git push` exits with git's code. It's the test's failure only if the test is the
            # last step or its own summary says so; otherwise it's just a failed command.
            if ev['kind'] != 'other':
                found = _summary(ev['kind'], text, last_decides)
                if expect:
                    ev['expected'] = True              # meant to go red (the old code, a mutation test): still a red run
                elif found and found[0] == 'fail':
                    ev['why'] = '%s; printed %s' % (ev['why'], _short(found[1], 50))
                elif found or not _ends_with_check(ev['cmd']):
                    ev['kind'] = 'other'               # the tests passed; another step failed
                ev['out'] = text[-20000:]
            return
        m = BACKGROUND.search(text[:400])
        task = extra.get('backgroundTaskId') or (m.group(1) if m and m.group(1) else None)
        if not task and m:
            a = re.search(r'agentId:\s*(\w+)', text)
            task = a.group(1) if a else None
        if m or extra.get('backgroundTaskId'):
            timed = extra.get('timedOutAfterMs') or TIMED_OUT.search(text[:400])
            ev['state'] = 'pending'
            ev['why'] = 'timed out and was moved to the background' if timed else 'was still running in the background'
            if task:
                self.tasks[str(task)] = ev
            return
        ev['state'], ev['why'] = read_output(ev['kind'], text, last_decides)
        if last_decides:
            _mark_expected(ev, text, expect)
        if ev['kind'] != 'other':
            ev['out'] = text[-20000:]

    def _settle(self, ev, status, code, output, summary=''):
        if ev is None or ev.get('k') != 'run' or ev['state'] != 'pending':
            return
        status = (status or '').lower()
        if status in ('', 'running', 'pending'):
            return
        if status in ('killed', 'stopped'):
            ev['state'], ev['why'] = 'not_run', 'was stopped before it finished'
        elif status == 'completed' and (code in (None, '0') or 'No matches found' in summary):
            expect = ev['kind'] != 'other' and expects_fail(ev['cmd'])
            last_decides = expect or (ev['kind'] == 'test' and _reruns_a_test(ev['cmd']))
            ev['state'], ev['why'] = read_output(ev['kind'], output or '', last_decides)
            if last_decides:
                _mark_expected(ev, output or '', expect)
            if output and ev['kind'] != 'other':
                ev['out'] = output[-20000:]
        else:
            ev['state'], ev['why'] = 'fail', ('exited with code %s' % code) if code not in (None, '0') else 'failed'
            if ev['kind'] != 'other' and expects_fail(ev['cmd']):
                ev['expected'] = True

    def _task_result(self, task, text, what, err):
        ev = self.tasks.get(task)
        if what == 'stop':
            if not err:
                self._settle(ev, 'stopped', None, None)
            return
        if err:
            return
        code = re.search(r'<exit_code>(-?\d+)</exit_code>', text)
        out = re.search(r'<(?:output|stdout)>(.*?)(?:</(?:output|stdout)>|\Z)', text, re.S)
        self._settle(ev, _tag(text, 'status'), code.group(1) if code else None, out.group(1) if out else None)

    def _notice(self, text):
        """A background task finished: <task-notification> names the tool call that started it."""
        if '<task-notification>' not in (text or ''):
            return
        for block in re.findall(r'<task-notification>(.*?)(?:</task-notification>|\Z)', text, re.S):
            ev = self.by_id.get(_tag(block, 'tool-use-id')) or self.tasks.get(_tag(block, 'task-id'))
            summary = _tag(block, 'summary')
            code = re.search(r'exit code (-?\d+)', summary)
            output = None
            if ev is not None and ev.get('kind') != 'other' and _tag(block, 'output-file'):
                output = _read_tail(_tag(block, 'output-file'))   # gone in old sessions; then the status alone decides
            self._settle(ev, _tag(block, 'status'), code.group(1) if code else None, output, summary)

    def last_edit(self):
        """Index of the last code edit that took effect, or -1."""
        for k in range(len(self.events) - 1, -1, -1):
            e = self.events[k]
            if e['k'] == 'edit' and e['code'] and e['ok'] is not False:
                return k
        return -1


# A claim made while saying that something still fails or is still running isn't hiding the result.
# "Pre-existing" or 无关 alone isn't enough ("kept the existing API"); it has to be about a failure or a test.
ACK_FAIL = re.compile(r'\b[1-9]\d*\s+(?:tests?\s+)?fail(?:s|ed|ing|ures?)?\b|\b[1-9]\d*\s+tests?\s+errors?\b'
                      r'|\bstill\s+(?:fail|fails|failing|broken|red)\b|\bknown\s+(?:failure|issue)\b'
                      r'|\b(?:fail\w*|tests?|errors?)\b\W+(?:\w+\W+){0,6}?(?:pre-?existing|unrelated|not\s+related|flaky)\b'
                      r'|\b(?:pre-?existing|unrelated|flaky)\W+(?:\w+\W+){0,3}?(?:fail\w*|tests?|errors?)\b'
                      r'|[1-9]\d*\s*(?:条|项|个)\s*(?:测试)?\s*(?:失败|报错)'
                      r'|(?:原有|本来就|之前就|已知|无关)[^。！？\n]{0,15}(?:失败|报错|没过)|(?:失败|报错|没过)[^。！？\n]{0,15}(?:原有|本来就|之前就|已知|无关)'
                      r'|(?:仍|还)(?:有|在|是)?.{0,12}(?:失败|报错|没过)', re.I)
NOT_PROOF = ('fail', 'timeout', 'not_run', 'pending', 'empty')


def _describe(ev):
    what = '`%s`' % _short(ev['cmd'], 60) if ev.get('cmd') else ev.get('name') or 'the check'
    return '%s %s' % (what, ev.get('why') or 'did not pass')


def _detail(runs_after):
    """Why the runs after the last edit don't count, from the last one that didn't pass."""
    bad = [e for e in runs_after if e['state'] in NOT_PROOF]
    return (' (' + _describe(bad[-1]) + ')') if bad else ''


def judge(turn, final_text=None, earlier=False):
    """Return None if the turn is fine, else (kind, claim, what's missing).

    final_text: the agent's last message, from the Stop hook's last_assistant_message field.
    The transcript is written asynchronously and can miss that message, so it wins when present.
    earlier: an Evidence object with the session's earlier turns (this turn is added to it), or
    True to mean only "a check passed in an earlier turn and no code changed after it".
    """
    ev = earlier if isinstance(earlier, Evidence) else Evidence(seeded=bool(earlier))
    ev.feed_turn(turn)
    return verdict(ev, turn, final_text)


def verdict(ev, turn, final_text=None):
    """judge() for a turn that has already been fed into the session's Evidence."""
    actions, errors, said, raw = parse(turn)
    if final_text:
        said, raw = strip_quoted(final_text), final_text
    if not said.strip() or re.search(HEDGE, said, re.I):
        return None
    claim = _first(DONE, said)
    if claim:
        v = _judge_done(ev, actions, claim, raw)
        if v:
            return v
    return judge_absent(turn, final_text)


def _judge_done(ev, actions, claim, raw):
    le = ev.last_edit()
    after = [e for e in ev.events[le + 1:] if e['k'] == 'run']
    # 'open' means no result was written yet. The transcript lags, so that isn't held against the claim.
    passed = [e for e in after if e['state'] in ('pass', 'open')]
    edited_now = le >= 0 and ev.events[le]['turn'] == ev.turn
    # Tests and builds: the latest finished one decides. A passing `ls -la` doesn't outweigh a failing `pytest`.
    # A run the command itself says should fail (the old code, a mutation test) doesn't decide.
    checks = [e for e in after if e['kind'] != 'other' and not e.get('expected')
              and e['state'] in ('pass', 'fail', 'timeout', 'empty')]
    if checks and checks[-1]['state'] == 'fail' and ACK_FAIL.search(raw):
        return None                    # it says what still fails: that's the result, not a hidden one
    # Code changed and nothing since has run: whatever the reply cites, the transcript shows no check of this code.
    if le >= 0 and not passed:
        return 'done', claim, ('nothing ran successfully after the last edit' + ('' if edited_now else ', made in an earlier turn')
                               + _detail(after))
    if checks and checks[-1]['state'] != 'pass':
        return 'done', claim, 'the last test or build after the edit did not pass: ' + _describe(checks[-1])
    # No code edits in the session: a wrap-up can lean on a check from an earlier turn, or cite proof.
    # Proof is looked for in the raw text: a hash in backticks or before a question still counts.
    if not passed and not edited_now and not has_proof(raw) and not any(n == 'EditDoc' for _, n, _ in actions):
        return 'done', claim, ('nothing ran in this turn and you cited no proof' if not actions else
                               'nothing you ran in this turn tested anything (only lookups, to-do updates or failed commands), '
                               'and you cited no proof') + _detail(after)
    return None


# ---------------------------------------------------------------------------
# Notes: worth a look, not proof that a claim is false. They never block; the user sees them.

FIX = re.compile(r'\b(?:fix(?:ed|es)?|resolved?|repaired)\b|修复|修好|解决', re.I)
TEST_FILE = re.compile(r'(?:^|/)(?:test_[^/]*|[^/]*_test\.\w+|[^/]*\.(?:test|spec)\.\w+|tests?/.*)$')
COMPILED = re.compile(r'\.(?:ts|tsx|mts|cts|go|rs|java|kt|kts|swift|c|cc|cpp|cxx|h|hpp|cs|scala|m|mm)$', re.I)
ARTIFACT = re.compile(r'(?:^|[\s;&|=])(?:node\s+|java\s+-jar\s+|bun\s+)?\.?/?(?:dist|build|out|target/(?:debug|release)|\.build/(?:debug|release)|bin)/[\w./-]+')
_WRAPPERS = {'env', 'time', 'sudo', 'nice', 'npx', 'bunx', 'pnpx', 'uv', 'poetry', 'pipenv', 'pdm', 'hatch', 'run', 'exec'}
_RUNNER_FLAG_ARGS = {'-k', '-m', '-c', '-p', '--rootdir', '--config', '-t', '--testNamePattern', '-run', '--grep', '-g',
                     '--reporter', '--maxfail', '-n', '--tb', '-o', '--cov', '--cov-report', '--deselect', '--ignore'}


def _test_part(cmd):
    for part in _split_cmd(cmd):
        if check_kind(part) == 'test':
            return part
    return None


def check_key(cmd):
    """(runner, targets) of a test command: what ran the tests and which files or names they were pointed at.
    No targets means the whole suite."""
    part = _test_part(cmd)
    if not part:
        return None
    toks = [_unquote(t) for t in _tokens(part)]
    while toks and (re.match(r'^[A-Za-z_]\w*=', toks[0]) or toks[0] in _WRAPPERS or toks[0] == 'cd'):
        toks = toks[2:] if toks[0] == 'cd' else toks[1:]
    if not toks:
        return None
    runner, rest = os.path.basename(toks[0]), toks[1:]
    if re.match(r'^python\d?(?:\.\d+)?$', runner) and len(rest) >= 2 and rest[0] == '-m':
        runner, rest = rest[1], rest[2:]
    elif runner in ('npm', 'pnpm', 'yarn', 'go', 'cargo', 'swift', 'dotnet', 'make', 'deno', 'bun', 'mvn', 'gradle') and rest:
        runner, rest = runner + ' ' + rest[0], rest[1:]
    targets, i = [], 0
    while i < len(rest):
        t = rest[i]
        if t in _RUNNER_FLAG_ARGS:
            if t in ('-k', '-t', '--testNamePattern', '-run', '--grep', '-g') and i + 1 < len(rest):
                targets.append('name:' + rest[i + 1])
            i += 2
            continue
        if not t.startswith('-') and t not in ('run', 'discover', 'test', '--', '2>&1') and not re.match(r'^\d*>', t):
            targets.append(t)
        i += 1
    if re.match(r'^python\d?', runner):          # python tests/test_x.py: the script is the target
        runner = 'python'
    return runner, tuple(targets)


def _same_check(a, b):
    ka, kb = check_key(a), check_key(b)
    if not ka or not kb or ka[0] != kb[0]:
        return False
    ta, tb = set(ka[1]), set(kb[1])
    if not ta or not tb or ta & tb:
        return True
    files = lambda ts: {t.split('::')[0].lstrip('./') for t in ts if not t.startswith('name:')}
    return bool(files(ta) & files(tb))


def _run_dir(ev):
    """Where a command ran: the session's directory, moved by a leading `cd`."""
    base = ev.get('cwd')
    for part in _split_cmd(ev.get('cmd') or ''):
        toks = _tokens(part)
        if not toks or toks[0] != 'cd':
            break
        if len(toks) > 1:
            d = os.path.expanduser(_unquote(toks[1]))
            base = d if os.path.isabs(d) else (os.path.join(base, d) if base else None)
    return os.path.normpath(base) if base else None


def _changed(ev, le):
    """Code files edited since the last test that passed before the last edit (the change the claim is about)."""
    start = 0
    for k in range(le - 1, -1, -1):
        e = ev.events[k]
        if e['k'] == 'run' and e['kind'] == 'test' and e['state'] == 'pass':
            start = k + 1
            break
    out = []
    for e in ev.events[start:le + 1]:
        if e['k'] == 'edit' and e['code'] and e['ok'] is not False:
            out.extend(p for p in e['paths'] if is_code(p) and os.path.isabs(p) and p not in out)
    return out[-30:]


def _module(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem in ('__init__', 'index', 'mod', 'main', 'lib'):
        stem = os.path.basename(os.path.dirname(path)) or stem
    return stem


def _not_loaded(run, changed):
    """The tests were pointed at files that never mention any changed file. None if unknown."""
    key = check_key(run['cmd'])
    base = _run_dir(run)
    if not key or not base:
        return None
    files = [t.split('::')[0] for t in key[1] if not t.startswith('name:')]
    files = [f for f in files if '/' in f or re.search(r'\.\w+$', f)]
    if not files or not changed:
        return None                               # the whole suite, or by name: can't tell what it loads
    texts, dirs = [], []
    for f in files:
        p = os.path.normpath(f if os.path.isabs(f) else os.path.join(base, f))
        rel = os.path.normpath(f).lstrip('./' if not os.path.isabs(f) else '')
        if os.path.isdir(p):
            return None                           # a folder: the whole suite under it, can't tell what it loads
        elif os.path.isfile(p):
            t = _read_tail(p, 524288)
            if t is None:
                return None
            texts.append((p, rel, t))
        else:
            return None                           # moved or deleted since: can't tell
    for c in changed:
        # Compared by the path as the command wrote it, so a session from another machine still matches.
        if any(c == p or c.endswith(os.sep + rel) for p, rel, _ in texts):
            return None
        if any(c.startswith(p + os.sep) or (os.sep + rel + os.sep) in c for p, rel in dirs):
            return None
        # The module's name, or its package's (`from src import charge` may load src/billing.py). Only a real
        # package counts: a folder like scripts/ or utils/ is mentioned everywhere and says nothing.
        pkg = os.path.dirname(c)
        names = {_module(c)}
        if any(os.path.isfile(os.path.join(pkg, n)) for n in ('__init__.py', 'index.js', 'index.ts', 'index.mjs', 'mod.rs', 'lib.rs')):
            names.add(os.path.basename(pkg))
        if any(re.search(r'(?<![\w])' + re.escape(n) + r'(?![\w])', t) for n in names for _, _, t in texts):
            return None
    shown = ', '.join(rel for _, rel, _ in texts) or ', '.join(rel for _, rel in dirs)
    return ('not-loaded', 'the tests that passed (%s) never mention %s, so they may not load your change'
            % (_short(shown, 60), _short(', '.join(os.path.basename(c) for c in changed[-3:]), 60)))


def _repo_root(path, _cache={}):
    """The nearest folder above path with a .git in it, or None."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    if d not in _cache:
        found, cur = None, d
        for _ in range(40):
            if os.path.exists(os.path.join(cur, '.git')):
                found = cur
                break
            up = os.path.dirname(cur)
            if up == cur:
                break
            cur = up
        _cache[d] = found
    return _cache[d]


def _elsewhere(run, changed):
    """The check ran in another project than the edited files. None if it's the same one, or unknown."""
    d = _run_dir(run)
    if not d or not changed or not os.path.isdir(d) or not all(os.path.exists(c) for c in changed):
        return None                               # moved, deleted, or another machine's paths: can't tell
    if any(c == d or c.startswith(d + os.sep) for c in changed):
        return None
    root, roots = _repo_root(d), {_repo_root(c) for c in changed}
    if root and None not in roots:
        different = root not in roots
    else:
        try:
            common = os.path.commonpath([d] + changed)
        except ValueError:
            common = ''
        home = os.path.normpath(_home())
        different = common in ('', os.sep, home, os.path.dirname(home)) or common.count(os.sep) < 2
    if not different:
        return None
    return ('elsewhere', 'the check `%s` ran in %s, but the edits were in %s'
            % (_short(run['cmd'], 40), d, _short(os.path.dirname(changed[-1]), 60)))


def red_then_green(ev, le):
    """The same test was red before the fix and green after the edit at index le. Red means its last run
    before the edit failed (an old failure that later went green belongs to an earlier task), or a run
    after the edit showed it failing on the old code (`git stash && pytest …`)."""
    tests = [(k, e) for k, e in enumerate(ev.events) if e['k'] == 'run' and e['kind'] == 'test']
    greens = [e for k, e in tests if k > le and e['state'] == 'pass']
    if any(g.get('red_inside') for g in greens):
        return True
    on_old_code = [e for k, e in tests if k > le and e.get('expected') and e['state'] == 'fail']
    for g in greens:
        before = [e for k, e in tests if k < le and e['state'] in ('pass', 'fail') and _same_check(e['cmd'], g['cmd'])]
        if (before and before[-1]['state'] == 'fail') or any(_same_check(r['cmd'], g['cmd']) for r in on_old_code):
            return True
    return False


def notes(ev, claim):
    """Notes for a "done" claim the hook let through: [(kind, text)]. Empty when there is nothing to add."""
    le = ev.last_edit()
    # Only with the turn that did the work (the edit or the runs after it), not again on every later wrap-up.
    if le < 0 or not any(e['turn'] == ev.turn for e in ev.events[le:] if e['k'] in ('edit', 'run')):
        return []
    after = [e for e in ev.events[le + 1:] if e['k'] == 'run' and e['state'] == 'pass']
    tests = [e for e in after if e['kind'] == 'test']
    changed = _changed(ev, le)
    out = []
    if FIX.search(claim) and tests and not red_then_green(ev, le):
        out.append(('no-red', 'tests passed after the last edit, but no run of them failed before it, '
                              'so nothing here shows they catch the bug'))
    if tests:
        n = _not_loaded(tests[-1], changed)
        if n:
            out.append(n)
        for c in changed:
            name = re.escape(os.path.basename(c))
            if re.search(r'(?m)(?:^|[\s/])' + name + r'\s+\d+\s+\d+(?:\s+\d+\s+\d+)?\s+0%|' + name + r'\s*\|\s*0(?:\.0+)?\s*\|',
                         tests[-1].get('out') or ''):
                out.append(('coverage-0', 'the coverage report shows %s at 0%%' % os.path.basename(c)))
                break
    checks = [e for e in after if e['kind'] != 'other']
    if checks and all(_elsewhere(e, changed) for e in checks):
        out.append(_elsewhere(checks[-1], changed))
    built = any(e['kind'] == 'build' for e in after)
    if not checks and not built and any(COMPILED.search(c) for c in changed):
        stale = [e for e in after if ARTIFACT.search(e.get('cmd') or '')]
        if stale and len(stale) == len(after):
            out.append(('stale-build', '`%s` runs a build output, and nothing rebuilt it after the edit'
                        % _short(stale[-1]['cmd'], 50)))
    return out


def _norm(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _norm_path(value):
    text = _norm(value).replace('\\', '/')
    if text.startswith('./'):
        text = text[2:]
    return text.rstrip('/')


def _unquote(token):
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def _tokens(text):
    return re.findall(r"'[^']*'|\"[^\"]*\"|\S+", text)


def _split_cmd(command):
    """Split a shell command on pipes and separators, not on a | inside quotes."""
    saved = []

    def hide(match):
        saved.append(match.group(0))
        return '\ue000%d\ue001' % (len(saved) - 1)

    masked = re.sub(r"'[^']*'|\"[^\"]*\"", hide, command)
    parts = []
    for piece in re.split(r'&&|\|\||[;|\n]', masked):
        part = re.sub(r'\ue000(\d+)\ue001', lambda m: saved[int(m.group(1))], piece).strip()
        if part:
            parts.append(part)
    return parts


# Flags that take the next word, so `grep -A 3 name` is a search for name, not for 3.
_OPT_ARG = {
    'grep': {'-A', '-B', '-C', '-e', '-f', '-m', '--include', '--exclude', '--exclude-dir'},
    'rg': {'-A', '-B', '-C', '-e', '-g', '-t', '-T', '-m'},
    'ag': {'-A', '-B', '-C', '-g', '-G'},
    'ack': {'-A', '-B', '-C'},
    'fd': {'-e', '-E', '-t', '-g'},
    'head': {'-n', '-c'},
    'tail': {'-n', '-c'},
}
_PATTERN_FLAG = {'-e', '--regexp'}
_SKIP_VERB = {'cd', 'pwd', 'echo', 'pushd', 'popd'}
_READ_VERB = {'cat', 'head', 'tail', 'less', 'more'}
_SEARCH_VERB = {'grep', 'rg', 'ag', 'ack', 'fd', 'locate', 'find', 'ls', 'git grep'}
_FIND_NAME = {'-name', '-iname', '-path', '-ipath', '-regex', '-iregex', '-wholename'}


def _search_query(verb, tokens):
    """The word that was searched for: the pattern, not the folder or the flags."""
    takes = _OPT_ARG.get(verb, ())
    positionals, forced = [], None
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t == '--':
            positionals.extend(_unquote(x) for x in tokens[i + 1:])
            break
        if t.startswith('-') and t != '-':
            name, eq, attached = t.partition('=')
            arg = attached if eq else None
            if name in takes or name in _PATTERN_FLAG:
                if arg is None and i + 1 < n and not tokens[i + 1].startswith('-'):
                    arg = tokens[i + 1]
                    i += 1
                if name in _PATTERN_FLAG and arg is not None:
                    forced = _unquote(arg)
                i += 1
                continue
            i += 1
            continue
        positionals.append(_unquote(t))
        i += 1
    if forced is not None:
        return forced
    return positionals[0] if positionals else ''


def _find_query(tokens):
    names, i = [], 0
    while i < len(tokens):
        if tokens[i] in _FIND_NAME and i + 1 < len(tokens):
            names.append(_unquote(tokens[i + 1]))
            i += 2
            continue
        i += 1
    if names:
        return ' '.join(names)
    for t in tokens:
        if not t.startswith('-'):
            return _unquote(t)
    return ''


def _one_part(tokens):
    """(program, what it looked for) for one piece of a shell command, or None to ignore it."""
    verb = tokens[0]
    if verb == 'git':
        if 'grep' in tokens:
            return ('git grep', _norm(_search_query('grep', tokens[tokens.index('grep') + 1:])))
        args, i = [], 1
        while i < len(tokens):
            if tokens[i] in ('-C', '-c') and i + 1 < len(tokens):
                i += 2                               # git -C repo status is still git status
                continue
            if not tokens[i].startswith('-'):
                args.append(_unquote(tokens[i]))
            i += 1
        sub = args[0] if args else ''
        return ('git ' + sub, _norm(' '.join(args[1:])))
    if verb in ('grep', 'rg', 'ag', 'ack', 'fd', 'locate'):
        return (verb, _norm(_search_query(verb, tokens[1:])))
    if verb == 'find':
        return ('find', _norm(_find_query(tokens[1:])))
    if verb == 'ls':
        return ('ls', _norm_path(_search_query('ls', tokens[1:])))
    if verb in _READ_VERB:
        path = _norm_path(_search_query(verb, tokens[1:]))
        return ('Read', path) if path else None          # `head` at the end of a pipe isn't a lookup
    args = [_unquote(t) for t in tokens[1:] if not t.startswith('-')]
    return (verb, _norm(' '.join(args)))


def _shell_method(command):
    """The kind of shell lookup. Flags, folders and a trailing `| wc` don't make a new one."""
    found = []
    for part in _split_cmd(command):
        tokens = _tokens(part)
        if not tokens or tokens[0] in _SKIP_VERB:
            continue
        kind = _one_part(tokens)
        if kind:
            found.append(kind)
    if not found:
        return ('sh', _norm(command))
    searches = [k for k in found if k[0] in _SEARCH_VERB or k[0] == 'Read']
    use = searches or found
    return use[0] if len(use) == 1 else tuple(use)


def method_of(name, inp):
    """Which kind of lookup this was.

    Two lookups back a "not found" claim only when this differs. The same text
    searched again, in another folder or with different flags, is the same kind.
    A different tool, a different pattern or a different file is a different kind.
    Reading a file and catting that same file are the same kind.
    """
    inp = inp if isinstance(inp, dict) else {}
    if name == 'Grep':
        return ('Grep', _norm(inp.get('pattern')))
    if name == 'Glob':
        return ('Glob', _norm(inp.get('pattern')))
    if name == 'Read':
        return ('Read', _norm_path(inp.get('file_path')))
    if name == 'NotebookRead':
        return ('NotebookRead', _norm_path(inp.get('notebook_path') or inp.get('file_path')))
    if name == 'LS':
        return ('LS', _norm_path(inp.get('path')))
    if name == 'WebSearch':
        return ('WebSearch', _norm(inp.get('query')))
    if name == 'WebFetch':
        return ('WebFetch', _norm(inp.get('url')))
    if name in ('Bash', 'BashRead', 'PowerShell'):
        return _shell_method(str(inp.get('command') or ''))
    try:
        raw = json.dumps(inp, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = ''
    return (name, raw)


def judge_absent(turn, final_text=None):
    """The "not found" half of judge(), on its own, so replay can count both kinds.

    One search repeated is still one. The second lookup has to be a different kind.
    """
    actions, errors, said, raw = parse(turn)
    if final_text:
        said = strip_quoted(final_text)
    if not said.strip() or re.search(HEDGE, said, re.I):
        return None
    claim = _first(ABSENT, said)
    methods = [method_of(n, inp) for i, n, inp in actions if i not in errors and (looks(n) or n == 'BashRead')]
    if claim and len(set(methods)) < 2:
        if not methods:
            why = "you didn't search or read anything in this turn"
        elif len(methods) == 1:
            why = 'you looked only once in this turn'
        else:
            why = 'you only searched one way in this turn'
        return 'absent', claim, why
    return None


def _cancelled(patterns, sentence, start):
    if patterns is not DONE:
        return bool(CONDITION.search(re.split(r'[,，;；:：]', sentence[:start])[-1]))
    clause = CLAUSE_SEP.split(sentence[:start])[-1]
    return bool(NEGATION.search(clause) or OPENS_WITH_CONDITION.match(clause) or OPENS_WITH_CONDITION.match(sentence))


def _first(patterns, text):
    """First claim in the text, skipping negated and conditional ones (see NEGATION above)."""
    # Chinese sentences often run on with no space after 。, so split there without one.
    for sentence in re.split(r'(?<=[.!?])\s+|(?<=[。！？])\s*|\n', text):
        for p in patterns:
            for m in re.finditer(p, sentence, re.I):
                if _cancelled(patterns, sentence, m.start()):
                    continue
                if LIST_SEP.search(sentence[max(0, m.start() - 1):m.start()] + sentence[m.end():m.end() + 1]):
                    continue                            # "pass/fail/skip": a list of labels, not a claim
                return re.sub(r'^[\s*✅-]+|(?:\s+and|[\s.!:—-])+$', '', m.group(0))   # "- ✅ Done:" -> "Done"
    return None


def message(kind, claim, missing):
    if kind == 'done':
        if 'did not pass' in missing:
            how = 'Make it pass and show the result, say what still fails'
        elif 'after the last edit' in missing:
            how = 'Run the check that proves it (the tests, the script, a request to the live URL) and show the result'
        else:
            how = 'Run the check and show the result, or cite proof you already have (a commit hash, a line like "14 passed" or "HTTP 200")'
        return f'claim-check: you wrote "{claim}", but {missing}. {how}, or say plainly that it isn\'t verified yet.'
    if 'one way' in missing:
        how = "Search a different way (another tool, or a different name or file), or say it's a guess."
    else:
        how = "Search or read at least twice before saying something isn't there, or say it's a guess."
    return f'claim-check: you wrote "{claim}", but {missing}. {how}'


def log_block(payload, kind, claim, missing):
    """Append one row to the audit log. kind 'note' rows are notes shown to you, not blocks."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                'kind': kind, 'claim': claim, 'missing': missing,
                                'session': payload.get('session_id'), 'cwd': payload.get('cwd')},
                               ensure_ascii=False) + '\n')
    except OSError:
        pass


def note_message(claim, found):
    lines = [text for _, text in found]
    head = f'claim-check note on "{claim}": '
    return head + lines[0] + '.' if len(lines) == 1 else head + ' '.join(f'({k + 1}) {t}.' for k, t in enumerate(lines))


def read_stdin():
    # Claude Code sends UTF-8; a Windows console encoding would garble Chinese claims.
    buf = getattr(sys.stdin, 'buffer', None)
    return buf.read().decode('utf-8', 'replace') if buf else sys.stdin.read()


def _home():
    """The account's home directory. $HOME is not used: a project env block can set it."""
    if os.name == 'nt':
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_PROFILE. Ignores USERPROFILE, which a project env block can set.
            if ctypes.windll.shell32.SHGetFolderPathW(None, 40, None, 0, buf) == 0 and buf.value:
                return buf.value
        except (AttributeError, OSError, ValueError):
            pass
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (AttributeError, ImportError, KeyError, OSError):
        return os.path.expanduser('~')


def _inside(path, parent):
    if not path or not parent:
        return False
    try:
        path, parent = os.path.realpath(path), os.path.realpath(parent)
    except (OSError, ValueError):
        return False
    return path == parent or path.startswith(parent + os.sep)


def config_home(project_dir=None):
    """Where Claude Code keeps user settings. Never a directory inside the project."""
    # Project and local settings cannot set CLAUDE_CONFIG_DIR. A value that still
    # points inside the project is ignored, so a repo cannot become the config home.
    d = os.environ.get('CLAUDE_CONFIG_DIR')
    if d and os.path.isabs(d) and not _inside(d, project_dir):
        return d
    return os.path.join(_home(), '.claude')


def managed_settings_dir():
    """File-based managed settings. Not the project, and not chosen by an env var."""
    if os.name == 'nt':
        return 'C:\\Program Files\\ClaudeCode'
    if sys.platform == 'darwin':
        return '/Library/Application Support/ClaudeCode'
    return '/etc/claude-code'


def _slug(plugin_id):
    # Same folding Claude Code uses for ~/.claude/plugins/data/<id>/.
    return re.sub(r'[^A-Za-z0-9_-]', '-', plugin_id)


def _claim_check_id(key):
    return isinstance(key, str) and (key == 'claim-check' or key.startswith('claim-check@'))


def _read_settings(path, project_dir):
    """Load one settings file. Project .claude/settings*.json is never a plugin setting."""
    if project_dir:
        try:
            real = os.path.realpath(path)
            local = os.path.realpath(os.path.join(project_dir, '.claude'))
            if real in (os.path.join(local, 'settings.json'), os.path.join(local, 'settings.local.json')):
                return {}
        except (OSError, ValueError):
            return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _options_map(data):
    configs = data.get('pluginConfigs') if isinstance(data, dict) else None
    if not isinstance(configs, dict):
        return {}
    out = {}
    for key, entry in configs.items():
        if not _claim_check_id(key) or not isinstance(entry, dict):
            continue
        options = entry.get('options')
        if isinstance(options, dict):
            out[key] = dict(options)
    return out


def _merge_options(base, extra):
    merged = {k: dict(v) for k, v in base.items()}
    for key, options in extra.items():
        merged.setdefault(key, {}).update(options)
    return merged


def _managed_maps(project_dir):
    root = managed_settings_dir()
    if not root or _inside(root, project_dir):
        return {}
    merged = _options_map(_read_settings(os.path.join(root, 'managed-settings.json'), project_dir))
    dropin = os.path.join(root, 'managed-settings.d')
    if not os.path.isdir(dropin) or _inside(dropin, project_dir):
        return merged
    for name in sorted(os.listdir(dropin)):
        if name.startswith('.') or not name.endswith('.json'):
            continue
        merged = _merge_options(merged, _options_map(_read_settings(os.path.join(dropin, name), project_dir)))
    return merged


def _saved_options(project_dir):
    """Plugin options from user settings, then managed settings. Managed keys win."""
    home = config_home(project_dir)
    user = _options_map(_read_settings(os.path.join(home, 'settings.json'), project_dir))
    return _merge_options(user, _managed_maps(project_dir)), home


def _id_from_data(home, known):
    """Plugin id from CLAUDE_PLUGIN_DATA, or '' if the variable is set but not ours."""
    raw = os.environ.get('CLAUDE_PLUGIN_DATA')
    if not raw:
        return None
    root = os.path.join(home, 'plugins', 'data')
    try:
        data, root = os.path.realpath(raw), os.path.realpath(root)
    except (OSError, ValueError):
        return ''
    if os.path.dirname(data) != root:
        return ''                                # not the user's plugin data dir
    matches = [k for k in known if _slug(k) == os.path.basename(data)]
    return matches[0] if len(matches) == 1 else ''


def _id_from_cache(home, known):
    """Plugin id from a cache-shaped CLAUDE_PLUGIN_ROOT, '' if unusable, None if not a cache path."""
    raw = os.environ.get('CLAUDE_PLUGIN_ROOT')
    if not raw:
        return None
    cache = os.path.join(home, 'plugins', 'cache')
    try:
        root, cache = os.path.realpath(raw), os.path.realpath(cache)
    except (OSError, ValueError):
        return ''
    if root != cache and not root.startswith(cache + os.sep):
        return None                               # in-place install; the data dir names it
    parts = os.path.relpath(root, cache).split(os.sep)
    if len(parts) != 3 or parts[1] != 'claim-check' or parts[0] in ('', '.', '..'):
        return ''
    plugin_id = 'claim-check@' + parts[0]
    return plugin_id if plugin_id in known else ''


def _select_plugin_id(known, home):
    """Which install's options apply. '' means: don't guess, leave the hook on."""
    data_id, root_id = _id_from_data(home, known), _id_from_cache(home, known)
    if data_id is not None and root_id is not None and data_id != root_id:
        return ''
    if data_id is not None:
        return data_id
    if root_id is not None:
        return root_id
    if len(known) == 1:
        return next(iter(known))                 # one install, and this process wasn't told which
    return ''


def plugin_options(project_dir=None):
    """This install's plugin settings. Missing means the hook runs and blocks."""
    try:
        known, home = _saved_options(project_dir)
        plugin_id = _select_plugin_id(known, home)
        return known.get(plugin_id, {}) if plugin_id else {}
    except Exception:
        return {}


def hook():
    try:
        payload = json.loads(read_stdin() or '{}')
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get('stop_hook_active'):
        return 0                                    # already blocked once; let it stop
    # enabled / log_only come from the plugin's userConfig, in user or managed
    # settings. Env vars and project files are not consulted: a repo can set those.
    opts = plugin_options(payload.get('cwd'))
    if opts.get('enabled') is False:
        return 0
    final = payload.get('last_assistant_message')
    if isinstance(final, str) and final.strip() and not _makes_claim(final):
        return 0                                    # nothing claimed: no need to read the transcript
    try:
        ev, last = session(payload.get('transcript_path'), final)
        if last is None:
            return 0
        v = verdict(ev, last, final)
        claim = None if v else done_claim(last, final)
        found = notes(ev, claim) if claim else []
    except Exception:                               # never block because of our own bug
        return 0
    if v:
        log_block(payload, *v)
        if opts.get('log_only') is True:
            return 0                                # trial run: log what it would have blocked, let the turn end
        # additionalContext keeps Claude going, like decision "block", with the same loop protection
        # (stop_hook_active), but shows as "Stop hook feedback" instead of a hook error.
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'Stop',            # ASCII-safe for Windows consoles
                                                 'additionalContext': message(*v)}}))
    elif found and opts.get('notes') is not False:
        log_block(payload, 'note', claim, '; '.join(k for k, _ in found))
        # systemMessage is shown to you; the turn ends as usual and the agent is not sent back.
        print(json.dumps({'systemMessage': note_message(claim, found)}))
    return 0


def _makes_claim(text):
    said = strip_quoted(text)
    return bool(said.strip()) and not re.search(HEDGE, said, re.I) and bool(_first(DONE, said) or _first(ABSENT, said))


def done_claim(turn, final_text=None):
    """The "done" claim in the final reply, or None (no claim, or an honest "not verified")."""
    said = strip_quoted(final_text) if final_text else parse(turn)[2]
    if not said.strip() or re.search(HEDGE, said, re.I):
        return None
    return _first(DONE, said)


def _written(entries, final_text):
    """Has the transcript caught up with the reply Claude Code handed the hook?"""
    want = re.sub(r'\s+', ' ', final_text).strip()[-80:]
    seen = 0
    for e in reversed(entries):
        if is_human(e):
            return False
        if e.get('type') != 'assistant':
            continue
        got = re.sub(r'\s+', ' ', _text(e)).strip()
        if got and (want in got or got[-80:] in want):
            return True
        seen += 1
        if seen >= 5:
            return False
    return False


def session(path, final_text=None, wait=1.0):
    """The session's Evidence and its last turn.

    The transcript is written asynchronously. If the final reply isn't in it yet, the commands just
    before it may be missing too, so wait up to `wait` seconds for it to catch up.
    """
    deadline = time.monotonic() + wait
    entries, pos = [], 0
    while True:
        more, pos = read_from(path, pos)     # only what was appended since the last look
        entries += [e for e in more if not e.get('isSidechain')]    # a subagent's own steps, as in replay
        if not final_text or _written(entries, final_text) or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    ts = list(turns(entries))
    ev = Evidence()
    for t in ts:
        ev.feed_turn(t)
    return ev, (ts[-1] if ts else None)


def check(path):
    ev, last = session(path)
    v = verdict(ev, last) if last else None
    print(message(*v) if v else 'claim-check: pass')
    claim = None if v or not last else done_claim(last)
    found = notes(ev, claim) if claim else []
    if found:
        print(note_message(claim, found))
    return 1 if v else 0


REASONS = (        # how replay groups the turns it would have blocked, by the start of the reason (first match wins)
    ('nothing ran successfully after the last edit, made in an earlier turn',
     'a wrap-up: code edited in an earlier turn, nothing run successfully since'),
    ('nothing ran successfully after the last edit', 'nothing ran successfully after the last code edit'),
    ('the last test or build after the edit did not pass', 'the last test or build after the code edit did not pass'),
    ('nothing ran in this turn and you cited no proof',
     'no tool calls in the turn, nothing run since the last code edit, no proof cited'),
    ('nothing you ran in this turn tested anything', 'only lookups, to-do updates or failed commands'),
)


def _reason(missing):
    return next((label for start, label in REASONS if missing.startswith(start)), missing)


def _n(n, one, many=None):
    return f'{n:,} {one if n == 1 else (many or one + "s")}'


def replay(root):
    if not os.path.isdir(root):
        print(f'No such directory: {root}')
        return 1
    files = sorted(glob.glob(os.path.join(root, '**', '*.jsonl'), recursive=True))
    # The Stop hook runs for the main agent only. Subagent transcripts are judged by nobody, so skip them.
    main = [f for f in files if (os.sep + 'subagents' + os.sep) not in f]
    n_turns = dups = 0
    done_seen = set()
    seen = {'done': [0, 0], 'absent': [0, 0]}      # turns with the claim, turns that would be blocked
    why, noted, kinds, red_green = {}, 0, {}, [0, 0]
    for f in main:
        try:
            entries = [e for e in load(f) if not e.get('isSidechain')]
        except OSError:
            continue
        ev = Evidence()
        for t in turns(entries):
            ev.feed_turn(t)
            said = parse(t)[2]
            # A resumed or forked session copies earlier turns into a new file. Skip exact copies only:
            # the same message id can also start a turn that went on differently, and that one counts.
            key = (t[0].get('uuid') or t[0].get('timestamp'), _text(t[0])[:200], said)
            if key in done_seen:
                dups += 1
                continue
            done_seen.add(key)
            n_turns += 1
            for kind, pats in (('done', DONE), ('absent', ABSENT)):
                if _first(pats, said):
                    seen[kind][0] += 1
            v = verdict(ev, t)
            if v and v[0] == 'done':
                seen['done'][1] += 1
                r = _reason(v[2])
                why[r] = why.get(r, 0) + 1
            claim = None if (v and v[0] == 'done') else done_claim(t)
            if claim:
                found = notes(ev, claim)
                noted += bool(found)
                for k, _ in found:
                    kinds[k] = kinds.get(k, 0) + 1
                if FIX.search(claim) and ev.last_edit() >= 0:
                    red_green[0] += 1
                    red_green[1] += red_then_green(ev, ev.last_edit())
            if judge_absent(t):                     # counted on its own, even when "done" was blocked too
                seen['absent'][1] += 1
    skipped = len(files) - len(main)
    print(f'{_n(len(main), "transcript")}, {_n(n_turns, "turn")}'
          + (f' ({_n(dups, "copy", "copies")} from resumed sessions skipped)' if dups else '')
          + (f'; {_n(skipped, "subagent transcript")} skipped, the Stop hook does not run there.' if skipped else '.'))
    for kind, label in (('done', '"done / fixed / deployed"'), ('absent', '"not found / doesn\'t exist"')):
        c, b = seen[kind]
        print(f'  {label}: {_n(c, "turn ends", "turns end")} with the claim; {b} {"has" if b == 1 else "have"} no proof by these rules ({b / c:.0%}).' if c
              else f'  {label}: no such claims.')
        if kind == 'done':
            for r, n in sorted(why.items(), key=lambda x: -x[1]):
                print(f'      {n:5}  {r}')
    if noted:
        print(f'  Notes (shown to you, never block) on {_n(noted, "claim")} it let through: '
              + ', '.join(f'{n} {k}' for k, n in sorted(kinds.items(), key=lambda x: -x[1])) + '.')
    if red_green[0]:
        print(f'  "fixed" after a code edit: {red_green[0]:,}; with a test that failed before the edit and passed after: {red_green[1]:,}.')
    return 0


def report(path=None):
    """Summarize a block log. The hook always writes LOG; path is only for this command."""
    path = path or LOG
    try:
        rows = []
        with open(path, encoding='utf-8') as f:
            for l in f:
                try:
                    rows.append(json.loads(l))
                except ValueError:
                    pass                            # a line cut off mid-write
    except OSError:
        print(f'No blocks logged yet ({path}).')
        return 0
    shown_notes = sum(1 for r in rows if r.get('kind') == 'note')
    rows = [r for r in rows if r.get('kind') != 'note']   # notes never blocked anything
    if shown_notes:
        print(f'{_n(shown_notes, "note")} shown (they never block).')
    if not rows:
        print(f'No blocks logged yet ({path}).')
        return 0
    label = {'done': '"done"', 'absent': '"not found"'}
    by = {}
    for r in rows:
        k = label.get(r.get('kind'), r.get('kind'))
        by[k] = by.get(k, 0) + 1
    print(f'{_n(len(rows), "block")} since {rows[0].get("at")}: ' + ', '.join(f'{v} {k}' for k, v in sorted(by.items())))
    shown = min(10, len(rows))
    print('Last block (check it for false positives):' if shown == 1
          else f'Last {shown} blocks (check them for false positives):')
    for r in rows[-10:]:
        print(f'  {r.get("at")}  {label.get(r.get("kind"), r.get("kind")):11}  "{r.get("claim")}"  ({r.get("missing")})')
    return 0


def _report_path(args):
    """The file `report` should read. (None, False) means the fixed hook log."""
    if not args:
        return None, False
    if len(args) == 2 and args[0] == '--log' and args[1]:
        return args[1], False
    if len(args) == 1 and args[0].startswith('--log=') and args[0][6:]:
        return args[0][6:], False
    return None, True


def main(argv):
    if len(argv) > 1 and argv[1] == 'check' and len(argv) > 2:
        return check(argv[2])
    if len(argv) > 1 and argv[1] == 'replay':
        return replay(argv[2] if len(argv) > 2 else os.path.expanduser('~/.claude/projects'))
    if len(argv) > 1 and argv[1] == 'report':
        path, bad = _report_path(argv[2:])
        if bad:
            print(__doc__)
            return 2
        return report(path)
    if len(argv) > 1:
        print(__doc__)
        return 2
    return hook()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
