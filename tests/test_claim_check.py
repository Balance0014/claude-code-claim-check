"""What each kind of claim must catch and what it must let through. No network, no real transcripts."""
import io
import json
import os
import subprocess
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

    def test_chinese_sentences_split_without_a_space(self):
        # A 还没 or 如果 in the previous sentence doesn't cancel the claim in the next one.
        for text in ('注册页还没改。登录的 bug 已经修好了。', '如果你愿意，我明天再看看。登录的 bug 已经修好了。'):
            self.assertEqual(C._first(C.DONE, text), '已经修好', text)
        self.assertEqual(C._first(C.ABSENT, '如果需要我再查。没找到调用方。'), '没找到')

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

    def test_parse_keeps_what_each_lookup_was_given(self):
        actions = C.parse(turn(tool('Grep', pattern='parse_token', path='src'),
                               tool('Bash', command='grep -n parse_token src'),
                               tool('Edit', file_path='a.py')))[0]
        self.assertEqual([(n, inp) for _, n, inp in actions],
                         [('Grep', {'pattern': 'parse_token', 'path': 'src'}),
                          ('BashRead', {'command': 'grep -n parse_token src'}),
                          ('Edit', None)])

    def test_the_same_search_twice_is_not_enough(self):
        # Another folder, extra flags, or a trailing pipe is still one search.
        same = (
            (tool('Grep', pattern='parse_token'), tool('Grep', pattern='parse_token', path='src')),
            (tool('Grep', pattern='parse_token'), tool('Grep', pattern='parse_token', glob='*.py')),
            (tool('Grep', pattern='parse_token'), tool('Grep', pattern=' parse_token ')),
            (tool('Bash', command='grep -n parse_token -r src'), tool('Bash', command='grep parse_token lib')),
            (tool('Bash', command='cd repo && grep parse_token .'), tool('Bash', command='grep parse_token | wc -l')),
            (tool('Bash', command='grep -E "a|b" src'), tool('Bash', command="grep -E 'a|b' lib")),
            (tool('Bash', command="find src -name '*.py'"), tool('Bash', command="find lib -name '*.py'")),
            (tool('Bash', command='rg -n parse_token src'), tool('Bash', command='rg parse_token lib')),
            (tool('Glob', pattern='**/*.py', path='src'), tool('Glob', pattern='**/*.py', path='lib')),
            (tool('Read', file_path='src/app.py'), tool('Read', file_path='./src/app.py')),
            (tool('Read', file_path='src/app.py'), tool('Bash', command='cat src/app.py')),
            (tool('Read', file_path='src/app.py'), tool('Bash', command='head -n 40 src/app.py')),
            (tool('WebSearch', query='parse_token'), tool('WebSearch', query='parse_token')),
            (tool('Grep', pattern='解析'), tool('Grep', pattern='解析', path='src')),
            (tool('mcp__code__search', query='parse_token', path='src'),
             tool('mcp__code__search', path='src', query='parse_token')),
            (tool('Bash', command='git grep parse_token'), tool('Bash', command='git -C repo grep -n parse_token')),
            (tool('Bash', command='ls -la ./src'), tool('Bash', command='ls src')),
        )
        for first, second in same:
            v = C.judge(turn(first, second, say('parse_token is not used anywhere.')))
            self.assertEqual(v[0], 'absent', second)
            self.assertEqual(v[2], 'you only searched one way in this turn')
        v = C.judge(turn(tool('Grep', pattern='解析'), tool('Grep', pattern='解析', path='src'),
                         say('全仓都没找到这个函数。')))
        self.assertEqual(v[0], 'absent')

    def test_a_different_kind_of_lookup_is_enough(self):
        pairs = (
            (tool('Grep', pattern='parse_token'), tool('Grep', pattern='parseToken')),
            (tool('Grep', pattern='parse_token'), tool('Read', file_path='src/registry.py')),
            (tool('Grep', pattern='parse_token'), tool('Glob', pattern='**/*token*')),
            (tool('Grep', pattern='parse_token'), tool('Bash', command='grep -n parse_token -r .')),
            (tool('Bash', command='grep parse_token -r .'), tool('Bash', command='rg parse_token')),
            (tool('Bash', command='grep parse_token'), tool('Bash', command='grep parseToken')),
            (tool('Read', file_path='src/a.py'), tool('Read', file_path='src/b.py')),
            (tool('WebSearch', query='parse_token'), tool('WebSearch', query='parseToken users')),
            (tool('Grep', pattern='解析'), tool('Read', file_path='src/app.py')),
            (tool('mcp__code__search', query='parse_token'), tool('mcp__code__search', query='parseToken')),
            (tool('Bash', command='git grep parse_token'), tool('Bash', command='grep parse_token')),
        )
        for first, second in pairs:
            self.assertIsNone(C.judge(turn(first, second, say('Nothing calls it.'))), second)
        self.assertIsNone(C.judge(turn(tool('Grep', pattern='解析'), tool('Read', file_path='src/app.py'),
                                       say('全仓都没找到这个函数。'))))

    def test_one_lookup_is_still_once_and_a_failed_one_does_not_count(self):
        v = C.judge(turn(tool('Grep', pattern='parse_token'), say("That helper doesn't exist.")))
        self.assertEqual(v[2], 'you looked only once in this turn')
        self.assertIn('at least twice', C.message(*v))
        v = C.judge(turn(tool('Grep', pattern='parse_token'), tool('Grep', pattern='parse_token', path='src'),
                         say("That helper doesn't exist.")))
        self.assertIn('different way', C.message(*v))
        v = C.judge(turn(tool('Grep', pattern='parse_token'),
                         tool('Read', error=True, file_path='src/registry.py'),
                         say("There are no callers.")))
        self.assertEqual(v[2], 'you looked only once in this turn')

    def test_two_of_the_same_and_one_different_passes(self):
        self.assertIsNone(C.judge(turn(tool('Grep', pattern='parse_token'),
                                       tool('Grep', pattern='parse_token', path='src'),
                                       tool('Read', file_path='src/registry.py'),
                                       say('Nothing imports that module.'))))

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
    _ENV = ('CLAUDE_PLUGIN_DATA', 'CLAUDE_PLUGIN_ROOT', 'CLAUDE_CONFIG_DIR', 'CLAIM_CHECK_OFF',
            'CLAIM_CHECK_LOG_ONLY', 'CLAUDE_PLUGIN_OPTION_ENABLED', 'CLAUDE_PLUGIN_OPTION_LOG_ONLY', 'HOME')

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.managed = tempfile.mkdtemp()
        self._old_home, self._old_managed = C.config_home, C.managed_settings_dir
        self._saved_env = {k: os.environ.get(k) for k in self._ENV}
        C.config_home = lambda project_dir=None: self.home
        C.managed_settings_dir = lambda: self.managed
        for k in self._ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        C.config_home, C.managed_settings_dir = self._old_home, self._old_managed
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write_options(self, plugin_id, directory=None, filename='settings.json', **options):
        directory = self.home if directory is None else directory
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, filename)
        data = {}
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        data.setdefault('pluginConfigs', {})[plugin_id] = {'options': options}
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def run_hook(self, entries, **extra):
        d = tempfile.mkdtemp()
        path = os.path.join(d, 't.jsonl')
        with open(path, 'w') as f:
            f.write('\n'.join(json.dumps(e) for e in entries) + '\n')
        C.LOG = os.path.join(d, 'blocks.jsonl')
        sys.stdin = io.StringIO(json.dumps(dict({'transcript_path': path, 'session_id': 's1', 'cwd': d}, **extra)))
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

    def test_default_is_on(self):
        _, out, _ = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertIn('claim-check:', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_plugin_setting_off_does_not_block_or_log(self):
        self.write_options('claim-check@claim-check', enabled=False)
        code, out, log = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertEqual((code, out), (0, ''))
        self.assertFalse(os.path.exists(log))

    def test_log_only_plugin_setting_logs_without_blocking(self):
        self.write_options('claim-check@buildwithclaude', log_only=True)
        _, out, log = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertEqual(out, '')
        with open(log) as f:
            self.assertEqual(json.loads(f.readline())['kind'], 'done')

    def test_off_wins_over_log_only(self):
        self.write_options('claim-check@claim-check', enabled=False, log_only=True)
        _, out, log = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertEqual(out, '')
        self.assertFalse(os.path.exists(log))

    def test_a_string_false_does_not_turn_the_hook_off(self):
        self.write_options('claim-check@claim-check', enabled='false', log_only='true')
        _, out, log = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertIn('claim-check:', json.loads(out)['hookSpecificOutput']['additionalContext'])
        self.assertTrue(os.path.exists(log))

    def test_env_and_project_files_cannot_disable(self):
        project = tempfile.mkdtemp()
        claude_dir = os.path.join(project, '.claude')
        os.makedirs(claude_dir)
        for name in ('settings.json', 'settings.local.json'):
            with open(os.path.join(claude_dir, name), 'w', encoding='utf-8') as f:
                json.dump({'env': {'CLAIM_CHECK_OFF': '1', 'CLAIM_CHECK_LOG_ONLY': '1',
                                   'CLAUDE_PLUGIN_OPTION_ENABLED': 'false',
                                   'CLAUDE_PLUGIN_OPTION_LOG_ONLY': 'true'},
                           'pluginConfigs': {'claim-check@buildwithclaude':
                                             {'options': {'enabled': False, 'log_only': True}}}}, f)
        os.environ.update(CLAIM_CHECK_OFF='1', CLAIM_CHECK_LOG_ONLY='1',
                          CLAUDE_PLUGIN_OPTION_ENABLED='false', CLAUDE_PLUGIN_OPTION_LOG_ONLY='true',
                          CLAUDE_CONFIG_DIR=claude_dir, HOME=project)
        old = os.getcwd()
        os.chdir(project)
        try:
            _, out, _ = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')), cwd=project)
        finally:
            os.chdir(old)
        self.assertIn('claim-check:', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_another_marketplace_off_does_not_disable_this_install(self):
        self.write_options('claim-check@claim-check', enabled=False)
        os.environ['CLAUDE_PLUGIN_DATA'] = os.path.join(self.home, 'plugins', 'data', 'claim-check-buildwithclaude')
        _, out, _ = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertIn('claim-check:', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_this_installs_plugin_setting_off_is_honored(self):
        self.write_options('claim-check@buildwithclaude', enabled=False)
        os.environ['CLAUDE_PLUGIN_DATA'] = os.path.join(self.home, 'plugins', 'data', 'claim-check-buildwithclaude')
        os.environ['CLAUDE_PLUGIN_ROOT'] = os.path.join(
            self.home, 'plugins', 'cache', 'buildwithclaude', 'claim-check', '0.4.0')
        _, out, log = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertEqual(out, '')
        self.assertFalse(os.path.exists(log))

    def test_disagreeing_plugin_paths_leave_the_hook_on(self):
        self.write_options('claim-check@buildwithclaude', enabled=False)
        os.environ['CLAUDE_PLUGIN_DATA'] = os.path.join(self.home, 'plugins', 'data', 'claim-check-buildwithclaude')
        os.environ['CLAUDE_PLUGIN_ROOT'] = os.path.join(
            self.home, 'plugins', 'cache', 'claim-check', 'claim-check', '0.4.0')
        _, out, _ = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertIn('claim-check:', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_plugin_data_outside_the_user_dir_cannot_turn_the_hook_off(self):
        self.write_options('claim-check@claim-check', enabled=False)
        project = tempfile.mkdtemp()
        os.environ['CLAUDE_PLUGIN_DATA'] = os.path.join(project, 'data', 'claim-check-claim-check')
        _, out, _ = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')), cwd=project)
        self.assertIn('claim-check:', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_managed_setting_overrides_the_user(self):
        self.write_options('claim-check@claim-check', enabled=True)
        self.write_options('claim-check@claim-check', self.managed, 'managed-settings.json', enabled=False)
        _, out, log = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertEqual(out, '')
        self.assertFalse(os.path.exists(log))
        self.write_options('claim-check@claim-check', self.managed, 'managed-settings.json', enabled=True)
        _, out, _ = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertIn('claim-check:', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_managed_drop_in_overrides_the_managed_file(self):
        self.write_options('claim-check@claim-check', self.managed, 'managed-settings.json', enabled=True)
        drop = os.path.join(self.managed, 'managed-settings.d')
        self.write_options('claim-check@claim-check', drop, '20-claim-check.json', enabled=False)
        _, out, _ = self.run_hook(turn(tool('Edit', file_path='a.py'), say('Fixed.')))
        self.assertEqual(out, '')

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
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = C.main(['claim_check.py', 'report', '--log', log])
        self.assertEqual(code, 0)
        self.assertIn('1 block since t1: 1 "not found"', buf.getvalue())
        self.assertIn('Last block (check it for false positives):', buf.getvalue())

    def test_report_without_log_still_reads_the_hook_log(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, 'blocks.jsonl')
            with open(log, 'w') as f:
                f.write(json.dumps({'at': 't1', 'kind': 'done', 'claim': 'Fixed', 'missing': 'x'}) + '\n')
            old, C.LOG = C.LOG, log
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(C.main(['claim_check.py', 'report']), 0)
            finally:
                C.LOG = old
        self.assertIn('1 block since t1: 1 "done"', buf.getvalue())

    def test_report_log_requires_a_path(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(C.main(['claim_check.py', 'report', '--log']), 2)
        self.assertIn('--log', buf.getvalue())

    def test_hook_log_ignores_claim_check_log(self):
        home = tempfile.mkdtemp()
        other = tempfile.mkdtemp()
        config = tempfile.mkdtemp()
        env_log = os.path.join(other, 'elsewhere.jsonl')
        transcript = os.path.join(other, 't.jsonl')
        with open(transcript, 'w') as f:
            f.write('\n'.join(json.dumps(e) for e in turn(tool('Edit', file_path='a.py'), say('Fixed.'))) + '\n')
        env = os.environ.copy()
        env.update(HOME=home, USERPROFILE=home, CLAIM_CHECK_LOG=env_log, CLAUDE_CONFIG_DIR=config)
        script = os.path.join(os.path.dirname(__file__), '..', 'claim_check.py')
        proc = subprocess.run(
            [sys.executable, script],
            input=json.dumps({'transcript_path': transcript, 'session_id': 's', 'cwd': other}),
            text=True, capture_output=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('claim-check:', proc.stdout)
        fixed = os.path.join(home, '.claude', 'claim-check', 'blocks.jsonl')
        self.assertTrue(os.path.isfile(fixed))
        self.assertFalse(os.path.exists(env_log))
        with open(fixed, encoding='utf-8') as f:
            self.assertEqual(json.loads(f.readline())['kind'], 'done')


class ConfigHome(unittest.TestCase):
    """config_home() itself, with no test double: $HOME and an in-project config dir don't count."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ('HOME', 'CLAUDE_CONFIG_DIR')}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_home_and_an_in_project_config_dir_are_not_the_user_settings(self):
        project = tempfile.mkdtemp()
        os.environ['HOME'] = project
        os.environ['CLAUDE_CONFIG_DIR'] = os.path.join(project, '.claude')
        os.makedirs(os.environ['CLAUDE_CONFIG_DIR'])
        with open(os.path.join(project, '.claude', 'settings.json'), 'w', encoding='utf-8') as f:
            json.dump({'pluginConfigs': {'claim-check@claim-check':
                                         {'options': {'enabled': False, 'from_project_file': True}}}}, f)
        self.assertEqual(C.config_home(project), os.path.join(C._home(), '.claude'))
        self.assertNotIn('from_project_file', C.plugin_options(project))

    def test_a_config_dir_outside_the_project_is_the_plugin_setting(self):
        home, project = tempfile.mkdtemp(), tempfile.mkdtemp()
        os.environ['CLAUDE_CONFIG_DIR'] = home
        with open(os.path.join(home, 'settings.json'), 'w', encoding='utf-8') as f:
            json.dump({'pluginConfigs': {'claim-check@claim-check': {'options': {'enabled': False}}}}, f)
        self.assertIs(C.plugin_options(project).get('enabled'), False)


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


class ProofAndConditions(unittest.TestCase):
    def test_a_pass_count_next_to_failures_is_not_proof(self):
        for text in ('It is fixed: 3 failed, 2 passed.', 'It is fixed: 1 failed, 14 passed in 2.1s.',
                     'It is fixed: 10 tests, 0 passed.', '已经修复:5 个通过,3 个失败。', '已经修复:0 个测试通过。'):
            self.assertEqual(C.judge(turn(say(text)))[0], 'done', text)
        for text in ('It is fixed: 14 passed, 0 failed.', '已经修复:12 个通过,0 个失败。'):
            self.assertIsNone(C.judge(turn(say(text))), text)

    def test_status_written_other_ways(self):
        for proof in ('status 200', 'status code 200', '201 Created', '202 Accepted'):
            self.assertIsNone(C.judge(turn(say(f'It is deployed: {proof}.'))), proof)

    def test_a_uuid_piece_is_not_a_commit_and_negated_all_green_is_not_proof(self):
        for text in ('It is deployed, request req-3f9a2c1e-77.', '已经部署,但还不是全绿。'):
            self.assertEqual(C.judge(turn(say(text)))[0], 'done', text)

    def test_a_condition_inside_a_noun_phrase_does_not_cancel_the_claim(self):
        for text in ('The crash when saving is fixed.', 'The bug where login fails if the token expires is fixed.',
                     'The regression from before the upgrade is fixed.', 'No problem — it is fixed.',
                     'There were no errors and everything is deployed.', '测试没有报错,并且已经上线。', '测试没有报错并且已经上线。'):
            v = C.judge(turn(tool('Edit', file_path='app.py'), say(text)))
            self.assertEqual(v and v[0], 'done', text)

    def test_an_opening_condition_or_a_negation_in_the_clause_still_cancels(self):
        for text in ("Once it's merged, that's done.", "If the tests pass, it's fixed.", "When CI finishes it's deployed.",
                     "The bug that never showed up on staging is fixed.", '一旦合并,就算做完了。', '没有复现过的那个 bug 已经修好了。'):
            self.assertIsNone(C.judge(turn(tool('Edit', file_path='app.py'), say(text))), text)

    def test_command_substitution_and_lookalike_names_are_not_read_only(self):
        self.assertFalse(C.read_only('echo $(pytest)'))
        self.assertFalse(C.read_only('find-unused-deps'))
        self.assertTrue(C.read_only('find . -name "*.py" | wc -l'))

    def test_replay_counts_not_found_even_when_done_is_blocked_too(self):
        t = [dict(you('status?'), uuid='u1'), say("Everything is deployed. The old flag doesn't exist anymore.")]
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'a.jsonl'), 'w') as f:
                f.write('\n'.join(json.dumps(e) for e in t))
            buf = io.StringIO()
            with redirect_stdout(buf):
                C.replay(d)
        self.assertIn('"not found / doesn\'t exist": 1 turn ends with the claim; 1 has no proof', buf.getvalue())


class EarlierTurns(unittest.TestCase):
    """A wrap-up with no edits can rely on a check from an earlier turn if no code changed after it."""
    def verdict(self, *turns_):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 't.jsonl')
            with open(path, 'w') as f:
                f.write('\n'.join(json.dumps(e) for t in turns_ for e in t) + '\n')
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = C.check(path)
        return code, buf.getvalue()

    def wrap_up(self, text='All done, the login bug is fixed.'):
        return [you('thanks, anything left?'), say(text)]

    def test_a_check_in_an_earlier_turn_counts(self):
        tested = turn(tool('Edit', file_path='app.py'), tool('Bash', command='pytest'), say('14 tests pass.'))
        self.assertEqual(self.verdict(tested, self.wrap_up()), (0, 'claim-check: pass\n'))

    def test_a_check_two_turns_back_still_counts(self):
        tested = turn(tool('Edit', file_path='app.py'), tool('Bash', command='pytest'), say('14 tests pass.'))
        looked = [you('where is the config?')] + tool('Read', file_path='config.toml') + [say('It is in config.toml.')]
        self.assertEqual(self.verdict(tested, looked, self.wrap_up())[0], 0)

    def test_an_edit_after_the_check_still_blocks(self):
        tested = turn(tool('Bash', command='pytest'), say('14 tests pass.'))
        edited = [you('rename the helper')] + tool('Edit', file_path='app.py') + [say('Renamed it.')]
        code, out = self.verdict(tested, edited, self.wrap_up())
        self.assertEqual(code, 1)
        self.assertIn('nothing ran in this turn', out)

    def test_a_failed_or_read_only_run_earlier_does_not_count(self):
        failed = turn(tool('Edit', file_path='app.py'), tool('Bash', error=True, command='pytest'), say('Tests fail.'))
        self.assertEqual(self.verdict(failed, self.wrap_up())[0], 1)
        reread = turn(tool('Edit', file_path='app.py'), tool('Bash', command='git diff'), say('Looks right.'))
        self.assertEqual(self.verdict(reread, self.wrap_up())[0], 1)

    def test_a_turn_with_its_own_edit_needs_its_own_check(self):
        v = C.judge(turn(tool('Edit', file_path='app.py'), say('Fixed.')), earlier=True)
        self.assertEqual(v[0], 'done')

    def test_the_hook_reads_earlier_turns(self):
        tested = turn(tool('Edit', file_path='app.py'), tool('Bash', command='pytest'), say('14 tests pass.'))
        d = tempfile.mkdtemp()
        path = os.path.join(d, 't.jsonl')
        with open(path, 'w') as f:
            f.write('\n'.join(json.dumps(e) for e in tested + self.wrap_up()) + '\n')
        C.LOG = os.path.join(d, 'blocks.jsonl')
        home = tempfile.mkdtemp()
        old_home, old_managed = C.config_home, C.managed_settings_dir
        C.config_home = lambda project_dir=None: home
        C.managed_settings_dir = lambda: home
        sys.stdin = io.StringIO(json.dumps({'transcript_path': path, 'session_id': 's1'}))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                C.hook()
        finally:
            C.config_home, C.managed_settings_dir = old_home, old_managed
        self.assertEqual(buf.getvalue(), '')

    def test_replay_carries_the_check_across_turns(self):
        t1 = [dict(you('fix it'), uuid='u1')] + tool('Edit', file_path='a.py') + tool('Bash', command='pytest') + [say('Fixed.')]
        t2 = [dict(you('status?'), uuid='u2'), say('Everything is fixed.')]
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'a.jsonl'), 'w') as f:
                f.write('\n'.join(json.dumps(e) for e in t1 + t2))
            buf = io.StringIO()
            with redirect_stdout(buf):
                C.replay(d)
        self.assertIn('2 turns end with the claim; 0 have no proof', buf.getvalue())

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



# ---------------------------------------------------------------------------
# 0.6.0: the session's timeline, real outcomes, red-then-green, and notes.

def bash(command, out='ok', error=False, extra=None, uid=None, name='Bash', **inp):
    """A shell call and its result. extra is Claude Code's toolUseResult (bashEditDiff, backgroundTaskId, ...)."""
    i = uid or f'toolu_{next(N)}'
    result = {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': i, 'is_error': error,
                                                        'content': out}]}}
    if extra is not None:
        result['toolUseResult'] = extra
    return [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': i, 'name': name,
                                                           'input': dict(inp, command=command)}]}}, result]


def edit(path='/repo/app.py', error=False):
    return tool('Edit', error=error, file_path=path)


def notice(uid, status='completed', code=0, task='b1'):
    summary = (f'Background command "tests" completed (exit code {code})' if status == 'completed'
               else f'Background command "tests" failed with exit code {code}')
    return you(f'<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>{uid}</tool-use-id>\n'
               f'<status>{status}</status>\n<summary>{summary}</summary>\n</task-notification>')


def session(*turns_):
    """Verdict on the last turn, with the earlier turns as the session's history."""
    ev = C.Evidence()
    for t in turns_[:-1]:
        ev.feed_turn(t)
    return C.judge(turns_[-1], earlier=ev)


def later(text, *parts):
    out = [you('next')]
    for p in parts:
        out.extend(p if isinstance(p, list) else [p])
    return out + [say(text)]


PYTEST_FAIL = 'Exit code 1\n=== 1 failed, 4 passed in 0.31s ==='


class Outcomes(unittest.TestCase):
    """What a command's result says, not what the reply says about it."""

    def test_a_non_zero_exit_is_not_proof(self):
        v = C.judge(turn(edit(), bash('pytest', PYTEST_FAIL, error=True), say('Tests pass now.')))
        self.assertIn('nothing ran successfully after the last edit', v[2])
        self.assertIn('exited with code 1', v[2])

    def test_a_timed_out_command_is_not_proof(self):
        out = 'Command did not complete within its 120s timeout and was moved to the background with ID: b1.'
        v = C.judge(turn(edit(), bash('pytest', out, extra={'timedOutAfterMs': 120000, 'backgroundTaskId': 'b1'}),
                         say('Fixed.')))
        self.assertIn('timed out', v[2])
        v = C.judge(turn(edit(), bash('pytest', 'Command timed out after 2m 0s', error=True), say('Fixed.')))
        self.assertIn('timed out', v[2])

    def test_a_command_that_never_ran_is_not_proof(self):
        for out in ('Error: Permission for this action was denied by the Claude Code auto mode classifier.',
                    "The user doesn't want to proceed with this tool use.", 'Exit code 127\nbash: pytest: command not found'):
            v = C.judge(turn(edit(), bash('pytest', out, error=True), say('Fixed.')))
            self.assertIn('did not run', v[2], out)

    def test_a_background_run_counts_only_once_it_finished(self):
        start = bash('pytest', 'Command running in background with ID: b1', extra={'backgroundTaskId': 'b1'}, uid='t-bg')
        v = C.judge(turn(edit(), start, say('Fixed.')))
        self.assertIn('still running in the background', v[2])
        self.assertIsNone(C.judge(turn(edit(), start, notice('t-bg'), say('Fixed.'))))
        v = C.judge(turn(edit(), start, notice('t-bg', 'failed', 1), say('Fixed.')))
        self.assertIn('exited with code 1', v[2])

    def test_reading_or_stopping_a_background_run_settles_it(self):
        start = bash('pytest', 'Command running in background with ID: b2', extra={'backgroundTaskId': 'b2'})
        done = tool('TaskOutput', task_id='b2')
        done[1]['message']['content'][0]['content'] = '<status>completed</status>\n<exit_code>0</exit_code>\n<output>5 passed</output>'
        self.assertIsNone(C.judge(turn(edit(), start, done, say('Fixed.'))))
        failed = tool('TaskOutput', task_id='b2')
        failed[1]['message']['content'][0]['content'] = '<status>completed</status>\n<exit_code>2</exit_code>'
        self.assertIn('exited with code 2', C.judge(turn(edit(), start, failed, say('Fixed.')))[2])
        self.assertIn('stopped', C.judge(turn(edit(), start, tool('TaskStop', task_id='b2'), say('Fixed.')))[2])

    def test_an_unfinished_check_doesnt_block_when_something_else_passed(self):
        start = bash('pytest', 'Command running in background with ID: b3', extra={'backgroundTaskId': 'b3'})
        self.assertIsNone(C.judge(turn(edit(), start, bash('curl -s https://example.com/health'), say('It is deployed.'))))

    def test_a_check_that_also_writes_a_report_is_still_a_check(self):
        diff = {'bashEditDiff': {'files': [{'filePath': '/repo/report.md', 'hunks': []}]}}
        self.assertIsNone(C.judge(turn(edit(), bash('for t in a b; do python3 $t.py; done > report.md', extra=diff),
                                       say('Fixed.'))))

    def test_a_background_subagent_counts_only_once_it_finished(self):
        start = bash('', 'Async agent launched successfully.\nagentId: a1', name='Agent', uid='t-agent')
        self.assertIsNotNone(C.judge(turn(edit(), start, say('Fixed.'))))
        self.assertIsNone(C.judge(turn(edit(), start, notice('t-agent', task='a1'), say('Fixed.'))))

    def test_a_failure_hidden_by_a_pipe_is_read_from_the_summary(self):
        for cmd, out in (('pytest 2>&1 | tail -3', '=== 2 failed, 5 passed in 1.20s ==='),
                         ('python -m unittest discover | tail -2', 'Ran 7 tests in 0.1s\n\nFAILED (failures=2)'),
                         ('npm test || true', 'Tests:       1 failed, 9 passed, 10 total'),
                         ('cargo test | tail', 'test result: FAILED. 3 passed; 1 failed'),
                         ('npx tsc --noEmit | tail', "src/a.ts(3,5): error TS2322: Type 'x'.\nFound 1 error in src/a.ts")):
            v = C.judge(turn(edit(), bash(cmd, out), say('Fixed.')))
            self.assertIn('printed a failure', v[2], cmd)

    def test_a_run_of_zero_tests_is_not_proof(self):
        for cmd, out in (('pytest -k nothing', 'collected 0 items\n\n=== no tests ran in 0.01s ==='),
                         ('python -m unittest tests.missing | tail -3', 'Ran 0 tests in 0.000s\n\nNO TESTS RAN'),
                         ('npx jest src/none', 'No tests found, exiting with code 0')):
            v = C.judge(turn(edit(), bash(cmd, out), say('Fixed.')))
            self.assertIn('ran no tests', v[2], cmd)
        # Some modules ran tests and one ran none: that's not an empty run.
        out = 'test_a: OK\ntest_b: OK\ntest_c: NO TESTS RAN'
        self.assertIsNone(C.judge(turn(edit(), bash('for t in a b c; do python3 -m unittest $t | tail -1; done', out),
                                       say('Fixed.'))))

    def test_a_crash_after_a_zero_exit_is_not_proof(self):
        out = 'Traceback (most recent call last):\n  File "app.py", line 3\nValueError: bad config'
        self.assertIn('Python traceback', C.judge(turn(edit(), bash('python app.py | tail -5', out), say('Fixed.')))[2])

    def test_the_last_failing_test_decides_over_an_unrelated_success(self):
        t = turn(edit(), bash('pytest', PYTEST_FAIL, error=True), bash('python app.py --help'), say('Fixed.'))
        v = C.judge(t)
        self.assertIn('the last test or build after the edit did not pass', v[2])
        self.assertIn('say what still fails', C.message(*v))

    def test_saying_what_still_fails_is_not_hiding_it(self):
        for text in ('Fixed the parser. 1 test in test_legacy.py still fails; it was failing before.',
                     'The parser is fixed; the remaining failure is pre-existing.', '解析器已修复,还有 1 个测试失败,是原有问题。'):
            t = turn(edit(), bash('pytest', PYTEST_FAIL, error=True), bash('python app.py --help'), say(text))
            self.assertIsNone(C.judge(t), text)

    def test_a_later_passing_run_of_the_tests_counts(self):
        self.assertIsNone(C.judge(turn(edit(), bash('pytest', PYTEST_FAIL, error=True), bash('pytest', '5 passed'),
                                       say('Fixed.'))))

    def test_red_then_green_in_one_command_is_green(self):
        out = 'FAILED (failures=1)\n' + '-' * 20 + '\nRan 3 tests in 0.1s\n\nOK'
        self.assertIsNone(C.judge(turn(edit(), bash('python -m unittest t_old; python -m unittest t_new', out),
                                       say('Fixed.'))))

    def test_another_step_failing_is_not_a_test_failure(self):
        # pytest passed, git push failed: the push is not a failing check, and curl still proves something.
        out = 'Exit code 1\n3 passed in 0.12s\nerror: failed to push some refs'
        self.assertIsNone(C.judge(turn(edit(), bash('pytest -q; git push', out, error=True),
                                       bash('curl -s localhost:8000/health'), say('Fixed.'))))

    def test_a_run_meant_to_fail_is_not_a_failed_check(self):
        t = turn(edit(), bash('pytest', '5 passed'),
                 bash('cp -r . /tmp/m && cd /tmp/m && sed -i s/x/y/ app.py && pytest  # mutation: must fail',
                      PYTEST_FAIL, error=True),
                 say('Fixed.'))
        self.assertIsNone(C.judge(t))

    def test_a_label_then_its_number_is_not_a_failure(self):
        self.assertIsNone(C.judge(turn(edit(), bash('python3 tests/run_tests.py', 'OK\nrun 43 fail 0 err 0'),
                                       say('Fixed.'))))

    def test_commands_are_classified_by_what_they_run(self):
        for cmd, kind in (('pytest -q', 'test'), ('cd repo && npm test', 'test'), ('uv run pytest', 'test'),
                          ('go test ./...', 'test'), ('for t in a b; do python3 -m unittest $t; done', 'test'),
                          ('npx tsc --noEmit', 'build'), ('make', 'build'), ('cargo build --release', 'build'),
                          ('git add tests/test_x.py', 'other'), ('echo pytest', 'other'), ('make deploy', 'other'),
                          ('jest --watch', 'other'), ("python3 - <<'EOF'\nimport pytest\nEOF", 'other'),
                          ('python -c "import subprocess; subprocess.run([\'pytest\'])"', 'other')):
            self.assertEqual(C.check_kind(cmd), kind, cmd)


class Edits(unittest.TestCase):
    """What counts as changing the code after the check."""

    def test_a_failed_or_rejected_edit_changes_nothing(self):
        self.assertIsNone(C.judge(turn(edit(), bash('pytest', '5 passed'), edit(error=True), say('Fixed.'))))
        tested = turn(edit(), bash('pytest', '5 passed'), say('5 passed.'))
        self.assertIsNone(session(tested, later('Done.', edit(error=True))))

    def test_a_shell_edit_after_the_check_blocks(self):
        diff = {'bashEditDiff': {'files': [{'filePath': '/repo/app.py', 'hunks': []}]}}
        v = C.judge(turn(edit(), bash('pytest', '5 passed'), bash("sed -i '' 's/a/b/' app.py", extra=diff),
                         say('All 5 tests pass.')))
        self.assertIn('nothing ran successfully after the last edit', v[2])
        self.assertIsNone(C.judge(turn(edit(), bash("sed -i '' 's/a/b/' app.py", extra=diff), bash('pytest', '5 passed'),
                                       say('All 5 tests pass.'))))

    def test_files_changed_by_git_or_another_process_are_not_your_edit(self):
        diff = {'bashEditDiff': {'files': [{'filePath': '/repo/app.py', 'hunks': []}]}}
        for cmd in ('git pull --rebase', 'git fetch -q && git merge --ff-only origin/main && echo ok > /tmp/x', 'sleep 30'):
            self.assertIsNone(C.judge(turn(edit(), bash('pytest', '5 passed'), bash(cmd, extra=diff), say('Fixed.'))), cmd)

    def test_a_script_writing_data_files_is_not_a_code_edit(self):
        diff = {'bashEditDiff': {'files': [{'filePath': '/repo/ledger.json', 'hunks': []},
                                           {'filePath': '/repo/NOTES.md', 'hunks': []}]}}
        self.assertIsNone(C.judge(turn(edit(), bash('pytest', '5 passed'), bash('python3 log_run.py > ledger.json', extra=diff),
                                       say('Fixed.'))))

    def test_without_claude_codes_record_a_shell_edit_is_not_seen(self):
        # Documented blind spot: no bashEditDiff (older Claude Code, untracked file) means sed -i still counts as a run.
        self.assertIsNone(C.judge(turn(edit(), bash("sed -i '' 's/a/b/' app.py"), say('Fixed.'))))


class SessionTimeline(unittest.TestCase):
    """Acceptance cases: a check in an earlier turn, an edit after it, a failed check, no check at all."""

    def test_a_valid_check_in_an_earlier_turn_counts(self):
        tested = turn(edit(), bash('pytest', '14 passed'), say('14 passed.'))
        looked = later('It is in config.toml.', tool('Read', file_path='/repo/config.toml'))
        self.assertIsNone(session(tested, looked, later('Everything is fixed.')))

    def test_an_edit_after_the_check_needs_a_new_check(self):
        tested = turn(edit(), bash('pytest', '14 passed'), say('14 passed.'))
        v = session(tested, later('Renamed it.', edit()), later('Everything is fixed.'))
        self.assertIn('nothing ran in this turn', v[2])
        self.assertIsNone(session(tested, later('Renamed it.', edit()), later('Everything is fixed.', bash('pytest', '14 passed'))))

    def test_a_failed_check_in_an_earlier_turn_blocks_the_wrap_up(self):
        failed = turn(edit(), bash('pytest', PYTEST_FAIL, error=True), say('One test fails.'))
        v = session(failed, later('All done.'))
        self.assertEqual(v[0], 'done')
        self.assertIn('exited with code 1', v[2])

    def test_no_check_at_all_blocks(self):
        v = session(turn(edit(), say('Changed it.')), later('It is fixed.'))
        self.assertEqual(v[0], 'done')

    def test_check_reads_the_whole_transcript(self):
        tested = turn(edit(), bash('pytest', '14 passed'), say('14 passed.'))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 't.jsonl')
            with open(path, 'w') as f:
                f.write('\n'.join(json.dumps(e) for e in tested + later('All fixed.')) + '\n')
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(C.check(path), 0)


