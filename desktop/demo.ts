/* `demo.html` 的驱动 —— 完整流程 + 单态按钮 + 确认卡。

   这一页只做两件事：把按钮接到 `sequence.ts` 的态机上，把读数打出来。渲染的判断全在
   `sequence.ts` 里，这里不许再写第二份长相参数 —— 那正是「色值只有一个来源」那条规则
   在这一层的落点。

   **没有待机态**：未唤醒 = `hidden` = 不画。所以「收起」之后画布是空的，不是暗着的球。 */
import {
  loadSheets, newMotion, setState, stepMotion, drawOrb, lookOf,
  type SeqState, type Sheets, type Motion,
} from './src/sequence';

const cv = document.getElementById('orb') as HTMLCanvasElement;
const logEl = document.getElementById('log') as HTMLElement;
const card = document.getElementById('card') as HTMLElement;
const ampEl = document.getElementById('amp') as HTMLInputElement;
const autoEl = document.getElementById('auto') as HTMLInputElement;
const lightEl = document.getElementById('light') as HTMLInputElement;
const bigEl = document.getElementById('big') as HTMLInputElement;

const ctx = cv.getContext('2d');
if (ctx === null) throw new Error('no 2d context');

const m: Motion = newMotion();
let sheets: Sheets | null = null;
let failed = '';

/** 画布的物理像素跟着 CSS 尺寸与 DPR 走。生产是 140px @2× = 280。 */
function resize(): void {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const css = cv.clientWidth || 140;
  cv.width = Math.round(css * dpr);
  cv.height = Math.round(css * dpr);
}

/* ── 流程 ───────────────────────────────────────────────────────────────────
   一条链跑下来：唤醒 → 听 2.2s → 思 3.4s → 说 4.5s → 收起。时长按真实一轮对话的量级给，
   不是为了好看 —— 思考那一段必须够长，才看得出「转圈越来越快」。 */
type Step = { s: SeqState; ms: number };
const CHAIN: Step[] = [
  { s: 'listening', ms: 2200 },
  { s: 'thinking', ms: 3400 },
  { s: 'speaking', ms: 4500 },
  { s: 'hidden', ms: 0 },
];
let chainTimer: number | null = null;

function stopChain(): void {
  if (chainTimer !== null) { clearTimeout(chainTimer); chainTimer = null; }
}

function runChain(i = 0): void {
  stopChain();
  if (i >= CHAIN.length) return;
  const step = CHAIN[i];
  setState(m, step.s);
  if (step.ms > 0) chainTimer = window.setTimeout(() => runChain(i + 1), step.ms);
}

function go(s: SeqState): void {
  stopChain();
  setState(m, s);
  card.classList.toggle('show', s === 'gated');
}

document.getElementById('run')?.addEventListener('click', () => {
  card.classList.remove('show');
  runChain(0);
});
document.getElementById('stop')?.addEventListener('click', () => go('hidden'));
for (const b of document.querySelectorAll<HTMLButtonElement>('button[data-s]')) {
  b.addEventListener('click', () => go(b.dataset.s as SeqState));
}
// 确认卡的两个出口：允许 → 接着说；拒绝 → 光垮掉再收起。挂起的确认必须有落定，
// 「一直挂着」在安全语义上等价于「未拒绝」。
document.getElementById('cAllow')?.addEventListener('click', () => { card.classList.remove('show'); go('speaking'); });
document.getElementById('cDeny')?.addEventListener('click', () => {
  card.classList.remove('show');
  go('cancelled');
  window.setTimeout(() => { if (m.state === 'cancelled') setState(m, 'hidden'); }, 1400);
});

lightEl.addEventListener('change', () => document.body.classList.toggle('light', lightEl.checked));
bigEl.addEventListener('change', () => { cv.classList.toggle('big', bigEl.checked); resize(); });

/* ── URL 覆盖 ───────────────────────────────────────────────────────────────
   `?state=speaking&t=3.2` 直接落在一个**已经完成淡入**的态上。两个用处：
     ① headless 截图取证 —— 虚拟时间会把 rAF 压掉，淡入进度停在 20% 上，球几乎看不见，
        所以取证必须能跳过淡入（这条在 zoom.html 上踩过一次）
     ② 把一组参数发给别人看，不用口述「点哪几个按钮」
   `t` 固定时间 ⇒ 同一个 URL 每次渲出同一帧，截图可复现。 */
