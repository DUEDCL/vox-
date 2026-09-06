"""扫码登录：微信通道的凭据是**扫出来的**，不是填进去的。

使用者的问题：「为什么 hermes 的微信渠道是扫码绑定的，你这次做的却是填写 token？」——
问得对。上一版要求先把一个 iLink bot token 塞进环境变量，可那个 token **本来就是扫码换
来的**：没有扫码这一步的话，一个正常使用者根本无处可取。

## 三步，抄自 Hermes 的实现

1. ``GET ilink/bot/get_bot_qrcode?bot_type=3`` → ``{qrcode, qrcode_img_content}``。
   前者是十六进制的轮询票据，后者是**要被扫的那个完整 URL**（liteapp 链接）。
   微信扫的是后者 —— 把 hex 直接编成二维码扫不出东西来。
2. 轮询 ``GET ilink/bot/get_qrcode_status?qrcode=<hex>``，``status`` 五个值：
   ``wait`` / ``scaned``（扫了还没确认）/ ``scaned_but_redirect``（**换域名**，
   ``redirect_host`` 里给新的）/ ``expired``（要重新取，上游给三次机会）/ ``confirmed``。
3. ``confirmed`` 时凭据在同一份响应里：``ilink_bot_id`` / ``bot_token`` / ``baseurl`` /
   ``ilink_user_id``。

``scaned_but_redirect`` 那一条不是可选的：它换掉的是**后续所有请求**的 base_url。忽略它
的症状是「扫完了，然后一直卡在 wait」。

## 凭据存在哪，为什么不是环境变量

存 ``.vox/channels/weixin.json``（gitignored，和 ``.vox/acks`` 同一个目录的规矩）。

这和「密钥只从环境变量读」不矛盾，而是那条规矩的边界：环境变量适合**人手上已经有**的
凭据（百炼的 key、中转站的 token），因为那样仓库里不留任何值。扫码换来的 token 不在人
手上 —— 它是程序在运行时拿到的，要求人再手抄进 ``.env`` 只会让人抄错，而且下一次过期
又要抄一遍。所以它落盘，权限收到 0600（Windows 上 ``chmod`` 只有读写位有意义，做了但
不指望它是安全边界）。

``token_env`` 那条路**没有被删**：环境变量里有值就优先用它。自动化和「我自己已经有一个
token」的情形仍然走那条。

安全姿态两条，都在代码里：

* 这份文件里**只有** bot token 与 account id，没有别的凭据；``describe()`` 报的是
  account id 的前 8 位与 base_url，**永不回显 token**。
* 二维码那个 URL 会被渲染成 SVG 交给控制台（``segno``，纯 Python）。**不引外链** ——
  一个从 CDN 拉 JS 去渲二维码的登录页，等于把登录流程交给第三方。

证据等级：AUTO（协议形状抄自本机 Hermes 源码 + 假 transport 的往返）。真的扫一次是
REAL-WEIXIN。
"""

from __future__ import annotations

import io
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from core.channels.contract import ChannelError
from core.channels.weixin import (
    API_TIMEOUT_MS,
    CHANNEL_VERSION,
    ILINK_APP_CLIENT_VERSION,
    ILINK_APP_ID,
    ILINK_BASE_URL,
    HttpTransport,
    _random_wechat_uin,
)

#: 取二维码与查状态。两个都是 GET，都不带 Authorization —— 此刻还没有凭据。
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

#: ``bot_type=3`` 是 Hermes 用的值（个人号 bot）。抄下来而不是猜。
DEFAULT_BOT_TYPE = "3"

QR_TIMEOUT_MS = 35_000

#: 二维码过期后重新取的次数上限，和上游一致。超过就让人重新点一次「扫码登录」——
#: 无限刷新会让一个已经放弃的登录页一直在打微信的接口。
MAX_REFRESH = 3

#: 凭据落盘的位置（相对仓库根）。gitignored。
CREDENTIAL_PATH = ".vox/channels/weixin.json"

#: 五个状态里除了这两个都还要继续等。
TERMINAL = ("confirmed", "expired")


def credential_path() -> Path:
    """凭据文件的绝对路径。``VOX_WEIXIN_CREDENTIALS`` 可改 —— 测试要它。"""
    override = os.getenv("VOX_WEIXIN_CREDENTIALS", "").strip()
    if override:
        return Path(override)
    from core.audio.config import repo_root

    return repo_root() / CREDENTIAL_PATH