class FixNotes(unittest.TestCase):
    """Red then green, and checks that may not have loaded the change: notes, never blocks."""

    def notes(self, t, claim=None):
        ev = C.Evidence()
        ev.feed_turn(t)
        self.assertIsNone(C.verdict(ev, t), 'a note case must not be a block')
        return [k for k, _ in C.notes(ev, claim or C.done_claim(t))]

    def test_red_then_green_has_no_note(self):
        t = turn(bash('pytest tests/test_bug.py', PYTEST_FAIL, error=True), edit(),
                 bash('pytest tests/test_bug.py -q', '1 passed'), say('The bug is fixed.'))
        self.assertEqual(self.notes(t), [])
        t = turn(bash('python -m pytest tests/test_bug.py::test_x', PYTEST_FAIL, error=True), edit(),
                 bash('pytest', '40 passed'), say('The bug is fixed.'))
        self.assertEqual(self.notes(t), [])

    def test_green_without_red_is_a_note_not_a_block(self):
        t = turn(edit(), bash('pytest', '5 passed'), say('The bug is fixed.'))
        self.assertEqual(self.notes(t), ['no-red'])
        self.assertEqual(self.notes(turn(edit(), bash('pytest', '5 passed'), say('It is deployed.'))), [])
        # Red from other tests doesn't count as reproducing this bug.
        t = turn(bash('pytest tests/test_a.py', PYTEST_FAIL, error=True), edit(),
                 bash('pytest tests/test_b.py', '3 passed'), say('The bug is fixed.'))
        self.assertEqual(self.notes(t), ['no-red'])

    def project(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, '.git'))
        os.makedirs(os.path.join(d, 'tests'))
        os.makedirs(os.path.join(d, 'src'))
        with open(os.path.join(d, 'src', 'billing.py'), 'w') as f:
            f.write('def charge(): pass\n')
        return d

    def at(self, cwd, entries):
        for e in entries:
            e['cwd'] = cwd
        return entries

    def test_a_run_meant_to_fail_still_counts_as_the_red(self):
        t = turn(bash('pytest tests/test_bug.py  # should fail before the fix', PYTEST_FAIL, error=True), edit(),
                 bash('pytest tests/test_bug.py', '1 passed'), say('The bug is fixed.'))
        self.assertEqual(self.notes(t), [])

    def test_a_note_comes_with_the_turn_that_did_the_work_not_every_wrap_up(self):
        worked = turn(edit(), bash('pytest', '5 passed'), say('The bug is fixed.'))
        self.assertEqual(self.notes(worked), ['no-red'])
        ev = C.Evidence()
        ev.feed_turn(worked)
        wrap = later('Everything is fixed.')
        ev.feed_turn(wrap)
        self.assertIsNone(C.verdict(ev, wrap))
        self.assertEqual(C.notes(ev, C.done_claim(wrap)), [])

    def test_tests_that_never_mention_the_changed_file(self):
        d = self.project()
        with open(os.path.join(d, 'tests', 'test_auth.py'), 'w') as f:
            f.write('import auth\n')
        with open(os.path.join(d, 'tests', 'test_billing.py'), 'w') as f:
            f.write('from src.billing import charge\n')
        changed = os.path.join(d, 'src', 'billing.py')
        t = turn(edit(changed), self.at(d, bash('pytest tests/test_auth.py', '3 passed')), say('Deployed.'))
        self.assertEqual(self.notes(t), ['not-loaded'])
        t = turn(edit(changed), self.at(d, bash('pytest tests/test_billing.py', '3 passed')), say('Deployed.'))
        self.assertEqual(self.notes(t), [])
        # The whole suite, or tests picked by name: it can't tell what they load, so it says nothing.
        for cmd in ('pytest', 'pytest -k charge'):
            t = turn(edit(changed), self.at(d, bash(cmd, '3 passed')), say('Deployed.'))
            self.assertEqual(self.notes(t), [], cmd)

    def test_a_check_in_another_project(self):
        a, b = self.project(), self.project()
        t = turn(edit(os.path.join(a, 'src', 'billing.py')), self.at(a, bash(f'cd {b} && pytest', '3 passed')),
                 say('Deployed.'))
        self.assertEqual(self.notes(t), ['elsewhere'])
        t = turn(edit(os.path.join(a, 'src', 'billing.py')), self.at(a, bash('cd tests && pytest', '3 passed')),
                 say('Deployed.'))
        self.assertEqual(self.notes(t), [])

    def test_a_build_output_run_without_a_rebuild(self):
        t = turn(edit('/repo/src/app.ts'), bash('node dist/app.js --selftest'), say('Deployed.'))
        self.assertEqual(self.notes(t), ['stale-build'])
        t = turn(edit('/repo/src/app.ts'), bash('npm run build'), bash('node dist/app.js --selftest'), say('Deployed.'))
        self.assertEqual(self.notes(t), [])

    def test_coverage_showing_the_changed_file_at_zero(self):
        out = 'Name               Stmts   Miss  Cover\nsrc/billing.py        10     10     0%\nTOTAL   10 10 0%\n3 passed'
        t = turn(edit('/repo/src/billing.py'), bash('pytest --cov=src', out), say('Deployed.'))
        self.assertEqual(self.notes(t), ['coverage-0'])


