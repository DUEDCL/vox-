# 测试文档(Testing)

> 工作区:`D:\program\vioce-wake`
> 最后更新:2026-07-28
> 配套 [routines.md](routines.md)(改完什么跑什么)与 [prototype-results.md](research/prototype-results.md)(实测数据)。

## 1. 验证等级定义

这是本项目最重要的测试纪律:**不得把低等级证据当高等级用**。

| 等级 | 含义 | 能证明 | 不能证明 |
|---|---|---|---|
| **DOC** | 上游文档或他仓报告,本地未复现 | 参考价值 | 本机可用性 |
| **AUTO** | 本地自动化测试/脚本,确定性,无外部硬件 | 代码逻辑正确 | 真实设备行为 |
| **SIM** | 模拟输入(mock 传输/合成音频/headless 浏览器) | 代码路径连通 | 真机表现 |
| **REAL-MIC** | 本机真实麦克风 | 音频链路真实可用 | 多环境质量 |
| **REAL-EVOX** | 真实 EvoX 会话 | 端到端业务闭环 | — |
| **REAL-WIN** | 真实 Tauri/WebView2 窗口 | 透明/DPI/多屏/RDP | — |

**当前达成**:DOC / AUTO / SIM 已建立,REAL-MIC 有一次唤醒验证。**REAL-EVOX 与 REAL-WIN 完全空白**,构成主要发布风险。

## 2. 测试环境

### 2.1 环境记录(2026-07-28 实测)

| 项 | 版本 |
|---|---|
| 操作系统 | Windows 11 Pro (10.0.26200) |
| Python(隔离 `.venv`) | 3.12.10 |
| sherpa-onnx / sherpa-onnx-core | 1.13.4 |
| numpy | 2.5.1 |
| sounddevice | 0.5.5 |
| soundfile | 0.14.0 |
| pytest | 9.1.1 |
| Node.js | v24.11.1 |
| npm | 11.7.0 |
| TypeScript | 5.6.x |
| Vite | 8.1.x |
| Tauri | 2.x |

### 2.2 环境搭建

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` 通过 `-r requirements-voice.txt` 引入固定的语音运行时依赖,并额外固定 `pytest==9.1.1`;生产运行环境可只安装 `requirements-voice.txt`。

### 2.3 模型依赖

模型**不由 pip 安装**,需单独准备,总计约 413 MB:

| 模型 | 路径 | 体积 | 用途 |
|---|---|---:|---|
| KWS Zipformer | `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/` | 36 MB | 中文唤醒 |
| Silero VAD | `models/silero_vad.onnx` | 2.3 MB | 端点检测 |
| MeloTTS VITS | `models/vits-melo-tts-zh_en/` | 183 MB | 中英合成 |
| (未清理归档) | `models/kws.tar.bz2` + `tts.tar.bz2` | 192 MB | 可删除 |

Silero VAD SHA-256:`1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`

## 3. 测试分层

```
      ┌─────────────────────────────────┐
      │  L5 真机验收 REAL-*  ← 空白最多   │  发布阻塞
      ├─────────────────────────────────┤
      │  L4 端到端模拟 SIM               │  e2e_simulated.py
      ├─────────────────────────────────┤
      │  L3 集成测试 AUTO                │  t10 harness / 桥接 HTTP
      ├─────────────────────────────────┤
      │  L2 契约测试 AUTO                │  schema / 状态机
      ├─────────────────────────────────┤
      │  L1 单元测试 AUTO                │  provider / plugin 工具面
      └─────────────────────────────────┘
