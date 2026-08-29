"""出站 HTTP 的共同约定。现在只有一条：自报身份。

urllib 默认发 ``Python-urllib/3.x``，而相当多网关的反爬规则直接对它回 403。本机实测过
两次，两个不同的地方：控制台的 ``/v1/models`` 探测，和 http agent 的 ``/v1/chat/
completions`` —— 同一个 key、同一个主机，UA 是 ``Python-urllib/3.13`` 就 403，换掉就 200。
这个 403 会被读成「密钥不对」，而钥匙其实是对的。

**声明真名，不伪装成浏览器。** 被拦的原因是「看起来像脚本」，而这确实是个脚本；伪装成
Chrome 会让对端的限流和审计都失去意义，也让我们自己在对端的日志里查不到自己。

`core/tools/search_backends.py` 里那个浏览器形状的 UA 是**另一件事**，不要统一过来：
它抓的是 HTML 页面，而那个端点会按 UA 决定返回带 JS 的版本还是无 JS 的版本 —— 那里
UA 是内容协商的一部分，不是身份声明。
"""

from __future__ import annotations

#: 调 API 时的自报身份。版本号跟着 desktop/src-tauri/tauri.conf.json 的 version 走。
API_USER_AGENT = "Vox/0.1 (local voice console)"

__all__ = ["API_USER_AGENT"]