def save_credentials(payload: Mapping[str, Any]) -> Path:
    """把扫码换来的凭据写下去。目录不存在就建，权限收到 0600。"""
    path = credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "account_id": str(payload.get("ilink_bot_id") or payload.get("account_id") or ""),
        "token": str(payload.get("bot_token") or payload.get("token") or ""),
        "base_url": str(payload.get("baseurl") or payload.get("base_url") or ILINK_BASE_URL),
        "user_id": str(payload.get("ilink_user_id") or payload.get("user_id") or ""),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if not body["account_id"] or not body["token"]:
        raise ChannelError("扫码回来的凭据不完整（缺 account_id 或 token）—— 没有存")
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        # Windows 上这一步基本没有效果。做了不指望它是安全边界 —— 边界是这台机器本身。
        pass
    return path


def load_credentials() -> dict[str, str] | None:
    """读回凭据，没有或读不动都返回 None（那等于「还没扫过」）。"""
    path = credential_path()
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, Mapping) or not body.get("token"):
        return None
    return {str(key): str(value) for key, value in body.items()}


def forget_credentials() -> bool:
    """删掉凭据（「解绑」）。返回是否真的删掉了一个文件。"""
    path = credential_path()
    if not path.is_file():
        return False
    path.unlink()
    return True


def describe_credentials() -> dict[str, Any]:
    """给控制台看的状态。**永不回显 token。**"""
    body = load_credentials()
    if body is None:
        return {"bound": False, "account_id": "", "base_url": "", "saved_at": ""}
    account = body.get("account_id", "")
    return {
        "bound": True,
        # 只报前 8 位：一个完整的 account id 在截图里就是一个可定位的标识符。
        "account_id": account[:8] + ("…" if len(account) > 8 else ""),
        "base_url": body.get("base_url", ""),
        "saved_at": body.get("saved_at", ""),
        "token_chars": len(body.get("token", "")),
    }


def qr_svg(data: str, *, scale: int = 4, border: int = 2) -> str:
    """把要被扫的那个 URL 渲成 SVG 字符串。

    用 ``segno``（纯 Python，无 C 扩展、无联网）。渲在**服务端**而不是让页面去 CDN 拉一个
    JS 二维码库：一个从第三方拉代码的登录页等于把登录流程交给第三方，而这一页上过的是
    使用者的微信账号。

    ``segno`` 缺失时抛 ``ChannelError`` 而不是静默返回空串 —— 一个空白的二维码框会被读成
    「接口坏了」，而真相是少一个包。
    """
    try:
        import segno  # noqa: PLC0415 - 只在真要渲二维码时才导
    except Exception as exc:  # noqa: BLE001
        raise ChannelError(
            "渲二维码要 segno（纯 Python）：.venv\\Scripts\\python.exe -m pip install segno"
        ) from exc
    buffer = io.BytesIO()
    segno.make(data, error="m").save(buffer, kind="svg", scale=scale, border=border)
    return buffer.getvalue().decode("utf-8")


