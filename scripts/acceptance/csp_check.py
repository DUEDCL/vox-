"""打包后的前端在**真的 CSP 下**跑一遍 —— dev server 不发 CSP，所以它测不出来。

    .venv\\Scripts\\python.exe scripts/acceptance/csp_check.py            # 现行策略
    .venv\\Scripts\\python.exe scripts/acceptance/csp_check.py --relaxed  # 对照：不发 CSP

存在的理由是一个静默两天的缺陷。`tauri.conf.json` 的 `connect-src` 少了 `'self'`，
于是 `desktop/src/sequence.ts` 取雪碧图元数据的两个 ``fetch`` 被 CSP 拒 —— 而
`desktop/src/main.ts` 对加载失败的处理是**回退手写渲染器 + 一条 console.warn**。
表现：打包后的唤醒球一直不是 AE 序列层，而每一层都报告自己健康，
`npm run dev` 那一侧永远是对的（它一个字节的 CSP 都不发）。

**策略从 `tauri.conf.json` 读，不在这里抄一份。** 抄一份就会漂，而漂掉之后这个脚本
验的是一个不存在的配置。

判据（要人看一眼浏览器控制台，所以这是 SIM 不是 AUTO）：

1. 控制台**零** `Content Security Policy` 报错；
2. 本进程打印的访问日志里 `orb/flow.json`、`orb/burst.json`、`orb/flow.png`、
   `orb/burst.png` 四条都是 200。

少任何一条都意味着打包后那一层不工作。
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "desktop" / "dist"
TAURI_CONF = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"

#: 这四条必须在日志里出现且为 200，否则序列层没起来。
REQUIRED = ("/orb/flow.json", "/orb/burst.json", "/orb/flow.png", "/orb/burst.png")


def read_csp() -> str:
    """生产用的那条策略，从 ``tauri.conf.json`` 原样取。"""
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    return str(((conf.get("app") or {}).get("security") or {}).get("csp") or "")


def make_handler(csp: str, seen: set[str]):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self) -> None:
            # CSP 只加在文档上 —— 它就是从文档继承给所有子资源的，加在每个响应上
            # 反而会掩盖「策略来自哪里」这件事。
            if csp and urlparse(self.path).path in ("", "/", "/index.html"):
                self.send_header("Content-Security-Policy", csp)
            super().end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            line = fmt % args
            path = urlparse(self.path).path
            if path in REQUIRED and " 200 " in f" {line} ":
                seen.add(path)
            sys.stderr.write(f"{line}\n")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="run the packaged frontend under the real CSP")
    parser.add_argument("--port", type=int, default=5399)
    parser.add_argument(
        "--relaxed",
        action="store_true",
        help="不发 CSP —— 对照组，等价于 dev server 那一侧",
    )
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not (DIST / "index.html").is_file():
        print(f"没有构建产物：{DIST}（先 cd desktop && npm run build）", file=sys.stderr)
        return 1
    csp = "" if args.relaxed else read_csp()
    print(f"dist:  {DIST}")
    print(f"csp:   {csp or '(不发 —— 对照组)'}")

    seen: set[str] = set()
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    url = f"http://127.0.0.1:{args.port}/"
    with http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), functools.partial(make_handler(csp, seen), directory=str(DIST))
    ) as httpd:
        print(f"open:  {url}   —— 看浏览器控制台，Ctrl+C 结束并打判据")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - 打不开浏览器不影响这个脚本的用途
                pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("")
    missing = [path for path in REQUIRED if path not in seen]
    for path in REQUIRED:
        print(f"  {'OK ' if path in seen else '缺 '} {path}")
    if missing:
        print(f"\n序列层没起来：{len(missing)}/4 条资产没被取到。控制台里应该有 CSP 报错。")
        return 1
    print("\n四条资产都到手了 —— 序列层在这条策略下能工作。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
