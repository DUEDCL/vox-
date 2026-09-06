"""扫码登录：凭据是扫出来的，不是填进去的。

使用者的问题是这个文件存在的理由：「为什么 hermes 的微信渠道是扫码绑定的，你这次做的
却是填写 token？」—— 那个 token 本来就是扫码换来的，没有扫码这一步普通使用者无处可取。

协议形状抄自本机的 Hermes 源码（`gateway/platforms/weixin.py`）。这里钉的是**状态机**：
五个状态、重定向、过期重取、以及「凭据落盘但永不回显」。

证据等级：AUTO（假 transport，不打网络）。真的扫一次是 REAL-WEIXIN。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.channels.contract import ChannelError
from core.channels.weixin_login import (
    MAX_REFRESH,
    QrLogin,
    describe_credentials,
    forget_credentials,
    load_credentials,
    login_blocking,
    qr_svg,
)

QR = {"qrcode": "abc123", "qrcode_img_content": "https://ilinkai.weixin.qq.com/liteapp?t=abc"}
CONFIRMED = {
    "status": "confirmed",
    "ilink_bot_id": "bot-777888999",
    "bot_token": "tk-" + "x" * 32,
    "baseurl": "https://sh.ilinkai.weixin.qq.com",
    "ilink_user_id": "u-1",
}


class FakeTransport:
    def __init__(self, script) -> None:
        self.script = list(script)
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def get_json(self, url, headers, timeout_s):
        del timeout_s
        self.urls.append(url)
        self.headers.append(dict(headers))
        if not self.script:
            raise AssertionError(f"多问了一次：{url}")
        return self.script.pop(0)


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    """凭据落到 tmp。不隔离的话跑一次测试就会覆盖使用者真的绑定。"""
    monkeypatch.setenv("VOX_WEIXIN_CREDENTIALS", str(tmp_path / "weixin.json"))


def test_begin_returns_a_rendered_qr_not_a_url():
    """页面拿到的是 SVG，不是让它自己去渲的字符串。

    在服务端渲是刻意的：一个从 CDN 拉 JS 二维码库的登录页，等于把登录流程交给第三方，
    而这一页上过的是使用者的微信账号。
    """
    transport = FakeTransport([QR])
    session = QrLogin(transport=transport)

    payload = session.begin()

    assert payload["svg"].startswith("<?xml") and "svg" in payload["svg"]
    assert payload["scan_is_url"] is True
    assert payload["status"] == "wait"
    assert "bot_type=3" in transport.urls[0]


def test_the_scan_target_is_the_url_not_the_hex_ticket():
    """微信扫的是 ``qrcode_img_content`` 那个完整 URL。把 hex 票据编成二维码扫不出东西 ——
    而「扫不出来」这个症状不会有任何报错。"""
    transport = FakeTransport([QR])
    session = QrLogin(transport=transport)
    session.begin()

    assert session.scan_url == QR["qrcode_img_content"]
    # 轮询用的仍然是 hex 票据，两者不能混。
    assert session.qrcode == "abc123"


def test_no_credential_is_sent_while_logging_in():
    """此刻还没有凭据。带一个上去只可能是带错了别人的。"""
    transport = FakeTransport([QR])
    QrLogin(transport=transport).begin()

    assert "Authorization" not in transport.headers[0]
    # 其余头要在：少一个 iLink-App-Id 会被当成未知客户端。
    assert transport.headers[0]["iLink-App-Id"] == "bot"


def test_a_redirect_switches_the_base_url_for_every_later_request():
    """``scaned_but_redirect`` **不是可选的**：它换掉后续所有请求的 base_url。
    忽略它的症状是「扫完了然后一直卡在 wait」—— 因为还在问旧域名。"""
    transport = FakeTransport([
        QR,
        {"status": "scaned_but_redirect", "redirect_host": "sh.ilinkai.weixin.qq.com"},
        {"status": "wait"},
    ])
    session = QrLogin(transport=transport)
    session.begin()

    result = session.poll()

    assert result["status"] == "scaned", "重定向对页面来说就是「扫到了」"
    assert session.base_url == "https://sh.ilinkai.weixin.qq.com"
    session.poll()
    assert "sh.ilinkai" in transport.urls[-1], "重定向之后还在问旧域名"


def test_an_expired_code_is_refreshed_and_the_page_gets_the_new_one():
    transport = FakeTransport([QR, {"status": "expired"}, dict(QR, qrcode="def456")])
    session = QrLogin(transport=transport)
    session.begin()

    result = session.poll()

    assert result["status"] == "refreshed"
    assert session.qrcode == "def456"
    assert session.refreshes == 1
    assert result["svg"], "换了码但没把新的二维码给页面"


def test_refreshing_forever_is_refused():
    """一个已经被放弃的登录页不该一直打微信的接口。"""
    script = [QR]
    for _ in range(MAX_REFRESH):
        script.extend([{"status": "expired"}, QR])
    script.append({"status": "expired"})
    session = QrLogin(transport=FakeTransport(script))
    session.begin()
    for _ in range(MAX_REFRESH):
        session.poll()

    with pytest.raises(ChannelError, match="连续过期"):
        session.poll()


def test_confirmed_saves_the_credential_and_never_echoes_the_token():
    transport = FakeTransport([QR, CONFIRMED])
    session = QrLogin(transport=transport)
    session.begin()

    result = session.poll()

    assert result["status"] == "confirmed"
    # **token 的值绝不能出现在返回给页面的任何地方。** 这一页会被截图。
    assert CONFIRMED["bot_token"] not in json.dumps(result, ensure_ascii=False)
    assert result["credentials"]["bound"] is True
    assert result["credentials"]["account_id"] == "bot-7778…", "account id 只报前 8 位"
    assert result["credentials"]["token_chars"] == len(CONFIRMED["bot_token"])
    # 落盘的那份是完整的 —— 通道要用它。
    assert load_credentials()["token"] == CONFIRMED["bot_token"]


def test_an_incomplete_confirmation_is_not_saved():
    """确认了但凭据不全 —— 存一个半截的比不存更糟：通道会带着一个空 token 去打接口，
    而那报回来的是 401，看起来像「token 无效」而不是「根本没绑上」。"""
    session = QrLogin(transport=FakeTransport([QR, {"status": "confirmed", "bot_token": ""}]))
    session.begin()

    with pytest.raises(ChannelError, match="不完整"):
        session.poll()
    assert load_credentials() is None


def test_polling_before_begin_is_refused():
    with pytest.raises(ChannelError, match="还没开始"):
        QrLogin(transport=FakeTransport([])).poll()


def test_describe_says_not_bound_before_anyone_scans():
    assert describe_credentials() == {
        "bound": False,
        "account_id": "",
        "base_url": "",
        "saved_at": "",
    }


def test_forget_unbinds():
    session = QrLogin(transport=FakeTransport([QR, CONFIRMED]))
    session.begin()
    session.poll()
    assert describe_credentials()["bound"] is True

    assert forget_credentials() is True
    assert describe_credentials()["bound"] is False
    assert forget_credentials() is False, "已经没有了还报删掉了"


def test_the_channel_reads_the_scanned_token_and_its_base_url():
    """通道拿 token 的顺序：环境变量优先，然后是扫码存下来的那份。

    还要跟着凭据换 base_url —— `scaned_but_redirect` 会把账号分到另一个域名上，而那个
    域名只有登录时才知道。忽略它的症状是长轮询一直空转。
    """
    from core.channels.weixin import WeixinChannel

    session = QrLogin(transport=FakeTransport([QR, CONFIRMED]))
    session.begin()
    session.poll()

    channel = WeixinChannel(transport=FakeTransport([]))
    assert channel._token() == CONFIRMED["bot_token"]
    assert channel.base_url == CONFIRMED["baseurl"]


def test_an_environment_token_still_wins(monkeypatch):
    """自动化那条路没有被关掉。反过来的话，一个想临时换账号的人会发现改环境变量没用。"""
    from core.channels.weixin import WeixinChannel

    session = QrLogin(transport=FakeTransport([QR, CONFIRMED]))
    session.begin()
    session.poll()
    monkeypatch.setenv("VOX_WEIXIN_TOKEN", "tk-from-env")

    assert WeixinChannel(transport=FakeTransport([]))._token() == "tk-from-env"


def test_an_unbound_channel_says_where_to_go():
    """报错要说**该往哪走**。上一版只说「没有 $VOX_WEIXIN_TOKEN」，而那个 token 本来
    就是扫码换来的 —— 使用者照那句话去找，是找不到的。"""
    from core.channels.weixin import WeixinChannel

    with pytest.raises(ChannelError, match="扫码登录"):
        WeixinChannel(transport=FakeTransport([]))._token()


def test_blocking_login_shares_the_same_state_machine():
    """命令行那条路存在的理由不是「也要有个 CLI」，而是控制台起不来时仍然能绑定。
    共用状态机，所以两条路不会各自漂移。"""
    transport = FakeTransport([QR, {"status": "wait"}, {"status": "scaned"}, CONFIRMED])
    seen: list[str] = []

    result = login_blocking(
        transport=transport, sleep=lambda _s: None, on_status=seen.append, timeout_s=10
    )

    assert result["status"] == "confirmed"
    assert seen == ["wait", "scaned", "confirmed"]


def test_blocking_login_times_out_instead_of_hanging():
    transport = FakeTransport([QR] + [{"status": "wait"}] * 50)
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    with pytest.raises(ChannelError, match="超时"):
        login_blocking(transport=transport, sleep=sleep, timeout_s=0.0)


def test_qr_svg_refuses_to_return_an_empty_frame():
    """``segno`` 缺失时要报错而不是给一个空白框 —— 空白框会被读成「接口坏了」。"""
    svg = qr_svg("https://example.com/x")
    assert svg.count("<path") >= 1