class HookNotesAndLag(unittest.TestCase):
    _ENV = Hook._ENV
    setUp, tearDown, run_hook, write_options = Hook.setUp, Hook.tearDown, Hook.run_hook, Hook.write_options

    def test_a_note_is_shown_to_you_and_does_not_send_the_agent_back(self):
        code, out, log = self.run_hook(turn(edit(), bash('pytest', '5 passed'), say('The bug is fixed.')))
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertNotIn('hookSpecificOutput', data)
        self.assertIn('no run of them failed before it', data['systemMessage'])
        with open(log) as f:
            self.assertEqual(json.loads(f.readline())['kind'], 'note')
        buf = io.StringIO()
        with redirect_stdout(buf):
            C.main(['claim_check.py', 'report', '--log', log])
        self.assertIn('1 note shown', buf.getvalue())
        self.assertIn('No blocks logged yet', buf.getvalue())

    def test_notes_can_be_turned_off(self):
        self.write_options('claim-check@claim-check', notes=False)
        _, out, log = self.run_hook(turn(edit(), bash('pytest', '5 passed'), say('The bug is fixed.')))
        self.assertEqual(out, '')
        self.assertFalse(os.path.exists(log))

    def test_no_claim_means_no_transcript_read(self):
        _, out, _ = self.run_hook([], transcript_path='/nope.jsonl', last_assistant_message='Here is the plan.')
        self.assertEqual(out, '')

    def test_it_waits_for_a_transcript_that_is_behind(self):
        import threading
        head = turn(edit())
        tail = bash('pytest', '5 passed') + [say('All 5 tests pass.')]
        d = tempfile.mkdtemp()
        path = os.path.join(d, 't.jsonl')
        with open(path, 'w') as f:
            f.write('\n'.join(json.dumps(e) for e in head) + '\n')

        def catch_up():
            with open(path, 'a') as f:
                f.write('\n'.join(json.dumps(e) for e in tail) + '\n')
        timer = threading.Timer(0.3, catch_up)
        timer.start()
        try:
            _, out, _ = self.run_hook([], transcript_path=path, last_assistant_message='All 5 tests pass.')
        finally:
            timer.join()
        self.assertEqual(out, '')
        # If it never catches up, it judges what's there.
        _, out, _ = self.run_hook(head, last_assistant_message='All 5 tests pass.')
        self.assertIn('claim-check:', json.loads(out)['hookSpecificOutput']['additionalContext'])


class ReplayScope(unittest.TestCase):
    def test_replay_skips_subagent_transcripts(self):
        blocked = [dict(you('fix it'), uuid='u1')] + edit() + [say('Fixed.')]
        sub = [dict(you('review the diff'), uuid='u2', isSidechain=True), say('All checks are done.')]
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'p', 's1', 'subagents'))
            with open(os.path.join(d, 'p', 's1.jsonl'), 'w') as f:
                f.write('\n'.join(json.dumps(e) for e in blocked))
            with open(os.path.join(d, 'p', 's1', 'subagents', 'agent-1.jsonl'), 'w') as f:
                f.write('\n'.join(json.dumps(e) for e in sub))
            buf = io.StringIO()
            with redirect_stdout(buf):
                C.replay(d)
        out = buf.getvalue()
        self.assertIn('1 transcript, 1 turn; 1 subagent transcript skipped', out)
        self.assertIn('1 turn ends with the claim; 1 has no proof', out)


if __name__ == '__main__':
    unittest.main()
