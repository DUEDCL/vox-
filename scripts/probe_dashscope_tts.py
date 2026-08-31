"""探针：百炼 TTS 的哪个「模型 + 音色」组合在你的额度下真能用。

## 为什么需要探

读文档定不了这件事，有一条已核实的矛盾：

- 非实时 HTTP 接口文档列出的合法 `model` 是 `qwen-audio-3.0-tts-plus/flash`、
  `cosyvoice-v3.5-plus/flash`、`cosyvoice-v3-plus/flash`、`cosyvoice-v2` ——
  **没有 `cosyvoice-v1`**。
- 而免费额度那一页上是 `cosyvoice-v1`（10K 字符、永不过期）。
- 而 `longyuan`（龙媛）是**v1** 的音色名；文档另有 `longyuan_v2` / `longyuan_v3`。

所以「v1 + longyuan」可能只走 WebSocket，也可能要在 v2 上换成 `longyuan_v2`。
一次真实请求就能定，猜不行。

## 用法

先把密钥放进 `.env`（变量名默认 `VOX_DASHSCOPE_KEY`）：

    VOX_DASHSCOPE_KEY=sk-...

然后：

    .venv\\Scripts\\python.exe scripts\\probe_dashscope_tts.py

它对每个组合发**一次**最短的合成请求（4 个汉字），成功的会报耗时与音频长度。
计费按字符算，所以整轮探测大约花掉几十个字符的额度。

密钥不回显，失败只印状态码与服务端消息。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audio.config import load_voice_config
from core.env_file import load_env_file

load_env_file()

from core.audio.tts_cloud import DashScopeTtsProvider

#: 要试的组合。顺序 = 优先希望它成的顺序。
#:
#: 前两条直接回答使用者的要求（免费额度那个模型 + 点名的音色）；后面几条是退路，
#: 每一条都要说明它是什么退路，否则一张全红的表看不出下一步该做什么。
COMBOS: tuple[tuple[str, str, str], ...] = (
    ("cosyvoice-v1", "longyuan", "免费额度那个模型 + 你点名的音色（文档没把 v1 列进 HTTP 合法值，所以这条是重点）"),
    ("cosyvoice-v2", "longyuan", "v2 上用 v1 的音色名"),
    ("cosyvoice-v2", "longyuan_v2", "v2 上用带版本后缀的音色名"),
    ("cosyvoice-v2", "longxiaochun", "v2 + 一个最常见的音色（用来分辨「模型不行」还是「音色不行」）"),
    ("cosyvoice-v3-flash", "longyuan_v3", "v3 flash + v3 音色名"),
    ("qwen-audio-3.0-tts-flash", "longyuan", "Qwen-Audio-TTS 那一支"),
)

TEXT = "你好小沃"


def main() -> int:
    config = load_voice_config()
    key_env = str(config.get("tts.key_env", "VOX_DASHSCOPE_KEY")) or "VOX_DASHSCOPE_KEY"
    import os

    if not os.getenv(key_env, "").strip():
        print(f"{key_env} 没有值。把百炼的 key 写进 .env 的这个变量，或在控制台「密钥」那栏存进去。")
        return 1

    print(f"密钥变量：{key_env}（有值）   试听文本：{TEXT!r}（{len(TEXT)} 字）")
    print("=" * 78)
    winners: list[tuple[str, str]] = []
    for model, voice, why in COMBOS:
        provider = DashScopeTtsProvider(model=model, voice=voice, key_env=key_env)
        label = f"{model} + {voice}"
        try:
            audio = provider.synthesize(TEXT)
        except Exception as exc:  # noqa: BLE001 - 探针要把每种失败都显示出来
            message = str(exc)
            print(f"  ✗ {label:42} {type(exc).__name__}")
            print(f"      {message[:170]}")
            print(f"      （这条是：{why}）")
            continue
        seconds = len(audio.samples) / max(1, audio.sample_rate)
        print(f"  ✓ {label:42} {audio.elapsed_ms:5} ms  {seconds:.2f}s  {audio.sample_rate} Hz")
        print(f"      （这条是：{why}）")
        winners.append((model, voice))

    print("=" * 78)
    if not winners:
        print("一个都没通。按可能性：① key 不对或不是百炼的 key；② 额度所在地域不是华北2（北京）；")
        print("③ 这个账号的这些模型没开通。控制台「密钥」那栏重存一次 key 再试。")
        return 1
    model, voice = winners[0]
    print(f"能用的组合 {len(winners)} 个，第一个是 {model} + {voice}。把它填进 config/voice.toml：")
    print()
    print("    [tts]")
    print('    provider = "dashscope"')
    print(f'    model = "{model}"')
    print(f'    voice = "{voice}"')
    print()
    print("然后重启控制台（改 [tts] 要重启才生效）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
