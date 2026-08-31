# Vox：仓库入口

@.claude/CLAUDE.md

本文件只写**怎么干活**（流程）。项目的硬约束在 `.claude/CLAUDE.md`，那份说的是**干什么、
不许干什么**。

## 一条主干，没有例外

这个仓库只有 `main`。不开长期分支、不开 worktree 并行、不搞「实现者 / 审查者」双 Agent 接力。

2026-08-29 之前用过一套双 Agent + GitHub Agent HQ 的流程（`.ai/tasks|reviews|handoffs`、
`AGENTS.md`、Issue/PR 模板、`ai-contract.yml`），它的产出是 **11 个分支、3 个 worktree、
100 个文件在工作区里躺了 6 天没提交**，而且工作所在的分支和当时打开的 worktree 差 13 个提交。
那套流程已整体删除，不要再建回来。

## 每次开工前（一条命令）

```bash
git status --short && git branch --show-current && git log --oneline -3
```

三件事同时成立才动手：**在 `main` 上**、**工作区干净**、**最新提交是你认得的那个**。
不成立就先处理，别在上面叠新改动 —— 上面那 100 个文件就是这么攒出来的。

## 每次收工前（顺序不能换）

```bash
.venv/Scripts/python.exe -m pytest tests -q
```

绿了（当前基线 **1190 passed, 3 skipped**）再提交，然后**当场推送**：

```bash
git add -A && git commit && git push
```

一次会话 = 一个提交 = 一次推送。**不要留到下次**：下一次会话看不见你这次改了什么，
只看见一个脏工作区，然后它会在上面继续叠。

## 给使用者的命令一律写 PowerShell

这台机器上的人在 **PowerShell** 里跑命令（提示符 `PS D:\program\vioce-wake>`），不是 bash。
所以贴给他的命令必须能直接粘进 PowerShell 跑：

| 不要写 | 要写 |
|---|---|
| `cd /d/program/vioce-wake` | `cd D:\program\vioce-wake` |
| `PYTHONUTF8=1 python ...`（前置赋值） | `$env:PYTHONUTF8=1` 单独一行，然后跑 |
| `cmd1 && cmd2` | `cmd1; cmd2`（PS 5.1 不认 `&&`） |
| `.venv/Scripts/python.exe` | `.\.venv\Scripts\python.exe` |
| `grep` / `tail` / `cat` | `Select-String` / `-Tail` / `Get-Content` |

**我自己在工具里用 bash 是另一回事** —— 那是我的执行环境。两者不要混：贴给使用者的命令
用他的 shell 写，我自己跑什么不需要他知道。

## 提交信息

Conventional Commits：`feat|fix|docs|test|refactor|chore(scope): 描述`。
正文写清三件事：改了什么、为什么、跑了哪些命令得到什么结果。

**不加 `Co-Authored-By` trailer。** GitHub 会把那个邮箱算进仓库的 Contributors 列表，
而这个仓库的 Contributors 只应该有人。正文最后一行写完就结束，不留署名。

## 只在这两种情况下开分支

1. 明知要改坏东西的实验 —— 用完当天删掉。
2. 需要给别人看 diff 的 PR —— 合并后当天删掉。

两种都不适用就直接在 `main` 上提交。完整的判断与命令见
[`docs/git-workflow.md`](docs/git-workflow.md)。

## 不许做的 git 操作

`reset --hard` / `clean -f` / `push --force` / `stash` 一律先问人。发现工作区有你没写过的
改动时**保留并避让**，不要覆盖、不要 stash。
