"""哪些配置项**还只能靠改文件** —— 把「全范围可配」从一句话变成一条命令。

    .venv\\Scripts\\python.exe scripts/audit_config_surface.py

使用者的硬要求：「web 界面应该可以对于 vox 进行全范围的配置修改并重启生效」。判据不是
「有个设置页」，而是**没有任何一项配置只能靠改文件或设环境变量才能改**。而「还差哪些」
这个问题靠记忆回答一定会漏，所以它在这里是个可以重跑的清单。

三类输出：

* **可改** —— 在 `core/console/routes.py` 的 `EDITABLE` 白名单里。
* **刻意不可改** —— 下面 `WONT` 那张表，每一条带理由。安全边界与凭据变量名在这一类里，
  它们不算缺口（放开它们等于让一个网页决定跑什么、发哪个凭据）。
* **缺口** —— 既不在白名单里、也没有理由。**这一类应该是空的。**

退出码：有缺口时 1，好让它能进 CI 或者被别的脚本判断。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audio.config import _SCHEMA as VOICE_SCHEMA  # noqa: E402
from core.channels.config import _SCHEMA as CHANNELS_SCHEMA  # noqa: E402
from core.console.routes import EDITABLE  # noqa: E402
from core.tools.policy import DEFAULTS as TOOLS_DEFAULTS  # noqa: E402

#: 刻意不放开的键 -> 理由。**每一条都要能一句话说清「为什么放开它是错的」**，
#: 否则它就是缺口而不是立场。
WONT: dict[str, str] = {
    # --- 三个模型共同的约定，不是一个旋钮 --------------------------------------
    "input.sample_rate": "16 kHz 是 KWS / ASR / 声纹三个模型共同的输入约定，改它等于换模型",
    # --- 凭据：值可以从页面存，「读哪个变量」不行 ------------------------------
    "tts.key_env": "它是「去读哪个环境变量」。让网页改它等于让它决定把哪个凭据发给百炼；"
    "密钥的**值**走 /api/secret 的白名单，那条路已经在了",
    "asr.key_env": "同 tts.key_env —— 识别 2026-09-03 也上云了，凭据变量名同样不从页面改",
    "weixin.token_env": "同 tts.key_env —— 让网页改「去读哪个环境变量」等于让它决定把哪个"
    "凭据发给腾讯。微信的 token 正常是扫码换来的，落在 .vox/channels/weixin.json",
    # --- 文件系统入口：走约定，不走网页 ----------------------------------------
    "wake.keywords_file": "它是个文件系统入口。留空时走约定路径 config/keywords.txt，"
    "而控制台的「唤醒词」那一栏写的就是那个文件 —— 手改与界面改落在同一处",
    # --- 安全边界：关掉它们该是一次打开编辑器的动作 ----------------------------
    "shell.enabled": "「说一句话就能在本机跑命令」是这个项目最大的攻击面",
    "shell.allow": "白名单只能收窄不能放宽，而一个网页能放宽它就等于没有白名单",
    "shell.require_confirmation": "确认闸门",
    "shell.require_verified_speaker": "声纹闸门",
    "shell.timeout_s": "和上面几条同一段配置，整段只读比逐项挑更难出错",
    "shell.max_output_bytes": "同上",
    "fs.enabled": "文件读取总开关，属于安全边界",
    "fs.roots": "沙箱根。一个网页能加根目录等于沙箱不存在",
    "fs.denied_names": "凭据 / 私钥 / 生物特征的文件名黑名单，只能收紧",
    "fs.denied_dirs": "enrollment（生物特征）与 memory（个人数据）就靠它挡住",
    "web.enabled": "出网总开关",
    "web.blocked_domains": "域名黑名单，只能收紧",
    "apps.entries": "「说出来的名字 → 可执行文件绝对路径」。让一个网页往里加一条"
    "等于给它代码执行 —— 这一条不打算放开。装了的应用现在靠 apps.discover 自动发现，所以这张表只剩「装在怪地方、发现不到」那种情况",
}

#: 已知缺口 -> 为什么还没做。**这一类是欠的账，不是立场**，所以它带的是计划不是理由。
#:
#: **2026-09-03 清空了。** `apps.sites` 与 `apps.play` 走 `/api/sites`（`set_section` +
#: `drop_key`，两者现在都认引号键 —— `[apps.sites]` 的键是中文）。它们能放开是因为两张表
#: 都只产出一个**浏览器要打开的地址**，而 `web.open` 已经允许任何请求打开一个地址，
#: 所以放开不增加任何能力。`apps.play` 只收 `http(s)` 模板：把 URI 当 argv 传给已装的 exe
#: 是给一个白名单里的程序加参数（想想 `--load-extension`），那一种留在文件里。
KNOWN_GAPS: dict[str, str] = {}

#: 有**专用编辑入口**的键 -> 那个入口。第三类，既不是标量白名单也不是缺口。
#:
#: 存在的理由：`EDITABLE` 是给 `set_scalars` 用的白名单，而表（`[apps.sites]` 这种）写不进
#: 标量白名单里。只看 `EDITABLE` 的审计会把「已经有专门界面能改」误报成缺口，
#: 而一个会误报的审计脚本很快就没人看了。
VIA_ENDPOINT: dict[str, str] = {
    "apps.sites": "/api/sites（表编辑器；只收 http(s)，因为它只产出一个要打开的地址）",
    "apps.play": "/api/sites（同上，模板必须带 {q}）",
}


def voice_keys() -> list[str]:
    return [f"{section}.{key}" for section, keys in VOICE_SCHEMA.items() for key in keys]


def tools_keys() -> list[str]:
    return [
        f"{section}.{key}"
        for section, body in TOOLS_DEFAULTS.items()
        if isinstance(body, dict)
        for key in body
    ]


def channels_keys() -> list[str]:
    """``config/channels.toml`` 的全部键。

    **2026-09-04 加进来的，而它一进来就报出五个缺口。** 那五个里有 `weixin.enabled` ——
    「绑定完了为什么还不收微信消息」的那个开关。这份审计此前只看 voice.toml 与
    tools.toml，所以一个整份文件的缺口它一次都没报过：一个只审两个文件的「全范围可配」
    审计，漏掉的正好是它该发现的东西。
    """
    return [
        f"{section}.{key}" for section, keys in CHANNELS_SCHEMA.items() for key in keys
    ]


def missing_from_shipped() -> list[str]:
    """在白名单里、但**出厂文件里没写**的键。

    控制台只显示文件里出现过的键（`editable_keys` 读的是文件），所以一个只存在于代码
    默认值里的键在页面上**不可见** —— 而不可见等于改不了，那正是「可改」这件事上最容易
    漏掉的一半。修法通常是把这一行写进出厂配置文件（文件本身也是文档）。
    """
    from core.console.routes import ConsoleApi

    api = ConsoleApi(runtime=None, stack=None)
    shown: dict[str, set[str]] = {}
    for entry in api.config_view()["files"]:
        shown[entry["file"]] = {key["key"] for key in entry.get("keys", ())}
    gaps: list[str] = []
    for file, allowed in EDITABLE.items():
        if file not in shown:
            continue
        for key in allowed:
            if key not in shown[file]:
                gaps.append(f"{file}:{key}")
    return sorted(gaps)


def audit() -> int:
    surfaces = {
        "voice.toml": voice_keys(),
        "tools.toml": tools_keys(),
        "channels.toml": channels_keys(),
    }
    gaps: list[str] = []
    for file, keys in surfaces.items():
        editable = set(EDITABLE.get(file, ()))
        covered = editable | set(VIA_ENDPOINT)
        print(f"\n=== {file} ===")
        print(f"  可改        {len(covered & set(keys))} / {len(keys)}")
        deliberate = [key for key in keys if key not in covered and key in WONT]
        known = [key for key in keys if key not in covered and key in KNOWN_GAPS]
        endpoints = [key for key in keys if key not in editable and key in VIA_ENDPOINT]
        missing = [
            key
            for key in keys
            if key not in covered and key not in WONT and key not in KNOWN_GAPS
        ]
        if deliberate:
            print("  刻意不可改：")
            for key in sorted(deliberate):
                print(f"    - {key}：{WONT[key]}")
        if endpoints:
            print("  有专用编辑入口：")
            for key in sorted(endpoints):
                print(f"    - {key}：{VIA_ENDPOINT[key]}")
        if known:
            print("  已知缺口（欠的账）：")
            for key in sorted(known):
                print(f"    - {key}：{KNOWN_GAPS[key]}")
        if missing:
            print("  **没有理由的缺口**：")
            for key in sorted(missing):
                print(f"    - {key}")
            gaps.extend(f"{file}:{key}" for key in missing)

    invisible = missing_from_shipped()
    if invisible:
        print("\n--- **在白名单里但出厂文件没写，所以页面上看不见** ---")
        for entry in invisible:
            print(f"  {entry}")
        gaps.extend(invisible)

    print("\n--- 不在上面这几张表里的配置面（各有自己的编辑入口）---")
    for line in (
        "config/models.toml   模型方案：/api/models（整段读写，含 provider / model / voice / key_env 变量名）",
        "config/agents.toml   agent 注册表：/api/agents/config（AGENT_EDITABLE 那几个字段）",
        "config/memory.toml   记忆：EDITABLE 里两项",
        "config/speaker.toml  声纹阈值：EDITABLE 里十项；档案本身走 /api/speaker/*",
        "config/keywords.txt  唤醒词：/api/wake（拼音行由后端生成，不让人手写音素）",
        ".env                 凭据的**值**：/api/secrets（allowed_secret_names 白名单）",
    ):
        print(f"  {line}")

    if gaps:
        print(f"\n还有 {len(gaps)} 项既不可改也没有理由 —— 那是缺口：{', '.join(gaps)}")
        return 1
    print("\n没有「既不可改也没理由」的键。")
    return 0


if __name__ == "__main__":
    sys.exit(audit())
