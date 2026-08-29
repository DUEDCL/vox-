# 模型分发策略

> 状态：决策稿（2026-08-28）
> 关闭的是发布阻塞项 **#11 的文档部分**。真实的打包与下载验证仍未做。
> 证据等级：**DOC**（体积是本机实测，分发链路一条都没跑过）。

## 1. 现在有什么（本机实测 2026-08-28）

| 内容 | 体积 | 必需 | 说明 |
|---|---:|---|---|
| `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/` | 36 MB | **是** | 中文唤醒（KWS） |
| `sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23/` | 78 MB | 是 | 流式识别（ASR）。关掉 `[asr] enabled` 可跑「只唤醒」模式 |
| `vits-melo-tts-zh_en/` | 183 MB | 否 | 语音合成（TTS）。缺它回合照常走完，只是不出声 |
| `3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx` | 38 MB | **是** | 声纹准入。缺它 `capture.start()` fail-closed 拒绝启动 |
| `silero_vad.onnx` | 2.3 MB | 否 | 端点检测。ASR 自带端点检测，这个是备选路径 |
| `kws.tar.bz2` `asr.tar.bz2` `tts.tar.bz2` | 261 MB | **否** | **可删归档**，解压完就没用了 |
| 合计 | **597 MB** | | 删掉三个归档后 **336 MB** |

「必需」的判据是 fail-closed：唤醒模型和声纹模型缺失会让平台**拒绝启动麦克风**
（那是设计，不是缺陷）；ASR 与 TTS 缺失只降级并如实报告，控制台的就绪清单会逐项说
缺什么、怎么补。

## 2. 决策

### 2.1 模型不进版本库，`models/` 保持 gitignore

已经如此，不改。理由不是体积习惯，是**许可证与来源可核验性**：模型权重的分发条款
与代码不同（见 `THIRD_PARTY_NOTICES.md`），把它们塞进 git 历史意味着这个仓库的每一份
拷贝都在再分发那些权重，而 git 历史里的东西删不掉。

### 2.2 三个 `.tar.bz2` 归档删掉，不进任何发布物

261 MB 里没有一个字节是运行时需要的 —— 它们是解压残留。删除动作留给使用者（不由脚本
自动删：一台网络受限的机器上，重新下载可能比留着 261 MB 贵）。

### 2.3 路径走环境变量，不进配置文件

`config/voice.toml` 里**没有模型路径**，四个位置由环境变量给，默认落在 `models/` 下的
固定目录名：

```
VOX_KWS_MODEL_DIR   VOX_ASR_MODEL_DIR   VOX_TTS_MODEL_DIR   VOX_VAD_MODEL
```

理由与 `config/speaker.toml` 的 `enrollment/` 路径同款：一个进版本控制的配置文件不该
记录某台机器的磁盘布局。副作用是**多个 Vox 实例可以共用一份模型**，这在同机测试时省
的是几百兆。

### 2.4 分发形态：三档，按使用者的网络与耐心分

| 档 | 内容 | 体积 | 适用 |
|---|---|---:|---|
| **代码档** | 仓库本身，零模型 | < 5 MB | 已有模型、或先看代码 |
| **最小可用档** | KWS + 声纹 | 74 MB | 只要唤醒 + 准入；ASR/TTS 后补 |
| **完整档** | 全部四项，无归档 | 336 MB | 开箱说话 |

「最小可用档」是这一版新增的可能性：`open_voice_stack` 的逐项降级让缺 ASR/TTS 变成
一个**被如实报告的降级**而不是启动失败，所以 74 MB 是一个真的能跑起来的档位。

### 2.5 获取方式：按需下载脚本（**未实现**）

形状已定，代码未写：

```
scripts/fetch_models.py --which kws,speaker      # 最小可用
scripts/fetch_models.py --which all             # 完整
scripts/fetch_models.py --verify                # 只校验已有文件的 SHA-256
```

三条硬约束（写在这里，等实现时对照）：

1. **每个文件都必须有 SHA-256 并在下载后校验**。声纹模型的摘要已经在
   `THIRD_PARTY_NOTICES.md` 里，其余三个要在实现这个脚本时补齐 —— 一个不校验摘要的
   模型下载器是一条供应链攻击路径。
2. **URL 必须是上游官方地址**（sherpa-onnx 的 GitHub release / ModelScope），不设镜像
   默认值。要用镜像就显式传 `--base-url`，那是使用者的决定。
3. **默认不自动跑**。安装时静默下载 336 MB 是一种既慢又不透明的行为；控制台的就绪清单
   已经会指出缺什么，让人自己决定什么时候补。

## 3. 打包（未实现，形状已定）

- **不做单文件 exe。** PyInstaller 打进 sherpa-onnx 的原生库 + 336 MB 权重会得到一个
  几百兆的产物，而它的更新代价是「整包重下」。模型与代码的更新节奏差一个数量级，绑在
  一起是错的。
- **形态是「代码 + 一条 fetch 命令」**：解压代码档 → `pip install -r requirements-voice.txt`
  → `fetch_models.py --which kws,speaker` → `run_console.py`。控制台负责把剩下的缺口
  逐项说清楚。
- Tauri 侧的唤醒球单独构建，不含模型。

## 4. 剩余风险

- **SenseVoiceSmall 权重许可证未取证**（备选 ASR）。启用前必须先归档 ModelScope 的
  许可证文本，否则不进任何发布物。
- **MeloTTS 的 183 MB 是全部体积的一半以上**。若要压，可选路径是 Kokoro-82M（ADR 001
  的备选），但那要重跑一遍 TTS 的真机验收。
- **本文档全部是 DOC 级**：三档分发一条都没打包过，`fetch_models.py` 不存在。这一项
  在发布阻塞项里应记为「文档部分已关闭，实现与验证未做」。