@dataclass
class QrLogin:
    """一次扫码登录的会话。

    做成对象而不是一个阻塞函数，因为控制台要的是**两个端点**：一个开始（拿二维码），
    一个轮询（问状态）。Hermes 那份是 CLI，所以它可以在一个 ``while`` 里边打点边等；
    网页不行 —— 一个 8 分钟不返回的 HTTP 请求会被任何一层超时掐断。
    """

    transport: Any = field(default_factory=HttpTransport)
    base_url: str = ILINK_BASE_URL
    bot_type: str = DEFAULT_BOT_TYPE
    #: 当前的轮询票据（十六进制）。
    qrcode: str = ""
    #: 要被扫的那个完整 URL。
    scan_url: str = ""
    refreshes: int = 0
    #: 最近一次看到的状态，给页面显示用。
    status: str = ""
    started_at: float = 0.0

    def _headers(self) -> dict[str, str]:
        # 没有 Authorization：此刻还没有凭据。其余头和正式请求一致 —— 少一个
        # iLink-App-Id 就会被当成未知客户端。
        return {
            "Content-Type": "application/json",
            "X-WECHAT-UIN": _random_wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }

    def _get(self, endpoint: str) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/{endpoint}"
        try:
            raw = self.transport.get_json(url, self._headers(), QR_TIMEOUT_MS / 1000)
        except Exception as exc:  # noqa: BLE001 - 网络失败要带上是哪一步
            raise ChannelError(f"{endpoint} 失败：{type(exc).__name__}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ChannelError(f"{endpoint} 回了一个不是 JSON 对象的东西")
        return dict(raw)

    def begin(self) -> dict[str, Any]:
        """取一张二维码，从头开始一次登录。返回给页面的是 SVG，不是 URL。"""
        self.refreshes = 0
        return self._fetch()

    def _fetch(self) -> dict[str, Any]:
        """取一张二维码，**不动重试计数**。

        和 ``begin`` 分开是因为过期重取也要走这里，而那时计数正是要保住的东西 ——
        合成一个函数的后果是 ``refreshes`` 每次刷新都被清零，于是「最多刷三次」变成
        「永远刷下去」，而一个已经被放弃的登录页会一直打微信的接口。
        """
        response = self._get(f"{EP_GET_BOT_QR}?bot_type={self.bot_type}")
        self.qrcode = str(response.get("qrcode") or "")
        # `qrcode_img_content` 才是要被扫的东西。缺了它才退回 hex —— 而那种情况下大概扫
        # 不出来，所以 `scan_is_url` 一起报给页面，好让「扫不出来」有个读数而不是猜。
        self.scan_url = str(response.get("qrcode_img_content") or "")
        if not self.qrcode:
            raise ChannelError("取二维码成功了但响应里没有 qrcode 字段")
        self.status = "wait"
        self.started_at = time.monotonic()
        return self._payload()

    def _payload(self) -> dict[str, Any]:
        data = self.scan_url or self.qrcode
        return {
            "status": self.status,
            "svg": qr_svg(data),
            "scan_is_url": bool(self.scan_url),
            "refreshes": self.refreshes,
            "waited_s": round(max(0.0, time.monotonic() - self.started_at), 1),
        }

    def poll(self) -> dict[str, Any]:
        """问一次状态。**一次一问**，等待留给页面。

        返回 ``{"status": ..., ...}``。``confirmed`` 时带 ``credentials``（已落盘）；
        ``expired`` 时自动换一张新的并把新 SVG 带回来，换够 ``MAX_REFRESH`` 次就如实报错。
        """
        if not self.qrcode:
            raise ChannelError("还没开始扫码登录（先调 begin）")
        response = self._get(f"{EP_GET_QR_STATUS}?qrcode={self.qrcode}")
        status = str(response.get("status") or "wait")
        self.status = status

        if status == "scaned_but_redirect":
            # **这一条不是可选的。** 它换掉后续所有请求的 base_url；忽略它的症状是
            # 「扫完了然后一直卡在 wait」。
            host = str(response.get("redirect_host") or "").strip()
            if host:
                self.base_url = f"https://{host}"
            return {**self._payload(), "status": "scaned"}

        if status == "expired":
            if self.refreshes >= MAX_REFRESH:
                raise ChannelError(f"二维码连续过期 {MAX_REFRESH} 次 —— 重新点一次扫码登录")
            self.refreshes += 1
            self._fetch()
            return {**self._payload(), "status": "refreshed"}

        if status == "confirmed":
            saved = save_credentials(response)
            return {
                **self._payload(),
                "status": "confirmed",
                "credentials": describe_credentials(),
                "path": str(saved),
            }

        return self._payload()


def login_blocking(
    *,
    transport: Any = None,
    poll_interval_s: float = 1.0,
    timeout_s: float = 480.0,
    sleep: Callable[[float], None] | None = None,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """命令行那条路：取码、打印、轮询到确认。

    存在的理由不是「也要有个 CLI」，而是**控制台起不来的时候仍然能绑定**。它和
    ``QrLogin`` 共用同一份状态机，所以两条路不会各自漂移。
    """
    session = QrLogin(transport=transport or HttpTransport())
    session.begin()
    waiter = sleep or time.sleep
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = session.poll()
        if on_status is not None:
            on_status(str(result.get("status", "")))
        if result.get("status") == "confirmed":
            return result
        waiter(poll_interval_s)
    raise ChannelError(f"扫码登录超时（{timeout_s:g} 秒没人扫）")


__all__ = [
    "CREDENTIAL_PATH",
    "EP_GET_BOT_QR",
    "EP_GET_QR_STATUS",
    "MAX_REFRESH",
    "QrLogin",
    "credential_path",
    "describe_credentials",
    "forget_credentials",
    "load_credentials",
    "login_blocking",
    "qr_svg",
    "save_credentials",
]
