# Git 工作流 —— 一条主干，一次会话一个提交

这份指南只解决一个问题：**多个 AI 会话、多个 Agent 轮流开发同一个仓库，最后分支和工作区
乱掉**。它不是通用 Git 教程，规则少到能背下来。

## 1. 上次到底乱成什么样（这是为什么有这份文件）

2026-08-29 整理前的实测状态：

| 现象 | 实测数字 |
|---|---|
| 本地分支 | **11 个**（`main` + 8 个 `codex/*` + 2 个 `claude/*` + `hermes/platform-dev`） |
| Git worktree | **3 个**（主目录 + 2 个 `.claude/worktrees/*`） |
| 未提交改动 | **100 个文件**（50 改 + 50 新），跨 6 天 |
| 远端 | GitHub 上 10 个分支，最新工作**一个都没推** |
| `origin/HEAD` | 指向 `codex/vox-migration` —— 一个早就被超过的迁移分支 |
| 最坑的一条 | 当时打开的 worktree 在 `claude/infallible-dijkstra-991f94`（干净），而**全部工作在主目录的另一个分支上**，两者差 13 个提交 |

最后一条是这类混乱的典型症状：**看起来干净的工作区，只是因为你站在错的地方。**

好消息是 11 个分支是一条直线（互为祖先），所以收敛没丢任何提交。坏消息是这种运气不可
复制 —— 两个 Agent 真的并行写同一个文件时，丢的就是代码。

## 2. 三条规则

1. **一条主干**：只用 `main`。
2. **一次会话一个提交，收工就推**。
3. **同一时刻只有一个会话在写这个仓库**。

第 3 条是前两条能成立的前提。要并行，就并行到**不同仓库**，不是不同分支。

## 3. 开工：一条命令，三个判据

```powershell
git status --short
git branch --show-current
git log --oneline -3
```

- 输出的第一段是空的 → 工作区干净 ✅
- 第二段是 `main` ✅
- 第三段的最新提交你认得 ✅

三个都对才开始写代码。任何一个不对，先看第 6 节。

## 4. 收工：测试 → 提交 → 推送

顺序不能换。测试没绿就提交，等于把「不知道能不能跑」这件事留给下一个会话去发现。

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp ".pytest-git-$PID"
```

测试数量会随功能演进和可选依赖变化，以当前命令输出为准。必须用 `.venv` 里的 Python，系统 Python 没装 sherpa-onnx。

基线要在**干净 shell** 里记 —— 先运行 `Get-ChildItem Env:PYTHON*`，确认没有影响基线的临时变量。设了 `PYTHONUTF8` 的
shell 曾经把一个失败用例变成通过，那次的基线数字整整错了一轮。

绿了再提交：

```powershell
git add <本次任务明确修改的文件>
git commit -m "feat(scope): 描述"
```

**当场推送，不要留到下次**：

```powershell
git push
```

留到下次的代价是具体的：下一个会话看不到你改了什么，只看到一个脏工作区，然后在上面
继续叠。第 1 节那 100 个文件就是这么攒的。

## 5. 提交信息写什么

格式 `feat|fix|docs|test|refactor|chore(scope): 描述`，正文回答三个问题：

- **改了什么**（文件与意图，不是 diff 复述）
- **为什么**（根因；「顺手」不是理由）
- **跑了什么，结果是什么**（命令 + 数字；跳过的步骤要说跳过）

## 6. 状态不对的时候怎么办

### 工作区有一堆改动，不知道是谁写的

先看，再决定，**不要 stash 也不要 reset**：

```powershell
git status --short
git diff --stat
```

是你要的 → 跑测试然后提交。不是你要的 → 停下来问人。**这个仓库禁止**
`reset --hard` / `clean -f` / `push --force` / `stash` 不问人就跑。

### 不在 main 上

```powershell
git branch --show-current
```

先把当前分支的东西提交掉（别带着改动切分支），然后：

```powershell
git switch main
git merge --ff-only <那个分支>
```

`--ff-only` 是故意的：能快进说明是直线，不能快进说明真的分叉了，那时候才需要想。

### 分支又攒多了

先确认它们都已经在 `main` 里（**这一步不能跳**）：

```powershell
git branch --merged main
```

列出来的才可以删。没列出来的分支有独有提交，删了就丢代码：

```powershell
git branch -d <分支名>
```

用 `-d` 不用 `-D`：`-d` 在有独有提交时会拒绝，`-D` 会闷头删掉。远端同名分支：

```powershell
git push origin --delete <分支名>
```

### 冒出了 .claude/worktrees/

Claude Code 有时会为一次任务开 worktree。它本身没问题，**忘了收才有问题** —— 第 1 节
最坑的那一条就是 worktree 留着、人站错了地方。看一眼：

```powershell
git worktree list
```

只该有一行（主目录 `D:\program\vioce-wake`）。多出来的，确认干净后移除：

```powershell
git worktree remove .claude/worktrees/<名字>
```

## 7. 多个 Agent / 多个会话怎么用

**串行，不并行。** 一个会话干完 → 测试绿 → 提交 → 推送 → 才开下一个。下一个会话开工时
第 3 节那条命令会告诉它站对了没有。

想让两个 Agent 同时干活，就给它们**两个仓库**（`git clone` 到不同目录），各自推自己的
提交。同一个仓库里靠分支隔离两个正在写文件的 Agent，是第 1 节那份实测的来源。

## 8. 现在的仓库长什么样

| 项 | 状态 |
|---|---|
| 分支 | `main` 是发布主线；其他远端分支应定期审查 |
| worktree | 目标状态只有主目录；本机残留需确认干净后再移除 |
| 测试基线 | 以当前隔离 `--basetemp` 命令输出为准 |
| 已删除的流程 | `.ai/`（协议 + 25 个任务/交接/审查档）、旧版双 Agent 流程文件、`scripts/ai/*.ps1`、`.github/` 的 Issue/PR 模板与 `ai-contract.yml` |

删掉的文件都还在 git 历史里，需要时可以取回：

```powershell
git show 4f5e526:.ai/CONTRACT.md
```