const q = new URLSearchParams(location.search);
const forcedState = q.get('state') as SeqState | null;
const forcedT = q.has('t') ? Number(q.get('t')) : null;
if (q.get('light') === '1') { lightEl.checked = true; document.body.classList.add('light'); }
if (q.get('big') === '1') { bigEl.checked = true; cv.classList.add('big'); }
if (q.get('card') === '1') card.classList.add('show');
if (q.has('amp')) ampEl.value = String(Number(q.get('amp')));

function forceNow(): void {
  if (forcedState === null) return;
  m.state = forcedState; m.prev = forcedState; m.w = 1; m.appear = 1;
  m.thinkingFor = forcedState === 'thinking' ? Number(q.get('think') ?? 3) : 0;
  autoEl.checked = false;
}

/* ── 主循环 ─────────────────────────────────────────────────────────────── */
let last = performance.now();
const t0 = last;
let frames = 0, fpsAt = last, fps = 0;

function tick(now: number): void {
  const dt = Math.min(0.1, (now - last) / 1000);
  last = now;
  const t = forcedT ?? (now - t0) / 1000;
  stepMotion(m, dt);
  forceNow();   // 覆盖必须在 step 之后 —— 否则淡入会把它推回去

  // 说话时音量自动起伏 —— 真机上这一路来自 TTS 的包络，这里用两个不整除的正弦近似，
  // 好让「呼吸跟着音量」这条链在没有麦克风的机器上也看得见
  let amp = Number(ampEl.value);
  if (autoEl.checked && m.state === 'speaking') {
    amp = 0.35 + 0.45 * Math.abs(Math.sin(t * 3.1) * 0.7 + Math.sin(t * 1.27) * 0.3);
  }

  if (sheets !== null) drawOrb(ctx, sheets, m, t, amp);

  frames++;
  if (now - fpsAt > 700) { fps = frames * 1000 / (now - fpsAt); frames = 0; fpsAt = now; }
  const lk = lookOf(m.state);
  logEl.innerHTML = failed !== ''
    ? `<span class="err">${failed}</span>`
    : [
      `态       ${m.state}${m.w < 1 ? `  ← ${m.prev} (${(m.w * 100) | 0}%)` : ''}`,
      `序列     ${lk.sheet}  ×${lk.rate.toFixed(2)}${m.state === 'thinking' ? ` ×加速${(1 + 0.7 * Math.min(1, m.thinkingFor / 6)).toFixed(2)}` : ''}`,
      `亮度     ${lk.gain.toFixed(2)}   呼吸 ±${(lk.breath * 100).toFixed(1)}% @ ${lk.breathHz.toFixed(2)} rad/s`,
      `语义色   ${lk.tint ?? '素材原色（六片各自的簇色）'}`,
      `音量     ${amp.toFixed(2)}${autoEl.checked && m.state === 'speaking' ? '（自动）' : ''}`,
      `出现     ${(m.appear * 100) | 0}%`,
      `画布     ${cv.width}×${cv.height}   ${fps.toFixed(0)} FPS`,
      sheets === null ? '资产     加载中…'
        : `资产     flow ${sheets.flow.frames}帧 · burst ${sheets.burst.frames}帧 · 格 ${sheets.flow.cell}px`,
    ].join('\n');

  requestAnimationFrame(tick);
}

resize();
window.addEventListener('resize', resize);
requestAnimationFrame(tick);

loadSheets('/orb')
  .then((s) => { sheets = s; if (forcedState === null) runChain(0); else forceNow(); })
  .catch((e) => {
    failed = `${String(e)}\n\n雪碧图还没生成。三步：\n`
      + '  1  taskkill /F /IM AfterFX.exe   ← aerender 与 AE GUI 同时跑会报 Unable to receive\n'
      + '  2  aerender -project "…ui设计工程文件.aep" -comp "预合成 3" \\\n'
      + '       -OMtemplate "带有 Alpha 的 TIFF 序列 " -s 0 -e 287 -output ".vox-ref-ae\\p3_[####].tif"\n'
      + '  3  python scripts/build_orb_assets.py ".vox-ref-ae/p3_*.tif" --bg ".vox-bg-ae/bg_*.tif" \\\n'
      + '       --start 76 --count 112 --fade 10 --crop 918,388,686 --name flow';
  });
