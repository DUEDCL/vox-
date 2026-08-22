# Vox 项目认知核心

更新时间：2026-08-23
来源：首次接手扫描、Vox 改名迁移与后续生命周期加固任务

## 项目目标

Vox 是 Windows 本地优先的语音唤醒对话原型：中文唤醒 → 本地 ASR/VAD → 可替换 Agent 会话桥 → 本地 TTS → 连续对话与打断，并通过 Tauri 透明置顶窗口呈现状态。

## 技术栈与模块边界

- Python：`core/`、`vox_plugin/`、`tests/`；负责语音状态、事件、音频提供器、Agent 适配器、会话桥、工具策略、记忆与运行时。
- Desktop：Vite + TypeScript 前端，Tauri 2 + Rust 宿主；负责透明置顶唤醒球、命中区、拖动、显隐、确认卡和父子进程事件管道。
- Contracts：`contracts/` 是可替换组件之间的稳定边界。
- Docs/AI：`docs/` 记录架构、需求、验证证据；`.ai/` 记录任务、审查、交接和协作协议。

## 必须保持的边界

1. 本地优先：不得新增云端调用、遥测或音频上传；记忆只存文本，声纹缓冲不落盘。
2. 契约优先：`contracts/voice-events.schema.json` 字节内容和版本保持不变；公开事件不能泄漏具体音频实现。
3. 安全拒绝优先：凭据、声纹、原始音频和模型权重不可读/不可提交；工具、确认、桥接错误默认 fail-closed。
4. EvoX 是外部会话/Agent 后端名称，不等同于 Vox 项目品牌；只有项目自有命名使用 `vox`/`VOX_*`。
5. 不执行 `git reset --hard`、`git clean`、强制推送或覆盖已有未提交业务修改。
6. 不擅自改变数据库结构、核心依赖、部署安全边界或 Windows 应用身份策略。

## 开发规范

- 改动前先读取 `.ai/CONTRACT.md`、`.claude/CLAUDE.md` 和相关记忆。
- 每个任务先写 `.ai/tasks/task-*.md`，声明允许范围、禁止范围、验收标准和验证命令。
- UI 组件考虑空、错、边界状态；接口输入校验、输出结构一致、错误可追溯；测试覆盖正常、边界和拒绝路径。
- 证据等级必须诚实：DOC < AUTO < SIM < REAL-MIC < REAL-AGENT < REAL-EVOX < REAL-WIN。
- 实现者不得批准自己的实现；独立审查必须有报告和实际命令证据。

## 当前进度快照

- Vox 改名迁移已完成；当前开发分支已推送到用户仓库。
- Python：`625 passed, 3 skipped`；DesktopBridge 专项 `33 passed`；前端构建和 Rust `cargo check` 已通过，均为 AUTO/SIM。
- VoiceRuntime、麦克风采集和 DesktopBridge 生命周期已加固；REAL-AGENT、REAL-EVOX、REAL-WIN、REAL-MIC 仍是后续发布风险。
- 物理目录仍为 `D:\program\vioce-wake`，暂未改名以避免中断当前工作区。

## 变更记录

- 2026-08-22：建立 Vox 项目认知核心；项目专属规则优先于通用规则。
- 2026-08-23：同步当前测试基线与生命周期加固进度；保留真实设备/Agent/窗口证据边界。
