/* `demo.html` 的驱动 —— 完整流程 + 单态按钮 + 确认卡。

   这一页只做两件事：把按钮接到 `sequence.ts` 的态机上，把读数打出来。渲染的判断全在
   `sequence.ts` 里，这里不许再写第二份长相参数 —— 那正是「色值只有一个来源」那条规则
   在这一层的落点。

   **没有待机态**：未唤醒 = `hidden` = 不画。所以「收起」之后画布是空的，不是暗着的球。 */
import {
  loadSheets, newMotion, setState, stepMotion, drawOrb, lookOf,
  type SeqState, type Sheets, type Motion,
} from './src/sequence';
import { BotRenderer, type BotState } from './src/bot-render';

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

/* ── 渲染层 ─────────────────────────────────────────────────────────────────
   两层并存。`bot` 是第十二代（bloub 引擎，零资产、纯矢量），`seq` 是现行的 AE 雪碧图。
   **态机只有一份**（`m`）：两层都从它取态，所以切换不改任何映射，也不会出现「一层在听
   另一层在说」。`clock` 是给 bot 层的时间戳 —— 它的引擎按绝对时间做态间 morph。 */
let renderer: 'bot' | 'seq' = 'seq';
const bot = new BotRenderer();
let clock = 0;

/** 切一个态，两层同时收到。 */
function apply(s: SeqState): void {
  setState(m, s);
  bot.setState(s as BotState, clock);
}

function setRenderer(next: 'bot' | 'seq'): void {
  renderer = next;
  document.getElementById('rSeq')?.classList.toggle('on', next === 'seq');
  document.getElementById('rBot')?.classList.toggle('on', next === 'bot');
  // 切过来时把 bot 层按当前态落定，否则它还停在建构时的 `sleep` 上（一个小点）。
  if (next === 'bot') { bot.setState(m.state as BotState, clock); bot.settle(); }
}

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
  apply(step.s);
  if (step.ms > 0) chainTimer = window.setTimeout(() => runChain(i + 1), step.ms);
}

function go(s: SeqState): void {
  stopChain();
  apply(s);
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
  window.setTimeout(() => { if (m.state === 'cancelled') apply('hidden'); }, 1400);
});

document.getElementById('rSeq')?.addEventListener('click', () => setRenderer('seq'));
document.getElementById('rBot')?.addEventListener('click', () => setRenderer('bot'));

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
if (q.get('renderer') === 'bot') setRenderer('bot');

function forceNow(): void {
  if (forcedState === null) return;
  m.state = forcedState; m.prev = forcedState; m.w = 1; m.appear = 1;
  m.thinkingFor = forcedState === 'thinking' ? Number(q.get('think') ?? 3) : 0;
  autoEl.checked = false;
  // bot 层的对等动作：`settle` 跳过入场 morph。不调的话静态一帧画的是**上一个态**
  // （从 hidden 出来时那是 `sleep`，一个直径十几像素的点），截图取证会全错。
  if (bot.current !== forcedState) bot.setState(forcedState as BotState, clock);
  bot.settle();
}

/* ── 主循环 ─────────────────────────────────────────────────────────────── */
let last = performance.now();
const t0 = last;
let frames = 0, fpsAt = last, fps = 0;

function tick(now: number): void {
  const dt = Math.min(0.1, (now - last) / 1000);
  last = now;
  const t = forcedT ?? (now - t0) / 1000;
  clock = t;
  stepMotion(m, dt);
  forceNow();   // 覆盖必须在 step 之后 —— 否则淡入会把它推回去

  // 说话时音量自动起伏 —— 真机上这一路来自 TTS 的包络，这里用两个不整除的正弦近似，
  // 好让「呼吸跟着音量」这条链在没有麦克风的机器上也看得见
  let amp = Number(ampEl.value);
  if (autoEl.checked && m.state === 'speaking') {
    amp = 0.35 + 0.45 * Math.abs(Math.sin(t * 3.1) * 0.7 + Math.sin(t * 1.27) * 0.3);
  }

  if (renderer === 'bot') bot.draw(ctx, t, m.appear, amp);
  else if (sheets !== null) drawOrb(ctx, sheets, m, t, amp);

  frames++;
  if (now - fpsAt > 700) { fps = frames * 1000 / (now - fpsAt); frames = 0; fpsAt = now; }
  const lk = lookOf(m.state);
  const p = bot.probe(t);
  logEl.innerHTML = failed !== '' && renderer === 'seq'
    ? `<span class="err">${failed}</span>`
    : (renderer === 'bot' ? [
      `态       ${m.state}${m.w < 1 ? `  ← ${m.prev} (${(m.w * 100) | 0}%)` : ''}`,
      `渲染     bloub · 第十二代 · 零资产（纯矢量，2837 行）`,
      `bloub 态 ${p.bot}   球色 ${m.state === 'error' ? '#E23A2E 朱红' : m.state === 'cancelled' ? '#6b8f86 压暗' : '#2fbfa0 青绿'}`,
      `几何     包围盒 ${p.w}×${p.h}（球半径=100）· 眼 ${p.eyes} · 点 ${p.dots} · 环 ${p.arcs} · 通知点 ${p.notif}`,
      `起伏     ${m.state === 'speaking' ? `±${(amp * 14).toFixed(1)}% @ 5.02 rad/s（音量驱动）` : '引擎自带 ±0.5%（活着，不是在说）'}`,
      `音量     ${amp.toFixed(2)}${autoEl.checked && m.state === 'speaking' ? '（自动）' : ''}`,
      `出现     ${(m.appear * 100) | 0}%`,
      `画布     ${cv.width}×${cv.height}   ${fps.toFixed(0)} FPS`,
    ] : [
      `态       ${m.state}${m.w < 1 ? `  ← ${m.prev} (${(m.w * 100) | 0}%)` : ''}`,
      `序列     ${lk.sheet}  ×${lk.rate.toFixed(2)}${m.state === 'thinking' ? ` ×加速${(1 + 0.7 * Math.min(1, m.thinkingFor / 6)).toFixed(2)}` : ''}`,
      `亮度     ${lk.gain.toFixed(2)}   呼吸 ±${(lk.breath * 100).toFixed(1)}% @ ${lk.breathHz.toFixed(2)} rad/s`,
      `语义色   ${lk.tint ?? '素材原色（六片各自的簇色）'}`,
      `音量     ${amp.toFixed(2)}${autoEl.checked && m.state === 'speaking' ? '（自动）' : ''}`,
      `出现     ${(m.appear * 100) | 0}%`,
      `画布     ${cv.width}×${cv.height}   ${fps.toFixed(0)} FPS`,
      sheets === null ? '资产     加载中…'
        : `资产     flow ${sheets.flow.frames}帧 · burst ${sheets.burst.frames}帧 · 格 ${sheets.flow.cell}px`,
    ]).join('\n');

  requestAnimationFrame(tick);
}

resize();
window.addEventListener('resize', resize);
/* 第一帧**同步**画，不等 rAF。取证时 rAF 可能一次都不触发（隐藏的浏览器面板会把它整个
   暂停），于是截图里是一张空画布 + 读数还停在 HTML 里那个 `…`，看起来像渲染层坏了。
   实测走过这一步。`tick` 自己会接上 rAF，所以这里不能再调一次，否则跑两条循环。 */
tick(performance.now());

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
