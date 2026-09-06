import { defineConfig } from 'vite';

/* **只有一个作用：让 UI 验收端口的 `/` 直接落在验收页上。**
 *
 * 为什么不改 `index.html`：那是生产入口，Tauri 打包和 `npm run tauri dev` 都要它。
 * 为什么要判端口：Tauri 的 `devUrl` 是 `http://localhost:5173`（`tauri-conf.json`），
 * 如果无条件重写 `/`，`npm run tauri dev` 就会把验收页装进那个 148px 的透明窗口。
 * 所以重写只在 **5273** 上生效 —— 那是这个 worktree 专给 UI 验收开的端口
 * （`.claude/launch.json` 的 `desktop-ui`）。
 *
 * 落点是 `demo.html`（实机演示：完整流程 + 单态按钮 + 确认卡），不是 `review.html`
 * —— 后者是手写渲染器（`core.ts`）的八态静态对照，渲染层换成 AE 预渲染序列之后它只剩
 * fallback 的验收价值，用 `/review.html` 直达。
 */
export default defineConfig({
  plugins: [
    {
      name: 'vox-review-root',
      apply: 'serve',
      configureServer(server) {
        if (server.config.server.port !== 5273) return;
        server.middlewares.use((req, _res, next) => {
          // 必须把带 query 的根路径也算进来：`/?state=speaking` 不等于 `/`，漏掉它的话
          // vite 会走 SPA fallback 返回 `index.html`（生产页），于是截图里是手写渲染器画的
          // 球而不是序列层 —— 两者长得都像一颗球，这个错静默且很难看出来，已踩过一次。
          const u = req.url ?? '';
          if (u === '' || u === '/' || u.startsWith('/?')) {
            req.url = `/demo.html${u.startsWith('/?') ? u.slice(1) : ''}`;
          }
          next();
        });
      },
    },
  ],
});
