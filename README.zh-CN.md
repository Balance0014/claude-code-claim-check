# claim-check

**一个 Claude Code 的 Stop 钩子：AI 想说「修好了」，得先拿出证据。**

[English](README.md) · MIT · Python 3.9+，不用装任何依赖

![claim-check 在 Claude Code 里拦下一句没有证据的「修好了」，AI 跑完测试后带着结果重新回答](docs/demo.svg)

用 AI 写代码，最坑人的是两句话：一句是「修好了」，其实改完之后什么都没跑；另一句是「这个没地方在用」，其实只搜了一次。两句听着都像板上钉钉，我就接着往下做，直到出了问题才发现不对。

2026 年 11 月 4 日，我用这个钩子回放了自己的 Claude Code 历史记录，共 3,977 轮（恢复会话时，之前的几轮会被原样复制进新文件，这样的副本有 1,649 份，已去掉）。其中 404 轮以「做完了 / 修好了 / 上线了 / 测试通过」这类话收尾，按钩子的规则，有 82 轮会被拦下。这 82 轮大多不是我原来担心的那种。

- 76 轮一个工具都没调用，多半是收尾汇报：说某件事修好了、上线了，却没给提交号、测试结果或状态码。证据可能在前面几轮，但我平时只看汇报，而汇报里看不到。
- 5 轮调了工具，但只是读文件、搜索、更新待办，或者命令报错了。
- 1 轮是改完代码什么都没跑，就说做完了。这才是我做这个钩子要防的情况。它这么少，多半是因为旧版从 5 月起一直在我电脑上拦着，你自己跑出来可能会多不少。

这 82 轮里我随机抽了 20 轮，逐条人工核对：16 轮正是这种说活干完了却什么证据都没给的汇报；2 轮其实不是在说活干完了，一轮是调研结论（「已验证会让模型变差」），一轮是条件句（「合并后就算做完了」）；另外 2 轮算不算都说得通。

另外，有 18 轮以「没找到」这类话收尾，其中 2 轮只查了一次，或者一次都没查。

每当 AI 想结束这一轮，钩子就把它嘴上说的，和你上一句话之后它实际跑过的东西对一遍。缺证据，就打回去一次，告诉它缺什么：

```text
claim-check: you wrote "is fixed", but nothing ran after the last edit. Run the check
that proves it (tests, the command, a request to the live URL) and show the result,
or say plainly that it is not verified yet.
```

## 怎么判

| AI 说 | 这一轮里必须有 |
| --- | --- |
| 「已修复」「已上线」「测试通过」「fixed」「deployed」「done」 | **最后一次改代码之后**真跑过点东西，而且没报错：一条命令、一次浏览器或接口调用、一次网页请求，或者一个子代理。只是看看的命令（`cat`、`grep`、`ls`、`git diff` 这类）、回头再读一遍文件、更新待办清单都不算；验完之后改笔记、改文档没关系。 |
| 同样的话，但这一轮没改代码，也没真跑过东西 | 回复里带证据：提交号、测试数、状态码、CI 链接。这一轮只改了文档的，直接放行。 |
| 「没找到」「没人调用」「doesn't exist」 | 至少读过、搜过或跑过两次。 |
| 「还没验证」「未测试」「not verified yet」 | 什么都不用。老实承认没验，一律放行。 |

代码块、引号、问句和 Markdown 表格行里的话都不算，「闲聊/已解决/广告」这种用斜杠隔开的标签也不算。它不会连着拦两次，所以 AI 不会被卡死；钩子自己出错时，一律放行。

## 安装

**Claude Code 插件：**

```text
/plugin marketplace add Balance0014/claude-code-claim-check
/plugin install claim-check@claim-check
```

装完开一个新会话。

**不用插件：**把 `claim_check.py` 放到任意位置，在 `~/.claude/settings.json` 里加上：

```json
{
  "hooks": {
    "Stop": [{ "hooks": [{ "type": "command", "command": "python3 /path/to/claim_check.py" }] }]
  }
}
```

**Codex 和其他 agent：**`npx skills add Balance0014/claude-code-claim-check` 装的是同一套规则的技能版。AI 会读，但没有东西强制它照做。Codex 有自己的钩子机制，这一版还没接。

## 看看你自己的比例

克隆仓库后运行：

```sh
python3 claim_check.py replay
```

它读 `~/.claude/projects` 里的历史会话，算出你的 AI 说「做完了」「没找到」时，有多少次会被这个钩子拦下，并按原因分类。数据不出你的电脑。

## 查它有没有拦错

每次拦截都记在 `~/.claude/claim-check/blocks.jsonl`，`python3 claim_check.py report` 列出最近十条。拦错了请[提 issue](https://github.com/Balance0014/claude-code-claim-check/issues)，附上被拦的那句话。每一次误拦我都当 bug 修。

## 它做不到的

- 它只匹配固定说法，AI 写「我认为应该可以了」就能绕过去。
- 改完之后跑过命令，只能证明跑了东西，证明不了测对了地方。
- 只认得中文和英文的说法。
- Markdown 表格行不检查，所以状态表里写着「已修复」也能过。
- 调研结论里如果用了「已验证」这类词（比如「已验证会让模型变差」），也会被当成「做完了」拦下。条件句也一样，比如「合并后就算做完了」，它认不出「合并后」是条件。
- 它只认 Claude Code 自带的改文件工具。用 `sed -i` 这类命令改的文件，它看不出是改动。
- Windows 上如果没有 `python3` 命令，把 `hooks/hooks.json` 或设置里的 `python3` 改成 `python`。
- 只有 Claude Code 能强制；在别的 agent 上是技能，照不照做看 AI 自己。

## 为什么做这个

我一个人经营一家公司，同时跑着 18 个 Claude Code 会话和大约 20 个定时 AI 任务（[具体怎么管](https://github.com/Balance0014/one-person-ai-company/blob/main/README.zh-CN.md)；这个工具就是其中[第 5 章](https://github.com/Balance0014/one-person-ai-company/blob/main/playbook/zh-CN/05-definition-of-done.md)做成的钩子）。第一版发布前那一周，我电脑上的旧版拦了 19 次。

为第一版重写时，我发现旧版有个 bug：它把工具返回的结果也当成了我新说的话，所以只数 AI 最后一条消息里的查找，前面查过的全都看不见。有一次 AI 明明已经搜过好几遍才说「没找到」，还是被它拦了。新版只把人亲手打的字算作新一轮，测试也分正反两类：该拦的要拦住，该放的要放行。

---

作者 [Balance Zhao](https://github.com/Balance0014)。新工具会先发在 [X](https://x.com/balance0014) 上。
