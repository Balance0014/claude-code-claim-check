#!/usr/bin/env python3
"""claim-check: stop a coding agent from saying "done" (or "not found") without proof.

Claude Code runs this as a Stop hook when the agent tries to end its turn. It reads the
session transcript, looks at what the agent claimed in its final message, and checks what
the agent actually did since your last message:

  * "fixed / deployed / tests pass / done" needs something that ran after the last code
    edit and did not fail: a shell command, an MCP tool, a URL fetch or a subagent. Reading
    the file you just edited is not proof. A turn with no edits can cite proof instead.
  * "doesn't exist / not found / nothing calls it" needs at least two lookups.

Missing proof: it blocks once, says exactly what is missing, and the agent either runs
the check or says plainly that it has not verified. Saying "not verified" always passes.
It never blocks twice in a row and never blocks because of its own errors. Every block
is appended to a log so you can audit false positives.

Usage:
  claim_check.py                      Stop hook mode (reads the hook payload on stdin)
  claim_check.py check FILE.jsonl     Judge the last turn of a transcript, print the verdict
  claim_check.py replay [DIR]         Judge every turn in past transcripts (default
                                      ~/.claude/projects) and print how many claims came without proof
  claim_check.py report               Summarize the block log

Standard library only. Python 3.9+.
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

LOG = os.environ.get('CLAIM_CHECK_LOG') or os.path.expanduser('~/.claude/claim-check/blocks.jsonl')

EDIT_TOOLS = {'Edit', 'MultiEdit', 'Write', 'NotebookEdit'}
READ_TOOLS = {'Read', 'Grep', 'Glob', 'LS'}
# What can prove a claim: running something. Shell commands, MCP tools (browsers, APIs), fetching a URL, a subagent.
# Bookkeeping such as TodoWrite proves nothing.
RUN_TOOLS = {'Bash', 'BashOutput', 'PowerShell', 'WebFetch', 'Agent', 'Task'}
# What counts as looking something up: reading, searching, or running a command.
LOOKUP_TOOLS = READ_TOOLS | RUN_TOOLS | {'WebSearch', 'NotebookRead'}


def runs(name):
    return name in RUN_TOOLS or name.startswith('mcp__')


def looks(name):
    return name in LOOKUP_TOOLS or name.startswith('mcp__')
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


def load(path):
    out = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    return out


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
                    path = str((i.get('input') or {}).get('file_path') or '')
                    if name in EDIT_TOOLS and DOC_FILE.search(path):
                        name = 'EditDoc'
                    elif name == 'Bash' and read_only(str((i.get('input') or {}).get('command') or '')):
                        name = 'BashRead'
                    actions.append((i.get('id'), name))
                    final = []          # text before a tool call is narration, not the answer
                elif i.get('type') == 'text':
                    final.append(i.get('text', ''))
        elif e.get('type') == 'user' and isinstance(c, list):
            for i in c:
                if isinstance(i, dict) and i.get('type') == 'tool_result' and i.get('is_error'):
                    errors.add(i.get('tool_use_id'))
    raw = '\n'.join(final)
    return actions, errors, strip_quoted(raw), raw


def judge(turn, final_text=None):
    """Return None if the turn is fine, else (kind, claim, what's missing).

    final_text: the agent's last message, from the Stop hook's last_assistant_message field.
    The transcript is written asynchronously and can miss that message, so it wins when present.
    """
    actions, errors, said, raw = parse(turn)
    if final_text:
        said, raw = strip_quoted(final_text), final_text
    if not said.strip() or re.search(HEDGE, said, re.I):
        return None

    claim = _first(DONE, said)
    if claim:
        ran = [k for k, (i, n) in enumerate(actions) if i not in errors and runs(n)]
        code_edits = [k for k, (_, n) in enumerate(actions) if n in EDIT_TOOLS]
        if code_edits:
            if not [k for k in ran if k > code_edits[-1]]:
                return 'done', claim, 'nothing ran successfully after the last edit'
        # Proof is looked for in the raw text: a hash in backticks or before a question still counts.
        elif not ran and not has_proof(raw) and not any(n == 'EditDoc' for _, n in actions):
            return 'done', claim, ('nothing ran in this turn and you cited no proof' if not actions else
                                   'nothing in this turn tested anything (only lookups, to-do updates or failed commands), '
                                   'and you cited no proof')

    return judge_absent(turn, final_text)


def judge_absent(turn, final_text=None):
    """The "not found" half of judge(), on its own, so replay can count both kinds."""
    actions, errors, said, raw = parse(turn)
    if final_text:
        said = strip_quoted(final_text)
    if not said.strip() or re.search(HEDGE, said, re.I):
        return None
    claim = _first(ABSENT, said)
    lookups = sum(1 for i, n in actions if i not in errors and (looks(n) or n == 'BashRead'))
    if claim and lookups < 2:
        return 'absent', claim, ("you didn't search or read anything in this turn" if not lookups
                                 else 'you looked only once in this turn')
    return None


def _cancelled(patterns, sentence, start):
    if patterns is not DONE:
        return bool(CONDITION.search(re.split(r'[,，;；:：]', sentence[:start])[-1]))
    clause = CLAUSE_SEP.split(sentence[:start])[-1]
    return bool(NEGATION.search(clause) or OPENS_WITH_CONDITION.match(clause) or OPENS_WITH_CONDITION.match(sentence))


def _first(patterns, text):
    """First claim in the text, skipping negated and conditional ones (see NEGATION above)."""
    for sentence in re.split(r'(?<=[.!?。！？])\s+|\n', text):
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
        how = ('Run the check that proves it (the tests, the script, a request to the live URL) and show the result'
               if 'after the last edit' in missing else
               'Run the check and show the result, or cite proof you already have (a commit hash, a line like "14 passed" or "HTTP 200")')
        return f'claim-check: you wrote "{claim}", but {missing}. {how}, or say plainly that it isn\'t verified yet.'
    return (f'claim-check: you wrote "{claim}", but {missing}. '
            "Search or read at least twice before saying something isn't there, or say it's a guess.")


def log_block(payload, kind, claim, missing):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                'kind': kind, 'claim': claim, 'missing': missing,
                                'session': payload.get('session_id'), 'cwd': payload.get('cwd')},
                               ensure_ascii=False) + '\n')
    except OSError:
        pass


def read_stdin():
    # Claude Code sends UTF-8; a Windows console encoding would garble Chinese claims.
    buf = getattr(sys.stdin, 'buffer', None)
    return buf.read().decode('utf-8', 'replace') if buf else sys.stdin.read()


def hook():
    try:
        payload = json.loads(read_stdin() or '{}')
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get('stop_hook_active') or os.environ.get('CLAIM_CHECK_OFF'):
        return 0                                    # already blocked once; let it stop
    path = payload.get('transcript_path')
    try:
        last = list(turns(load(path)))[-1]
        verdict = judge(last, payload.get('last_assistant_message'))
    except Exception:                               # never block because of our own bug
        return 0
    if verdict:
        log_block(payload, *verdict)
        # additionalContext keeps Claude going, like decision "block", with the same loop protection
        # (stop_hook_active), but shows as "Stop hook feedback" instead of a hook error.
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'Stop',            # ASCII-safe for Windows consoles
                                                 'additionalContext': message(*verdict)}}))
    return 0


def check(path):
    t = list(turns(load(path)))
    verdict = judge(t[-1]) if t else None
    print(message(*verdict) if verdict else 'claim-check: pass')
    return 1 if verdict else 0


REASONS = {        # how replay groups the turns it would have blocked
    'nothing ran successfully after the last edit': 'nothing ran successfully after the last code edit',
    'nothing ran in this turn and you cited no proof':
        'no tool calls in the turn and no proof cited (often a summary; the proof may be in an earlier turn)',
    'nothing in this turn tested anything (only lookups, to-do updates or failed commands), and you cited no proof':
        'only lookups, to-do updates or failed commands',
}


def _n(n, one, many=None):
    return f'{n} {one if n == 1 else (many or one + "s")}'


def replay(root):
    if not os.path.isdir(root):
        print(f'No such directory: {root}')
        return 1
    files = glob.glob(os.path.join(root, '**', '*.jsonl'), recursive=True)
    n_turns = dups = 0
    done_seen = set()
    seen = {'done': [0, 0], 'absent': [0, 0]}      # turns with the claim, turns that would be blocked
    why = {}
    for f in files:
        try:
            ts = list(turns(load(f)))
        except OSError:
            continue
        for t in ts:
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
            v = judge(t)
            if v and v[0] == 'done':
                seen['done'][1] += 1
                r = REASONS.get(v[2], v[2])
                why[r] = why.get(r, 0) + 1
            if judge_absent(t):                     # counted on its own, even when "done" was blocked too
                seen['absent'][1] += 1
    print(f'{_n(len(files), "transcript")}, {_n(n_turns, "turn")}'
          + (f' ({_n(dups, "copy", "copies")} from resumed sessions skipped).' if dups else '.'))
    for kind, label in (('done', '"done / fixed / deployed"'), ('absent', '"not found / doesn\'t exist"')):
        c, b = seen[kind]
        print(f'  {label}: {_n(c, "turn ends", "turns end")} with the claim; {b} {"has" if b == 1 else "have"} no proof by these rules ({b / c:.0%}).' if c
              else f'  {label}: no such claims.')
        if kind == 'done':
            for r, n in sorted(why.items(), key=lambda x: -x[1]):
                print(f'      {n:5}  {r}')
    return 0


def report():
    try:
        rows = []
        with open(LOG, encoding='utf-8') as f:
            for l in f:
                try:
                    rows.append(json.loads(l))
                except ValueError:
                    pass                            # a line cut off mid-write
    except OSError:
        print(f'No blocks logged yet ({LOG}).')
        return 0
    if not rows:
        print(f'No blocks logged yet ({LOG}).')
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


def main(argv):
    if len(argv) > 1 and argv[1] == 'check' and len(argv) > 2:
        return check(argv[2])
    if len(argv) > 1 and argv[1] == 'replay':
        return replay(argv[2] if len(argv) > 2 else os.path.expanduser('~/.claude/projects'))
    if len(argv) > 1 and argv[1] == 'report':
        return report()
    if len(argv) > 1:
        print(__doc__)
        return 2
    return hook()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
