# Vox 项目接手交接

日期：2026-08-22
任务：task-001-vox-migration
状态：REVIEW（实现已完成，待独立审查/人工合并）

## 当前入口

- 工作分支：`codex/vox-migration`
- Git 远程：`https://github.com/DUEDCL/vox-.git`
- 本地工作区：`D:\program\vioce-wake`（物理目录尚未改名，避免中断当前 Codex 工作区）
- 临时产物归档：`D:\program\vioce-wake-archive-20260822`

## 本次改动

1. 项目品牌、Python 插件包和桌面包名改为 Vox：
   - `evox_plugin/` → `vox_plugin/`
   - Cargo package → `vox`
   - npm package → `vox-desktop`
   - Tauri product name / identifier → `Vox` / `ai.vox.voicewake`
2. 项目自有环境变量、桌面可执行文件候选路径、Tauri IPC 命令和前端自定义事件统一为 `VOX_*` / `vox_*`。
3. 保留 EvoX 作为外部会话桥接和 Agent kind（`evox`）名称，不破坏外部协议边界。
4. `contracts/voice-events.schema.json` 未修改，工作副本与 HEAD SHA-256 均为 `2a917916f4cee389a0b3f288338d00165c20b71e`。
5. 已将 pytest/design/log、前端依赖、Vite dist 和 Rust target 归档到工作区外，未删除模型、虚拟环境或受保护数据。
6. 增加/保留 `.ai/` 协作任务、GitHub 模板和验证脚本，用于后续接手。

## 验证证据

- `D:\program\vioce-wake\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run`：`600 passed, 3 skipped`，退出码 0。
- `D:\program\vioce-wake\desktop\` 下 `npm run build`：通过，退出码 0。
- `D:\program\vioce-wake\desktop\src-tauri\` 下 `cargo check --locked`：通过，退出码 0。
- `git diff --check`：无空白错误。
- 验证等级：本次为 AUTO（自动测试）/ DOC（静态检查）；没有声称 REAL-MIC、REAL-AGENT、REAL-EVOX 或 REAL-WIN。

## 项目框架速览

- `vox_plugin/`：对外插件门面和运行时编排。
- `core/state.py`、`core/events.py`：六态语音状态机与事件信封构造。
- `core/audio/`：KWS/VAD/ASR/TTS/声纹等本地音频能力；声纹相关路径受保护。
- `core/agents/`：CLI、ACP、HTTP 和 EvoX 可替换 Agent 适配器。
- `core/session_bridge.py`：本地优先、Bearer Token、loopback、URL 凭据拦截和 turn_id 编码的会话桥。
- `core/dispatch/`、`core/tools/`、`core/memory/`：回合分发、工具策略和文本记忆。
- `desktop/`：Vite + TypeScript 前端与 Tauri 2/Rust 透明置顶窗口。
- `contracts/`：版本化语音/Agent 事件契约；`voice-events.schema.json` 字节内容不可变。
- `tests/`：Python 自动化测试；桌面行为通过 SIM 钩子和 Rust 单测覆盖。
- `docs/handoff.md`、`docs/project-overview.md`、`docs/architecture.md`、`docs/testing.md`：接手后的首要阅读入口。

## 待人工确认 / 风险

1. 远程仓库 `vox-` 当前是 GitHub 上的空仓库，且 API 显示为 public；本次不修改远程可见性。
2. 物理目录仍叫 `vioce-wake`。若要改为 `D:\program\vox`，应在关闭当前工作区后单独执行，并同步 `.claude/settings.json` 等路径配置。
3. Tauri identifier 已改为 `ai.vox.voicewake`，会影响 Windows 应用身份/升级识别，发布前需人工确认是否接受。
4. `EVOX_*` 兼容变量未保留；当前代码统一读取 `VOX_*`。若已有部署依赖旧变量，下一任务应增加显式兼容层并补测试。
5. 未创建 `_self_evolve/`，因为项目规则要求先取得用户许可。
6. 本次未进行独立 Claude 审查，因此不能标记 `PASS`/`VERIFIED`；实现者不批准自己的实现。

## 下一步建议

1. 独立审查 `git show --stat` 和改名 diff，重点看 IPC 事件、环境变量、Tauri identifier 及 `vox_plugin` rename。
2. 人工确认是否接受公开仓库、物理目录改名和 Tauri identifier 改变。
3. 审查通过后合并 `codex/vox-migration`，再开始功能开发。
4. 首个功能任务优先补 REAL-AGENT/REAL-EVOX/REAL-WIN 验收资产，避免继续扩大仅 AUTO/SIM 的“已完成”范围。