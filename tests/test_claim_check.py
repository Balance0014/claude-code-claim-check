"""What each kind of claim must catch and what it must let through. No network, no real transcripts."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import claim_check as C  # noqa: E402

N = iter(range(10**6))


def you(text):
    return {'type': 'user', 'message': {'role': 'user', 'content': text}}


def tool(name, error=False, **inp):
    i = f'toolu_{next(N)}'
    return [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': i, 'name': name, 'input': inp}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': i, 'is_error': error,
                                                      'content': 'boom' if error else 'ok'}]}}]


def say(text):
    return {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}


def turn(*parts):
    out = [you('fix the login bug')]
    for p in parts:
        out.extend(p if isinstance(p, list) else [p])
    return out


class Done(unittest.TestCase):
    def test_edit_then_claim_is_blocked(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), say('Fixed. The login bug is fixed.')))
        self.assertEqual(v[0], 'done')
        self.assertIn('nothing ran successfully after the last edit', v[2])

    def test_edit_then_test_then_claim_passes(self):
        self.assertIsNone(C.judge(turn(tool('Edit', file_path='app.py'), tool('Bash', command='pytest'),
                                       say('The login bug is fixed; 14 tests pass.'))))

    def test_test_then_edit_again_is_blocked(self):
        v = C.judge(turn(tool('Bash', command='pytest'), tool('Edit', file_path='app.py'), say('All fixed.')))
        self.assertEqual(v[0], 'done')

    def test_failed_command_is_not_proof(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), tool('Bash', error=True, command='pytest'),
                         say('Tests pass now.')))
        self.assertEqual(v[0], 'done')

    def test_bookkeeping_is_not_proof(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), tool('TodoWrite', todos=[]), say('Fixed.')))
        self.assertEqual(v[0], 'done')

    def test_a_date_is_not_a_commit(self):
        self.assertIsNotNone(C.judge(turn(say('Everything is deployed, build 20261028.'))))
        self.assertIsNone(C.judge(turn(say('Everything is deployed, commit 3f9a2c1.'))))

    def test_a_browser_check_counts_as_proof(self):
        self.assertIsNone(C.judge(turn(tool('Edit', file_path='app.py'),
                                       tool('mcp__browser__screenshot'), say('The page is fixed.'))))

    def test_rereading_the_file_is_not_proof(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), tool('Read', file_path='app.py'), say('Deployed and working.')))
        self.assertIsNotNone(v)

    def test_chained_or_piped_look_only_commands_are_not_proof(self):
        for cmd in ('cd repo && git diff', 'cat app.log | grep error', 'grep -E "a|b" app.py'):
            self.assertIsNotNone(C.judge(turn(tool('Edit', file_path='app.py'), tool('Bash', command=cmd), say('Fixed.'))), cmd)
        self.assertIsNone(C.judge(turn(tool('Edit', file_path='app.py'), tool('Bash', command='cd repo && pytest'),
                                       say('Fixed.'))))

    def test_updating_notes_after_the_check_is_fine(self):
        self.assertIsNone(C.judge(turn(tool('Edit', file_path='app.py'), tool('Bash', command='pytest'),
                                       tool('Edit', file_path='NOTES.md'), say('Fixed and verified.'))))

    def test_report_from_memory_needs_cited_proof(self):
        v = C.judge(turn(say('Everything is deployed.')))
        self.assertIn('nothing ran in this turn', v[2])
        self.assertIsNone(C.judge(turn(say('Everything is deployed (commit 3f9a2c1, CI green).'))))

    def test_each_kind_of_cited_proof_passes(self):
        for proof in ('14 passed', '14 tests passed', '12 tests, all pass', 'HTTP 200', 'HTTP/1.1 204',
                      '200 OK', 'exit code 0', 'CI is green', 'CI passed',
                      'https://github.com/o/r/actions/runs/123', 'https://github.com/o/r/pull/7',
                      '12 条测试全通过', '全绿', '退出码 0'):
            self.assertIsNone(C.judge(turn(say(f'It is deployed: {proof}.'))), proof)

    def test_proof_in_backticks_or_next_to_a_question_counts(self):
        for text in ('Everything is deployed (commit `3f9a2c1`).', 'It is deployed: `HTTP 200`.',
                     'Everything is deployed, commit 3f9a2c1, want me to tag it?'):
            self.assertIsNone(C.judge(turn(say(text))), text)

    def test_things_that_look_like_proof_but_are_not(self):
        for text in ('It is deployed: 14 tests.', 'It is deployed and returns 200.', 'It is deployed on 2026-11-04.',
                     'It is deployed: HTTP 500.', 'Tests pass: 0 passed, 12 failed.', 'It is fixed: 2 passed, 3 failed.',
                     'It is fixed: 3 tests fail, 2 pass.'):
            self.assertEqual(C.judge(turn(say(text)))[0], 'done', text)

    def test_more_english_claims(self):
        for text in ('It should work now.', 'All set.', 'Tests are green.', 'I verified the fix.', 'The page is live.'):
            self.assertEqual(C.judge(turn(tool('Edit', file_path='a.py'), say(text)))[0], 'done', text)

    def test_saying_not_verified_always_passes(self):
        self.assertIsNone(C.judge(turn(tool('Edit', file_path='app.py'),
                                       say('I fixed the handler but have not verified it yet.'))))

    def test_claim_then_a_question_is_still_a_claim(self):
        self.assertEqual(C.judge(turn(tool('Edit', file_path='a.py'), say('The login bug is fixed. Want me to update the docs too?')))[0], 'done')
        self.assertEqual(C.judge(turn(tool('Edit', file_path='a.py'), say('已修复,要不要我再更新文档?')))[0], 'done')

    def test_negative_and_conditional_sentences_are_not_claims(self):
        for text in ("This hasn't been deployed yet.", 'Nothing is deployed to prod right now.',
                     'It was working before the upgrade.', 'Bump the version, and once the tests pass, tag the release.',
                     '如果测试通过,就可以合并。'):
            self.assertIsNone(C.judge(turn(say(text))), text)

    def test_more_chinese_claims(self):
        for text in ('已经修复了。', '测试都通过了。', '部署好了。', '都做完了。'):
            self.assertIsNotNone(C._first(C.DONE, text), text)

    def test_docs_only_change_needs_no_run(self):
        self.assertIsNone(C.judge(turn(tool('Edit', file_path='README.md'), say('Done.'))))

    def test_requirements_file_is_code(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), tool('Bash', command='pytest'),
                         tool('Edit', file_path='requirements.txt'), say('Fixed.')))
        self.assertEqual(v[0], 'done')

    def test_cat_after_edit_is_not_proof(self):
        v = C.judge(turn(tool('Edit', file_path='a.py'), tool('Bash', command='cat a.py'), say('Fixed.')))
        self.assertEqual(v[0], 'done')

    def test_cited_proof_after_a_read_passes(self):
        self.assertIsNone(C.judge(turn(tool('Read', file_path='a.py'), say('It is deployed, commit 3f9a2c1.'))))

    def test_unrelated_negatives_are_not_a_hedge(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), say("Fixed. I didn't run into any other issues.")))
        self.assertEqual(v[0], 'done')

    def test_quoted_or_code_claims_do_not_count(self):
        self.assertIsNone(C.judge(turn(tool('Edit', file_path='app.py'),
                                       say('I changed the message to `Done.` and the banner to "All set!"'))))

    def test_only_the_final_message_counts(self):
        # "fixed" in narration before the last tool call is not the answer
        t = turn(say('Looks like it is fixed, let me check.'), tool('Bash', command='pytest'), say('Here is the output.'))
        self.assertIsNone(C.judge(t))

    def test_chinese_claims(self):
        self.assertEqual(C.judge(turn(tool('Edit', file_path='a.py'), say('已修复,已上线。')))[0], 'done')
        self.assertIsNone(C.judge(turn(tool('Edit', file_path='a.py'), say('改好了,还没验证。'))))

    def test_words_that_only_look_like_claims(self):
        for text in ('Use the fixed header.', '客厅装修好了', '按已验证的格式写', 'Is it fixed?'):
            self.assertIsNone(C._first(C.DONE, text), text)


class Absent(unittest.TestCase):
    def test_not_found_without_looking_is_blocked(self):
        v = C.judge(turn(say("That function doesn't exist anywhere in the repo.")))
        self.assertEqual(v[0], 'absent')

    def test_editing_files_is_not_looking(self):
        v = C.judge(turn(tool('Edit', file_path='a.py'), tool('Edit', file_path='b.py'),
                         say("That helper doesn't exist anymore.")))
        self.assertEqual(v[0], 'absent')

    def test_not_found_after_two_lookups_passes(self):
        self.assertIsNone(C.judge(turn(tool('Grep', pattern='parse_token'), tool('Glob', pattern='**/*.py'),
                                       say('parse_token is not used anywhere; nothing calls it.'))))

    def test_chinese_first_person_findings(self):
        self.assertEqual(C.judge(turn(say('全仓都没找到这个函数。')))[0], 'absent')
        self.assertIsNone(C._first(C.ABSENT, '如果找不到那个文件夹,看下面的路径'))

    def test_a_condition_cancels_a_not_found_claim(self):
        for text in ("If the config file doesn't exist, the script creates it.", '如果没找到配置文件，脚本会自动创建。',
                     'Make sure nothing calls it before you delete it.'):
            self.assertIsNone(C.judge(turn(say(text))), text)

    def test_questions_are_not_claims(self):
        self.assertIsNone(C.judge(turn(say('Does the old endpoint still exist? 到底存不存在?'))))

    def test_each_kind_of_absent_claim_is_caught(self):
        for text in ('That file no longer exists.', 'The flag is no longer used.', "The helper isn't used anywhere.",
                     'parse_token is not referenced anywhere.', 'Nothing imports that module.',
                     "There are no callers left.", '那个任务没在跑。', '这个函数没人调用。'):
            self.assertEqual(C.judge(turn(say(text)))[0], 'absent', text)


class Turns(unittest.TestCase):
    def test_tool_results_and_injected_notes_do_not_start_a_turn(self):
        entries = (turn(tool('Edit', file_path='a.py'), tool('Bash', command='make test'))
                   + [{'type': 'user', 'isMeta': True, 'message': {'content': 'Stop hook feedback: ...'}},
                      say('Fixed, tests pass.')])
        ts = list(C.turns(entries))
        self.assertEqual(len(ts), 1)
        self.assertIsNone(C.judge(ts[0]))


    def test_notifications_and_summaries_do_not_start_a_turn(self):
        entries = (turn(tool('Edit', file_path='a.py'), tool('Bash', command='make test'))
                   + [you('<task-notification>build finished</task-notification>'),
                      {'type': 'user', 'isCompactSummary': True, 'message': {'content': 'Summary of earlier work'}},
                      say('Fixed, tests pass.')])
        self.assertEqual(len(list(C.turns(entries))), 1)

    def test_odd_lines_do_not_crash(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 't.jsonl')
        with open(p, 'w') as f:
            f.write('123\n"text"\n[]\n' + json.dumps(you('hi')) + '\n')
        self.assertEqual(len(C.load(p)), 1)


class Hook(unittest.TestCase):
    def run_hook(self, entries, **extra):
        d = tempfile.mkdtemp()
        path = os.path.join(d, 't.jsonl')
        with open(path, 'w') as f:
            f.write('\n'.join(json.dumps(e) for e in entries) + '\n')
        C.LOG = os.path.join(d, 'blocks.jsonl')
        sys.stdin = io.StringIO(json.dumps(dict({'transcript_path': path, 'session_id': 's1'}, **extra)))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = C.hook()
        return code, buf.getvalue(), C.LOG

    def test_blocks_once_and_logs(self):
        code, out, log = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertEqual(code, 0)
        self.assertIn('claim-check:', json.loads(out)['hookSpecificOutput']['additionalContext'])
        with open(log) as f:
            self.assertEqual(json.loads(f.readline())['kind'], 'done')

    def test_uses_the_final_message_claude_code_passes_in(self):
        # the transcript may not have the last message yet; the payload does
        _, out, _ = self.run_hook(turn(tool('Edit', file_path='a.py')), last_assistant_message='Fixed.')
        self.assertIn('claim-check:', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_second_stop_is_let_through(self):
        _, out, _ = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')), stop_hook_active=True)
        self.assertEqual(out, '')

    def test_broken_input_never_blocks(self):
        sys.stdin = io.StringIO('not json')
        self.assertEqual(C.hook(), 0)
        _, out, _ = self.run_hook([], transcript_path='/nope.jsonl')
        self.assertEqual(out, '')
        sys.stdin = io.StringIO('[1, 2]')
        self.assertEqual(C.hook(), 0)

    def test_report_skips_a_line_cut_off_mid_write(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, 'blocks.jsonl')
            with open(log, 'w') as f:
                f.write(json.dumps({'at': 't1', 'kind': 'absent', 'claim': 'not found', 'missing': 'x'}) + '\n{"at": "t2", "ki')
            old, C.LOG = C.LOG, log
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    C.report()
            finally:
                C.LOG = old
        self.assertIn('1 block since t1: 1 not found', buf.getvalue())
        self.assertIn('Last block (check it for false positives):', buf.getvalue())


class FalsePositivesFromReplay(unittest.TestCase):
    """Cases found by hand-checking a sample of replay blocks (v0.2)."""

    def test_claim_word_in_a_table_is_not_a_claim(self):
        self.assertIsNone(C.judge(turn(say('| Step | Status |\n|---|---|\n| Login | verified |\n| Deploy | 已验证 |'))))

    def test_claim_in_prose_next_to_a_table_still_counts(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), say('| a | b |\n|---|---|\n\nThe login bug is fixed.')))
        self.assertEqual(v[0], 'done')

    def test_word_in_a_slash_list_is_a_label(self):
        self.assertIsNone(C.judge(turn(say('Each case is marked pass/fixed/skipped in the sheet.'))))
        self.assertIsNone(C.judge(turn(say('状态分三种:待处理/已解决/已关闭。'))))

    def test_claim_in_a_sentence_with_a_path_still_counts(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), say('The bug in src/auth/login.py is fixed.')))
        self.assertEqual(v[0], 'done')
        v = C.judge(turn(tool('Edit', file_path='app.py'), say('已修复 src/auth/login.py 的问题。')))
        self.assertEqual(v[0], 'done')

    def test_chinese_not_checked_yet_is_honest(self):
        self.assertIsNone(C.judge(turn(say('同事说那个毛病修好了,我还没核。'))))
        self.assertIsNone(C.judge(turn(say('已修复,但还未核。'))))
        self.assertIsNone(C.judge(turn(say('已上线,数字待核。'))))

    def test_block_message_offers_citing_proof_only_without_edits(self):
        self.assertIn('cite proof', C.message('done', 'Fixed', 'nothing ran in this turn and you cited no proof'))
        self.assertNotIn('cite proof', C.message('done', 'Fixed', 'nothing ran successfully after the last edit'))

    def test_block_message_quotes_the_claim_without_bullets(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), say('- ✅ Done: login works again')))
        self.assertEqual(v[1], 'Done')
        self.assertEqual(C.judge(turn(tool('Edit', file_path='app.py'), say('Deployed.')))[1], 'Deployed')

    def test_plain_chinese_claim_still_blocked(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), say('已经修好了。')))
        self.assertEqual(v[0], 'done')


class ReplayCounts(unittest.TestCase):
    def test_copied_turns_count_once_and_reasons_are_split(self):
        t1 = [dict(you('fix it'), uuid='u1')] + tool('Edit', file_path='a.py') + [say('Fixed.')]
        t2 = [dict(you('status?'), uuid='u2'), say('Everything is deployed.')]
        with tempfile.TemporaryDirectory() as d:
            for name, entries in (('a.jsonl', t1 + t2), ('b.jsonl', t1)):   # b.jsonl: a resumed session's copy
                with open(os.path.join(d, name), 'w') as f:
                    f.write('\n'.join(json.dumps(e) for e in entries))
            buf = io.StringIO()
            with redirect_stdout(buf):
                C.replay(d)
        out = buf.getvalue()
        self.assertIn('2 turns (1 copy from resumed sessions skipped)', out)
        self.assertIn('2 turns end with the claim; 2 have no proof', out)
        self.assertIn('1  nothing ran successfully after the last code edit', out)
        self.assertIn('1  no tool calls in the turn', out)


if __name__ == '__main__':
    unittest.main()
