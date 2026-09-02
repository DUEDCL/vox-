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
    "等于给它代码执行 —— 这一条不打算放开",
}

#: 已知缺口 -> 为什么还没做。**这一类是欠的账，不是立场**，所以它带的是计划不是理由。
KNOWN_GAPS: dict[str, str] = {
    "apps.sites": "是表不是标量，set_scalars 写不了 —— 要一个和 agents / mcp 同款的表编辑器",
    "apps.play": "同上（每个应用一个「带词打开」模板）",
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
    surfaces = {"voice.toml": voice_keys(), "tools.toml": tools_keys()}
    gaps: list[str] = []
    for file, keys in surfaces.items():
        editable = set(EDITABLE.get(file, ()))
        print(f"\n=== {file} ===")
        print(f"  可改        {len(editable & set(keys))} / {len(keys)}")
        deliberate = [key for key in keys if key not in editable and key in WONT]
        known = [key for key in keys if key not in editable and key in KNOWN_GAPS]
        missing = [
            key for key in keys if key not in editable and key not in WONT and key not in KNOWN_GAPS
        ]
        if deliberate:
            print("  刻意不可改：")
            for key in sorted(deliberate):
                print(f"    - {key}：{WONT[key]}")
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

    print("\n--- 不在这两张表里的配置面（各有自己的编辑入口）---")
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
