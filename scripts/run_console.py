"""One command: the runtime, the console, and a browser pointed at it.

    .venv\\Scripts\\python.exe scripts/run_console.py            # 控制台 + 打字对话
    .venv\\Scripts\\python.exe scripts/run_console.py --voice     # 再加上麦克风

This is the entry point that answers "what do I still need to do" without a list
of commands: the page shows the readiness checklist, records a voiceprint in the
browser, edits the safe half of the config, runs a turn, and streams the events.

Security posture, because this opens a listening socket:

- The bind address is checked, not defaulted -- ``ConsoleServer`` refuses anything
  that is not loopback.
- A random token is generated per run and every request needs it, including the
  page. The URL printed below carries it; nothing else does.
- ``--no-token`` exists for preview tooling that cannot add a query string. It is
  loopback-only, it prints a warning, and ``/api/state`` reports it.
- The console never confirms ``shell.run``. That surface is the orb (FR-6.13).

Evidence level: AUTO for the wiring and the API (``tests/test_console.py``), SIM
for the rendered page. Speaking into it is REAL-MIC.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from typing import Any

from core.audio import load_voice_config
from core.audio.acks import AckLibrary, parse_acks
from core.audio.config import ACK_CACHE_DIR, orb_environment, repo_root
from core.channels.config import load_channels_config, open_weixin
from core.channels.runner import ChannelRunner
from core.console import ConsoleApi, ConsoleError, ConsoleServer
from core.env_file import load_env_file
from vox_plugin.runtime import VoiceRuntime
from vox_plugin.voice_stack import open_voice_stack

DEFAULT_PORT = 8899

#: 重启时把当前 token 交给自己的替身。换 token 会让用户手上那一页变成 401 —— 而他刚刚
#: 只是点了个「重启」，不该因此被登出。
TOKEN_ENV = "VOX_CONSOLE_TOKEN"


def make_restart(api, server, runtime, stack, stop, pump):
    """把「怎么替换这个进程」交给启动脚本，``ConsoleApi`` 只负责知道有人按了按钮。

    顺序是有讲究的：先让 HTTP 响应发出去（delay），再停麦克风和 socket，然后 ``runtime.
    close()`` —— 那一步会收掉唤醒球，**必须在 execv 之前**，否则球变成孤儿进程，新起的
    那个会在桌面上叠出第二颗。
    """

    def restart(delay_s: float) -> None:
        time.sleep(delay_s)
        if server.token:
            os.environ[TOKEN_ENV] = server.token
        stop.set()
        pump.join(timeout=2.0)
        for step in (api.mic_stop, server.stop, runtime.close, stack.close):
            try:
                step()
            except Exception:  # noqa: BLE001 - 收尸失败不能挡住重启
                pass
        os.execv(sys.executable, [sys.executable, *sys.argv])

    return restart


def _cloud_only(config: dict, key: str) -> str:
    """云端 TTS 才有意义的那几个键。本机那条路一律返回空字符串。

    为什么要这一层：这些值进的是确认音的缓存文件名。本机 VITS 没有 voice / instruction
    这两个概念，把配置里残留的值带进文件名会让已经生成好的本机缓存全部改名重生成 ——
    换 provider 不该让本机那几个文件作废。
    """
    provider = str(config.get("tts.provider", "sherpa")).strip().lower()
    if provider in ("sherpa", "local", ""):
        return ""
    return str(config.get(key, "") or "").strip()


def resolve_port(explicit: int | None) -> int:
    """``--port`` beats ``PORT`` beats the default.

    ``PORT`` is how the preview harness assigns a free port when it is told the
    server may move; honouring it is what keeps this out of the orb's 5173.
    """
    if explicit:
        return explicit
    from_env = os.getenv("PORT", "").strip()
    if from_env.isdigit():
        return int(from_env)
    return DEFAULT_PORT


def start_channels(
    runtime: VoiceRuntime, stack: Any, *, disabled: bool = False
) -> tuple[Any, Any]:
    """按 ``config/channels.toml`` 起消息通道。返回 (runner, thread)，关着时是 (None, None)。

    **默认关**，所以这个函数在出厂配置下什么都不做、也不出网。打开之后它在**自己的线程**上
    长轮询：那条链路上的 `poll` 会挂 35 秒，放在主线程里等于控制台停摆。

    ASR 与 TTS 直接借语音栈那两个 —— 微信来的语音和麦克风来的语音在识别那一层是同一件事，
    给通道单独建一份模型等于把 110 MB 的 ASR 在内存里放两份。
    """
    if disabled:
        return None, None
    try:
        config = load_channels_config()
        channel = open_weixin(config)
    except Exception as exc:  # noqa: BLE001 - 通道配置坏了不该拖死控制台
        print(f"warning: 消息通道没起来：{type(exc).__name__}: {exc}")
        return None, None
    if channel is None:
        return None, None
    status = channel.check()
    if not status.get("available"):
        print(f"weixin: 配了但用不了 —— {status.get('reason')}")
        return None, None
    runner = ChannelRunner(
        channel=channel,
        runtime=runtime,
        reply_with_voice=bool(config.get("weixin.reply_with_voice", True)),
        asr=getattr(stack, "asr", None) if config.get("weixin.local_asr", True) else None,
        tts=getattr(stack, "tts", None),
    )
    thread = threading.Thread(
        target=runner.run_forever,
        kwargs={"poll_timeout_s": float(config.get("weixin.poll_timeout_s", 35.0))},
        name="vox-weixin",
        daemon=True,
    )
    thread.start()
    print(
        f"weixin: 在听（语音回复 {'开' if runner.reply_with_voice else '关'}，"
        f"本机转写 {'开' if runner.asr is not None else '关'}，"
        f"出站语音走 {'原生气泡（未验证）' if channel.voice_native else '文件附件'}）"
    )
    return runner, thread


def pump_forever(runtime: VoiceRuntime, stop: threading.Event) -> None:
    """Run queued utterances as turns, on this thread and not the audio callback.

    Keeps running whether or not the microphone is currently open, because the
    console can start and stop it at any time. An idle loop is one blocking
    ``get`` with a timeout, which costs nothing.

    **失败必须被记下来。** 这个循环此前是 `except Exception: time.sleep(0.2)` —— 一个字都
    不留。于是「唤醒命中了但没有后文」这件事在界面上和「根本没唤醒」长得一样，而实际可能是
    每一轮都在抛异常、循环在静静地空转。一个吞掉全部异常又不留痕的循环等于把最需要的那条
    线索删掉。仍然不重抛（一轮坏掉不该结束循环），但要写进运行日志。
    """
    failures = 0
    while not stop.is_set():
        try:
            runtime.pump(timeout=0.5)
        except Exception as exc:  # noqa: BLE001 - one bad turn must not end the loop
            failures += 1
            runtime.log(
                "pump",
                f"这一轮抛了：{type(exc).__name__}: {exc}",
                level="error",
                failures=failures,
            )
            time.sleep(0.2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Vox local console")
    parser.add_argument("--port", type=int, default=None, help=f"default {DEFAULT_PORT} or $PORT")
    parser.add_argument("--host", default="127.0.0.1", help="loopback only; checked, not trusted")
    parser.add_argument("--voice", action="store_true", help="open the microphone at startup")
    parser.add_argument("--no-orb", action="store_true", help="do not spawn the wake orb")
    parser.add_argument(
        "--no-weixin",
        action="store_true",
        help="不起微信通道（即使 config/channels.toml 里开着）",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--silent", action="store_true", help="no TTS, answers stay text")
    parser.add_argument(
        "--no-token",
        action="store_true",
        help="serve without a token (loopback only, development use -- see the header)",
    )
    args = parser.parse_args()

    # 在读任何配置之前：密钥只从环境变量读，而 .env 是给这一个进程树补环境变量的地方。
    # 打印的是变量名不是值 —— 启动日志会被复制到别处去。
    loaded = load_env_file()
    if loaded:
        print(f"env: 从 .env 读到 {', '.join(loaded)}")

    config = load_voice_config()
    stack = open_voice_stack(config, with_tts=False if args.silent else None)
    for warning in stack.warnings:
        print(f"warning: {warning}")

    # 球的外观从配置文件翻译成那三个环境变量。**这三项以前只能靠人手工设** —— 控制台的
    # 「唤醒球」那一栏只会生成一行 `VOX_ORB_SIZE=140 VOX_ORB_RENDERER=bot` 让人复制到
    # 启动环境里，而一项只能这样传的配置在使用者的路径里等于不存在。
    orb_env, orb_warnings = orb_environment(config)
    for warning in orb_warnings:
        print(f"warning: {warning}")

    runtime = VoiceRuntime(
        with_desktop=bool(config["orb.enabled"]) and not args.no_orb,
        visible=bool(config["orb.visible"]),
        hide_after_s=float(config["orb.hide_after_s"]),
        # 连续对话：回答说完之后留着话筒等下一句。见 config/voice.toml 的 [wake] follow_up。
        follow_up=bool(config["wake.follow_up"]),
        orb_env=orb_env,
    )
    report = runtime.start()
    print(f"orb:    {report.desktop}")
    print(f"tools:  {', '.join(report.tools) or '(none)'}")
    print(f"agents: {', '.join(report.agents) or '(none)'}")
    print(f"memory: {report.memory}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    if stack.tts is not None:
        # 降级要能被看见。`FallbackTts` 只在**真的合成**时才知道云端不行（401 这类失败
        # `load()` 探不到），而那一刻离启动日志已经很远了 —— 所以把它接到运行日志上，
        # 控制台「只看错误」那一档就能答「为什么不出声 / 为什么换了嗓子」。
        if hasattr(stack.tts, "on_problem"):
            stack.tts.on_problem = lambda message: runtime.log(
                "tts", message, level="error", degraded=True
            )
        status = stack.tts.load()
        if status.available:
            runtime.plugin.attach_tts(stack.tts)
            # 唤醒确认音用同一个合成器,第一次启动时把它们预生成好落盘。
            acks = AckLibrary(
                parse_acks(config["wake.acks"]),
                tts=stack.tts,
                cache_dir=repo_root() / ACK_CACHE_DIR,
                # 音色与语气都进缓存文件名。不传的话换了音色/改了 instruction 文件名不变，
                # 播出来还是上一把声音、上一种腔 —— 表现是「配置改了但声音没变」。
                # 本机那条路两个都是空的，与最早的缓存同名。
                voice=_cloud_only(config, "tts.voice"),
                instruction=_cloud_only(config, "tts.instruction"),
            )
            if acks.texts:
                info = runtime.attach_acks(acks)
                print(f"acks:   {info['cached']}/{len(acks.texts)} 已就绪")
                for text, why in info["failed"].items():
                    print(f"warning: 应答音「{text}」生成失败：{why}")
        else:
            print(f"warning: tts did not load: {status.details.get('reason')}")

    api = ConsoleApi(runtime, stack)
    # 消息通道（微信）。**由配置决定，默认关** —— 打开它意味着出网，见 config/channels.toml。
    channel_runner, channel_thread = start_channels(runtime, stack, disabled=args.no_weixin)
    try:
        server = ConsoleServer(
            api,
            host=args.host,
            port=resolve_port(args.port),
            require_token=not args.no_token,
            # 重启后沿用上一轮的 token，页面不用重连。第一次启动时它是空的，
            # ConsoleServer 自己生成一个。
            token=os.environ.pop(TOKEN_ENV, ""),
        )
        url = server.start()
    except ConsoleError as exc:
        print(f"console did not start: {exc}")
        runtime.close()
        stack.close()
        return 1

    stop = threading.Event()
    # 托盘的「设置…」要打开的地址。**在这里注入而不是让运行时去拼**：它带 token，而 token
    # 是这一层生成的 —— 运行时既不知道端口也不该去猜令牌。没有它的话托盘上点「设置」只会
    # 记一条日志（见 VoiceRuntime.open_settings）。
    runtime.settings_url = url
    pump = threading.Thread(target=pump_forever, args=(runtime, stop), name="vox-pump", daemon=True)
    pump.start()
    # 装上重启入口。页面上那颗按钮是唯一让「改完的配置」生效的路径 —— 唤醒词、模型方案、
    # agent 配置都是启动时读的。
    api.restart_hook = make_restart(api, server, runtime, stack, stop, pump)

    if args.voice:
        try:
            print(api.mic_start())
        except Exception as exc:  # noqa: BLE001 - a refused gate lands here
            print(f"microphone did not start: {exc}")
            print("控制台照常可用；按提示补齐后在页面上点「启动麦克风」。")

    print("")
    print(f"console: {url}")
    if args.no_token:
        print("WARNING: 没有 token —— 本机任何进程都能读写这个控制台。只在开发时用。")
    print("Ctrl+C 停止。")
    if not args.no_browser:
        # A failure here is not a failure of the console: print the URL and go on.
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("stopping")
    finally:
        stop.set()
        if channel_runner is not None:
            channel_runner.stop()
        pump.join(timeout=2.0)
        if channel_thread is not None:
            channel_thread.join(timeout=2.0)
        server.stop()
        api.mic_stop()
        runtime.close()
        stack.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
