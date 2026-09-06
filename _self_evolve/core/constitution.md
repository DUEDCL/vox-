# Vox 项目认知核心

更新时间：2026-08-24
来源：首次接手扫描、Vox 改名迁移与后续生命周期加固任务

## 项目目标

Vox 是 Windows 语音唤醒对话平台：中文唤醒、VAD 与声纹准入在本机完成，ASR/TTS 与 Agent 后端按配置使用本机或云端能力，并通过 Tauri 透明置顶窗口呈现状态。

## 技术栈与模块边界

- Python：`core/`、`vox_plugin/`、`tests/`；负责语音状态、事件、音频提供器、Agent 适配器、会话桥、工具策略、记忆与运行时。
- Desktop：Vite + TypeScript 前端，Tauri 2 + Rust 宿主；负责透明置顶唤醒球、命中区、拖动、显隐、确认卡和父子进程事件管道。
- Contracts：`contracts/` 是可替换组件之间的稳定边界。
- Docs：`docs/` 记录架构、需求、验证证据、ADR 与故意没做的技术债（`docs/backlog.md`）。

## 必须保持的边界

1. 本机准入、处理可配置：唤醒词与声纹不出网；通过准入后的请求可按版本化配置使用云端 ASR/TTS；记忆只存文本，声纹缓冲不落盘，未经配置不启用遥测。
2. 契约优先：`contracts/voice-events.schema.json` 字节内容和版本保持不变；公开事件不能泄漏具体音频实现。
3. 安全拒绝优先：凭据、声纹、原始音频和模型权重不可读/不可提交；工具、确认、桥接错误默认 fail-closed。
4. EvoX 是外部会话/Agent 后端名称，不等同于 Vox 项目品牌；只有项目自有命名使用 `vox`/`VOX_*`。
5. 不执行 `git reset --hard`、`git clean`、强制推送或覆盖已有未提交业务修改。
6. 不擅自改变数据库结构、核心依赖、部署安全边界或 Windows 应用身份策略。

## 开发规范

- 改动前先读取 `AGENTS.md` 和相关项目文档。
- 开工先明确「完成的判据」，收尾对照判据逐条报告，并给出实际跑过的命令与输出。
- UI 组件考虑空、错、边界状态；接口输入校验、输出结构一致、错误可追溯；测试覆盖正常、边界和拒绝路径。
- 证据等级必须诚实：DOC < AUTO < SIM < REAL-MIC < REAL-AGENT < REAL-EVOX < REAL-WIN。
- 「跑过测试」必须能贴出命令与输出；没跑就说没跑，失败就贴失败。

## 当前进度快照

- Vox 改名迁移已完成；所有开发分支已收敛到 `main`，仓库只保留这一条主干。
- Python：`1190 passed, 3 skipped`（干净 shell）；DesktopBridge 专项 `33 passed`；前端构建和 Rust `cargo test` 15 passed，均为 AUTO/SIM。
- VoiceRuntime、麦克风采集、DesktopBridge 生命周期和工具/Agent 事件隐私与派发/熔断/记忆 sink 隔离边界已加固；REAL-AGENT、REAL-EVOX、REAL-WIN、REAL-MIC 仍是后续发布风险。
- 物理目录仍为 `D:\program\vioce-wake`，暂未改名以避免中断当前工作区。

## 变更记录

- 2026-08-22：建立 Vox 项目认知核心；项目专属规则优先于通用规则。
- 2026-08-23：同步当前测试基线与生命周期加固进度；保留真实设备/Agent/窗口证据边界。
- 2026-08-23：补强工具事件的固定原因过滤与 sink 隔离；内部诊断和公开事件继续分层。
- 2026-08-24：统一 Dispatcher、CircuitBreaker、MemoryWriter、MemoryRecaller 的 sink best-effort 语义并增加失败计数。
- 2026-08-29：双 Agent 审查流程（`.ai/`、`AGENTS.md`、GitHub Agent HQ 模板与 CI）由使用者决定移除；分支收敛到 `main` 单主干，规则来源收敛为公开的 `AGENTS.md`，`CLAUDE.md` 仅作兼容入口。证据诚实与安全边界不变。
