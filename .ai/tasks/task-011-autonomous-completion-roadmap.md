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
2. **TTS 多段排队**（已知缺口）：按句切分合成队列、打断即清空；fake playback 测试；真实出声仍标 REAL 待在场。
3. **Agent 超时与重连策略**（缺口 #4）：dispatcher/session_bridge 层超时、退避重连；桥接安全姿态（bearer/loopback/凭据拦截）不得降级。
4. **REAL-AGENT 探测与真实一轮**（阻塞项 #5）：claude 登录态当前不通（已复测）；探测 codex exec / opencode 可用性，可用则真跑一轮并记 REAL-AGENT。
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