```

### L1–L2 单元与契约(`tests/`,6 文件 337 行,21 用例)

| 文件 | 用例数 | 覆盖内容 |
|---|---:|---|
| `test_event_schema.py` | 1 | 产出事件符合 JSON Schema 必填结构 |
| `test_voice_contract.py` | 2 | 生命周期契约;非法提交与取消被拒 |
| `test_plugin_tools.py` | 10 | pause/resume 门控、采集生命周期、启动失败回滚、合成唤醒标记、完整回合契约、传输层接线、诊断不泄漏 token、设备枚举 |
| `test_provider_adapter.py` | 2 | VoxCord 加载与 VAD 回退契约(**无 VoxCord 时 skip**) |
| `test_session_bridge.py` | 3 | token 与 loopback 强制、认证发送、缺 turn_id 判失败 |
| `test_sherpa_provider.py` | 3 | 缺模型不导入运行时、真实模型加载且静音无命中、VAD 拒静音识语音 |

### L3 集成(`tmp_proto/t10_voice_stack_validation.py`)

六项整合检查,一次运行全覆盖:

1. Windows 启动与模型加载(计时)
2. 合成音频中文唤醒命中
3. 8 轮开合资源释放(tracemalloc 监控)
4. 12 秒连续静音流(误触与 RTF)
5. 会话后端可替换(双 mock 传输行为一致)
6. 可打断 TTS(speaking 中 cancel)

### L4 端到端模拟(`scripts/e2e_simulated.py`)

链路:唤醒 → ASR 文本 → 桥接发送 → 回复 → TTS 事件 → 连续对话 → 取消 → 停止。产出 17 事件、2 次桥接发送、1 次取消。**mock 传输,无麦克风、无真实 EvoX**。

### L5 真机(部分空白)

| 脚本 | 等级 | 状态 |
|---|---|---|
| `scripts/smoke_microphone.py` | REAL-MIC | ✅ 设备开合与 VAD 管线可用 |
| `tmp_proto/live_wake.py` | REAL-MIC | ✅ 一次 `你好问问` 命中 |
| (无) 真实 EvoX 联调 | REAL-EVOX | ❌ 缺脚本与端点 |
| (无) 真机窗口验收 | REAL-WIN | ❌ 缺清单与脚本 |

## 4. 测试命令索引

| 目的 | 命令 | 等级 | 预期 |
|---|---|---|---|
| Python 全量 | `.venv\Scripts\python.exe -m pytest tests -q` | AUTO | 19 passed, 2 skipped |
| 语音冒烟 | `.venv\Scripts\python.exe scripts/smoke_voice.py` | SIM | 打印生命周期事件 |
| 端到端模拟 | `.venv\Scripts\python.exe scripts/e2e_simulated.py` | SIM | `E2E SIMULATED OK` |
| t10 栈验证 | `.venv\Scripts\python.exe tmp_proto/t10_voice_stack_validation.py` | AUTO+SIM | `t10 OK` |
| TTS→VAD→KWS | `.venv\Scripts\python.exe tmp_proto/tts_kws_vad.py` | SIM | `hit: true` |
| KWS 隔离 | `.venv\Scripts\python.exe tmp_proto/test_kws.py <wav>` | AUTO | RTF < 1,静音 clean |
| 设备枚举 | `.venv\Scripts\python.exe -c "import sounddevice as sd; print(sd.query_devices())"` | REAL-MIC | 列出输入设备 |
| 麦克风冒烟 | `.venv\Scripts\python.exe scripts/smoke_microphone.py --device 1 --duration 3` | REAL-MIC | `audio_saved: false` |
| 真机唤醒 | `.venv\Scripts\python.exe tmp_proto/live_wake.py --duration 45 --device 1` | REAL-MIC | `WAKE HIT` |
| 诊断 | `.venv\Scripts\python.exe -c "from evox_plugin import VoicePlugin; import json; print(json.dumps(VoicePlugin().diagnose(), ensure_ascii=False, indent=2))"` | AUTO | 不含 token 内容 |
| 前端构建 | `cd desktop; npm run build` | AUTO | tsc + vite 通过 |
| Rust 检查 | `cd desktop/src-tauri; cargo check` | AUTO | 零警告 |
| 渲染路线原型 | 起 http server 后驱动 `window.__SPIKE__` | SIM | 三路线 FPS 数据 |

## 5. 已验证结果汇总

### 5.1 语音链路(t10,2026-07-28)

| 检查项 | 实测值 | 判定 |
|---|---|---|
| KWS 模型加载 | 0.627 s | ✅ < 2s |
| VAD 模型加载 | 0.053 s | ✅ < 2s |
| 合成 `你好问问` 唤醒 | 命中,解码 0.026 s | ✅ |
| TTS 生成 | 0.235 s(RTF ~0.3) | ✅ < 0.5 |
| 8 轮开合内存增长 | 0.01 MB | ✅ 无泄漏 |
| 12 s 静音误触 | 0 次,RTF 0.0186 | ✅ |
| 双后端行为一致 | sent/state 相同,turn_id 不同 | ✅ |
| speaking 中打断 | `turn.cancelled`,传输层收到取消 | ✅ |

### 5.2 渲染路线(t11,2026-07-28,headless Chromium,DPR 1)

| 路线 | FPS(2s 窗口) | 判定 |
|---|---:|---|
| CSS-only(降级档) | 239.8 | ✅ 采用为降级档 |
| Canvas 2D(v1 主路径) | 239.9 | ✅ 采用为主路径 |
| WebGL shader(v2) | 240.0 | ✅ 保留为升级路径 |

附带证据:WebGL 编译链接无错;amplitude 输入 `[0,0.5,1,0.5,0]` 钳制为 `[0.12,0.5,1,0.5,0.12]`;`prefers-reduced-motion`、`hardwareConcurrency`(32)、Canvas/WebGL context、`devicePixelRatio` 均可读;零 console 错误;三路线截图各异。

### 5.3 真机麦克风(2026-07-26)

| 检查项 | 结果 |
|---|---|
| 口述 `你好问问` | 7.193 s 命中,score 1.0 |
| 音频落盘 | `audio_saved: false` |
| 资源释放 | `resources_released: true` |
| 3 s 安静采集 | RMS 0.000044,peak 0.001068,无 VAD 段(安静环境正常) |

### 5.4 安全与边界(桥接)

| 攻击面 | 防护结果 |
|---|---|
| 无 token | 拒绝(`BridgeError`) |
| 明文 HTTP 非 loopback | 拒绝 |
| `localhost.evil.example` | 拒绝(解析后判定) |
| URL 内嵌凭据 | 拒绝 |
| turn_id 路径注入 | `quote(safe="")` 编码 |
| 响应缺 turn_id | 判失败 |
| 诊断输出 | 仅 `token_configured: bool` |

## 6. 待验收矩阵(release gate)

| # | 待验收项 | 目标等级 | 需要的测试资产 | 当前 |
|---|---|---|---|---|
| 1 | 唤醒质量(安静) | REAL-MIC | 多次重复统计脚本 | ❌ 缺 |
| 2 | 唤醒质量(远场 3–5 m) | REAL-MIC | 同上 | ❌ 缺 |
| 3 | 唤醒质量(噪声/音乐) | REAL-MIC | 噪声场景清单 | ❌ 缺 |
| 4 | 误触率(长时静默/日常对话) | REAL-MIC | ≥1 h 静默跑 | ❌ 缺 |
| 5 | Silero 真机端点 | REAL-MIC | 语音起止断言脚本 | ❌ 缺 |
| 6 | EvoX 发送/增量回复 | REAL-EVOX | 真实端点 + 联调脚本 | ❌ 缺 |
| 7 | EvoX 取消/超时/重连 | REAL-EVOX | 故障注入用例 | ❌ 缺 |
| 8 | 流式首字延迟 | REAL-EVOX | 计时埋点 | ❌ 缺 |
| 9 | 透明合成(真实 WebView2) | REAL-WIN | 启动 + 目视清单 | ❌ 缺 |
| 10 | DPI 125/150/175% | REAL-WIN | 缩放切换清单 | ❌ 缺 |
| 11 | 多显示器定位 | REAL-WIN | 主副屏切换 | ❌ 缺 |
| 12 | 不抢焦点/点击穿透 | REAL-WIN | 焦点断言 | ❌ 缺 |
| 13 | 托盘常驻与退出 | REAL-WIN | 功能未实现 | ❌ 缺 |
| 14 | RDP 软件渲染降级 | REAL-WIN | 远程桌面会话 | ❌ 缺 |
| 15 | ≥30 min 资源画像 | REAL-WIN | CPU/内存/FPS 采样器 | ❌ 缺 |
| 16 | 打包产物可安装运行 | REAL-WIN | NSIS/MSI 安装验证 | ❌ 缺 |

**16 项待验收,全部空白**。这是从「原型可用」到「可发布」之间的真实距离。

## 7. 测试债务与改进建议

| 优先级 | 债务 | 建议 |
|---|---|---|
| 高 | 无 git 仓库,测试结果无法与代码版本关联 | 初始化 git,后续实测数据带 commit hash |
| 高 | 无 REAL-EVOX / REAL-WIN 任何资产 | Phase 4 前先写验收清单与脚本骨架 |
| 中 | 开发依赖与运行时依赖需保持同步 | 更新语音依赖时同步验证 `requirements-dev.txt` 的递归安装 |
| 中 | 无覆盖率统计 | 引入 `pytest-cov`,对 `core/` 设阈值 |
| 中 | `tmp_proto/` 与 `tests/` 边界模糊 | 稳定的原型脚本上升为正式测试 |
| 低 | 无 CI | 单机项目可暂缓,但建议本地 pre-commit 钩子 |
| 低 | 前端无单元测试 | Phase 4 渲染器落地后补 vitest |

## 8. 已知测试限制

1. **headless Chromium ≠ WebView2** — t11 的 FPS 与 WebGL 结论不能直接推广到真实 Tauri 窗口。
2. **合成音频 ≠ 真人语音** — TTS 回灌验证的是管线,不是唤醒质量。
3. **mock 传输 ≠ 真实 EvoX** — 证明了编排层与可替换性,不证明业务闭环。
4. **单主机验证** — 所有结论仅覆盖当前 Windows 11 + Realtek 设备组合。
5. **安静环境** — 现有麦克风测试均在安静环境,噪声鲁棒性未知。
6. **控制台中文乱码** — Windows 代码页显示问题,不是数据缺陷(UTF-8 字节正确)。
