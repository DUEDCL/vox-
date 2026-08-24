# Task 011 — Hermes 自主完善路线图（免独立审查模式）

状态：IN_PROGRESS
创建日期：2026-08-24
执行者：DeepSeek Hermes
分支：`hermes/platform-dev`（自 `5a492e0` 切出）
授权：人类直接指示「不使用审查机制，全程完善开发本项目」——本任务免除独立审查环节，由实现者自行验证并如实记录；其余红线不变。

## 不变的约束

- 三条设计红线（本地优先 / 组件可替换 / 验证等级诚实）照常生效；
- 免除的是「独立审查」流程，不是证据诚实：AUTO/SIM 不冒充 REAL-*；
- Task 008 的合并与主分支推进仍属人工闸门，本任务不触碰；
- 禁止 reset --hard / clean / stash / force-push；不覆盖他人未提交文件（task-009/010 与 008 审查报告保持未跟踪，由人类处置）。

## 自主可完成的工作队列（按依赖与价值排序）

1. ✅ **记忆跨进程持久性验收**（2026-08-24 完成）：`scripts/acceptance/verify_memory_persistence.py` + `tests/integration/test_memory_cross_process.py`；组内 66 passed，全量 **635 passed, 3 skipped**。文档已同步（routines/testing/handoff/project-overview/prototype-results/CLAUDE.md）。剩余：真机应用重启人工确认（REAL，P10）。
2. ✅ **TTS 多段排队**（2026-08-24 完成）：`split_speech` 切分在编排器、`tts.chunk` 逐段 index、provider `speak_segments`/`stop()`/`is_stopped()`；顺带修复真 provider 无 stop() 导致 barge-in 停不掉声音的缺陷。新增 tests/test_plugin_tts_queue.py 14 例，全量 **649 passed, 3 skipped**。真实出声与口头打断保持 REAL 待在场。
3. ✅ **Agent 超时与重连策略**（2026-08-24 完成）：核实 CLI 已有 120s 超时（错误 chunk）、runtime 已有回合级恢复；补上桥接仅连接期重试 `attempts`/`retry_backoff_s`（拒绝/DNS 才重试，超时与 HTTP 状态绝不自动重发——防回合重复执行）。新增 6 例测试，安全姿态未降级。
4. ⛔ **REAL-AGENT 探测与真实一轮**（2026-08-24 复测：三后端全部受阻）——claude Not logged in；codex exec 90s 无输出（疑似登录挂起）；opencode 无法连接其云端点。保持 SIM，待人类在本机任一后端完成登录/网络后重试。
5. **Canvas 2D 生产渲染器**（P8 缺口）：替换 DOM+CSS 主渲染路径，npm build + 页面冒烟验证。
6. **模型分发策略文档**（阻塞项 #11 文档部分）：归档去留、体积预算、获取方式决策稿。

## 需要人类在场或决策（不假装可自动化）

- REAL-MIC 三项（#1/#2/#8，见 task-010）；
- REAL-WIN 窗口/DPI/托盘/RDP（#6）；30 分钟资源画像可在无人值守下启动，但结论仍需人看环境；
- EvoX 真实端点（#3）——本机无服务；
NaN
- 一切合并主分支动作。

## 每个工作项的完成定义

最小回归绿 → 全量基线不低于 634 passed, 3 skipped → git diff --check 干净 → 文档同步（prototype-results/routines/project-overview 按需）→ 本任务内勾选并记录实际数字。
