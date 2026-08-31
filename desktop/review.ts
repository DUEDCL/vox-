/* 唤醒球验收面。**这一页是给人看的，不是给我截图用的。**
 *
 * 三条它必须做到的事：
 *   ① 深浅两种桌面底 —— 球是透明置顶窗口，只在深底上看是自欺；
 *   ② 全部八态 + 状态之间的**转化过程** —— 静止的格子里看不见弹簧过冲、成环、
 *      呼吸、晃动、液面涟漪，而那些正是这一代的全部内容；
 *   ③ 放大 360px 与真实 140px **同时**在场 —— 片体质感只有放大才看得出，
 *      而「桌面上到底多大」只有真实尺寸说得准。
 *
 * 插值用 **core.ts 导出的那两个函数本身**（`bloomSpring` / `ringRate`），不抄式子。
 * `preview.html` 的「活的一带」抄了一份 `*0.12` / `*0.07`，而主循环早就换成弹簧了 ——
 * 抄下来的常数会过期，import 进来的不会。
 */
import './src/style.css';
import {
  CorollaBreath, bloomLevel, bloomSpring, ringLevel, ringRate,
  type CoreFrame, type CoreState, type Palette,
} from './src/core';
import { mountTune } from './tune';

type Step = { state: CoreState; lanes: number; gated: boolean; hold: number; label: string };

/* 自动序列覆盖每一条要看的转化：醒过来（过冲）→ 成环 → 分组 → 开口（过冲）→ 收回 idle
   → 拦住 → 放行 → 出错（不过冲、最快）→ 垮掉（不过冲、落得更快）→ 回 idle。 */
const SEQ: Step[] = [
  { state: 'idle', lanes: 1, gated: false, hold: 2.0, label: '待机' },
  { state: 'listening', lanes: 1, gated: false, hold: 2.6, label: '聆听' },
  { state: 'thinking', lanes: 1, gated: false, hold: 2.8, label: '思考' },
  { state: 'thinking', lanes: 3, gated: false, hold: 2.4, label: '思考 ×3 路' },
  { state: 'speaking', lanes: 1, gated: false, hold: 2.8, label: '回复' },
  { state: 'idle', lanes: 1, gated: false, hold: 1.8, label: '回到待机' },
  { state: 'thinking', lanes: 1, gated: true, hold: 2.8, label: '闸门' },
  { state: 'idle', lanes: 1, gated: false, hold: 1.6, label: '放行' },
  { state: 'error', lanes: 1, gated: false, hold: 2.4, label: '异常' },
  { state: 'cancelled', lanes: 1, gated: false, hold: 2.4, label: '取消' },
];

const CASES: Step[] = [
  { state: 'idle', lanes: 1, gated: false, hold: 0, label: '待机' },
  { state: 'listening', lanes: 1, gated: false, hold: 0, label: '聆听' },
  { state: 'thinking', lanes: 1, gated: false, hold: 0, label: '思考' },
  { state: 'thinking', lanes: 3, gated: false, hold: 0, label: '思考 ×3' },
  { state: 'speaking', lanes: 1, gated: false, hold: 0, label: '回复' },
  { state: 'cancelled', lanes: 1, gated: false, hold: 0, label: '取消' },
  { state: 'error', lanes: 1, gated: false, hold: 0, label: '异常' },
  { state: 'thinking', lanes: 1, gated: true, hold: 0, label: '闸门' },
];

/* ---- 调色板：唯一来源是 style.css，切态时读一次（与 main.ts 的 readPalette 同做法）---- */
const probe = document.createElement('div');
probe.style.display = 'none';
document.body.appendChild(probe);
function readPalette(state: CoreState, gated: boolean): Palette {
  probe.dataset.state = state;
  if (gated) probe.dataset.confirm = 'true';
  else delete probe.dataset.confirm;
  const s = getComputedStyle(probe);
  const p = (k: string): string => s.getPropertyValue(k).trim();
  return {
    core: p('--lum-core'), mid: p('--lum-mid'), far: p('--lum-far'), alt: p('--lum-alt'),
    glass: p('--glass'), edge: p('--edge'),
  };
}

/* ---- 一个共享的仿真：四个球渲染**同一帧**，所以深浅两底与两个尺寸完全同步 ---- */
let cur: Step = SEQ[0];
let palette = readPalette(cur.state, cur.gated);
let bloom = bloomLevel({ state: cur.state, gated: cur.gated, amplitude: 0.85 } as CoreFrame);
let bloomVel = 0;
let ring = ringLevel({ state: cur.state, gated: cur.gated } as CoreFrame);
let t = 0;
let following = true;
let paused = false;
let stepIdx = 0;
let held = 0;

function frame(): CoreFrame {
  return {
    state: cur.state, t, amplitude: 0.85, lanes: cur.lanes, gated: cur.gated,
    palette, bloom, ring, pulse: 0,
  } as CoreFrame;
}

function goto(s: Step, manual: boolean): void {
  cur = s;
  palette = readPalette(s.state, s.gated);
  bloomVel = 0;   // 与 main.ts 一致：切态时清速度，否则上一段的冲劲会带进这一段
  if (manual) { following = false; }
  renderBar();
}

/* ---- 活的四个球 ---- */
type Live = { core: CorollaBreath; box: HTMLElement; css: number };
const lives: Live[] = [];
function mountLive(id: string, css: number): void {
  const box = document.getElementById(id);
  if (!box) return;
  const cv = box.querySelector('canvas') as HTMLCanvasElement;
  lives.push({ core: new CorollaBreath(cv), box, css });
}
mountLive('bigDark', 360);
mountLive('bigLight', 360);
mountLive('realDark', 140);
mountLive('realLight', 140);

/* ---- 静止八格 × 深浅两底。钉在每态的**目标值**上，供逐态对照 ----
   每格记下自己的 `{core, frame}`，好让参数面板改动后能重画（它们不在 rAF 里）。 */
const cases: { core: CorollaBreath; f: CoreFrame }[] = [];
function redrawCases(): void {
  for (const c of cases) c.core.draw(c.f);
}

function mountGrid(id: string): void {
  const host = document.getElementById(id);
  if (!host) return;
  for (const c of CASES) {
    const wrap = document.createElement('div');
    wrap.className = 'cellwrap';
    const cell = document.createElement('div');
    cell.className = 'cell';
    cell.dataset.state = c.state;
    if (c.gated) cell.dataset.confirm = 'true';
    const cv = document.createElement('canvas');
    cell.appendChild(cv);
    const name = document.createElement('b');
    name.textContent = c.label;
    const key = document.createElement('code');
    key.textContent = c.gated ? 'gated' : c.state;
    wrap.append(cell, name, key);
    host.appendChild(wrap);

    const core = new CorollaBreath(cv);
    const pal = readPalette(c.state, c.gated);
    const f = {
      state: c.state, t: 1.4, amplitude: 0.85, lanes: c.lanes, gated: c.gated,
      palette: pal, bloom: 0, ring: 0, pulse: 0,
    } as CoreFrame;
    f.bloom = bloomLevel(f);
    f.ring = ringLevel(f);
    core.resize(148);
    core.draw(f);
    cases.push({ core, f });
    // 点一格 = 让活的那两带跳到这一态，好逐态细看它的动
    cell.style.cursor = 'pointer';
    cell.addEventListener('click', () => goto(c, true));
  }
}
mountGrid('gridDark');
mountGrid('gridLight');

/* ---- 控制条 ---- */
const bar = document.getElementById('bar') as HTMLElement;
function renderBar(): void {
  if (!bar.dataset.built) {
    const mk = (label: string, fn: () => void, mark?: () => boolean): HTMLButtonElement => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.addEventListener('click', () => { fn(); renderBar(); });
      if (mark) b.dataset.mark = '1';
      bar.appendChild(b);
      return b;
    };
    mk('⏸ 暂停', () => { paused = !paused; });
    mk('▶ 跟随序列', () => { following = true; paused = false; });
    for (const c of CASES) mk(c.label, () => goto(c, true), () => cur === c);
    const read = document.createElement('span');
    read.className = 'read';
    read.id = 'read';
    bar.appendChild(read);
    bar.dataset.built = '1';
  }
  const btns = bar.querySelectorAll('button');
  btns[0].textContent = paused ? '▶ 继续' : '⏸ 暂停';
  btns[1].setAttribute('aria-pressed', String(following));
  CASES.forEach((c, i) => {
    btns[i + 2].setAttribute('aria-pressed', String(!following && cur.state === c.state
      && cur.lanes === c.lanes && cur.gated === c.gated));
  });
}
renderBar();

/* ---- 主循环。**式子不抄，直接调 core.ts 导出的 `bloomSpring` / `ringRate`** ---- */
const read = document.getElementById('read') as HTMLElement;
let last = performance.now();

function loop(now: number): void {
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  if (!paused) {
    t += dt;
    // 自动推进
    if (following) {
      held += dt;
      if (held >= cur.hold) {
        held = 0;
        stepIdx = (stepIdx + 1) % SEQ.length;
        const nx = SEQ[stepIdx];
        cur = nx;
        palette = readPalette(nx.state, nx.gated);
        bloomVel = 0;
        renderBar();
      }
    }
    // 每态自己的弹簧（过冲与否是 d 相对 2√k 定的，见 core.ts 的 bloomSpring）
    const sp = bloomSpring(cur.state);
    const target = bloomLevel(frame());
    bloomVel += (target - bloom) * sp.k - bloomVel * sp.d;
    bloom += bloomVel;
    if (bloom > 1) { bloom = 1; if (bloomVel > 0) bloomVel = 0; }
    if (bloom < 0) { bloom = 0; if (bloomVel < 0) bloomVel = 0; }
    // 成环/收回（收回更快：想完了要说话）
    const rt = ringLevel(frame());
    ring += (rt - ring) * ringRate(rt, ring);
    if (Math.abs(rt - ring) < 0.004) ring = rt;   // 吸附：指数逼近永远到不了 1
  }

  const f = frame();
  for (const l of lives) { l.core.resize(l.css); l.core.draw(f); }

  const sp = bloomSpring(cur.state);
  const over = sp.d < 2 * Math.sqrt(sp.k);
  read.textContent =
    `${cur.gated ? 'gated' : cur.state}${cur.lanes > 1 ? ' ×' + cur.lanes : ''}`
    + `   bloom ${bloom.toFixed(3)} → ${bloomLevel(f).toFixed(3)}`
    + `   ring ${ring.toFixed(2)} → ${ringLevel(f).toFixed(2)}\n`
    + `弹簧 k ${sp.k} d ${sp.d}（${over ? '允许过冲' : '不过冲'}）`
    + `   ${following ? `序列 ${stepIdx + 1}/${SEQ.length} · 停留 ${held.toFixed(1)}/${cur.hold}s` : '手动'}`
    + `${paused ? ' · 已暂停' : ''}`;
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);

const note = document.getElementById('note');
if (note) {
  note.innerHTML =
    '<b>这一页的三条读法。</b>① 上面两带是<b>活的</b>：弹簧过冲（醒过来、开口时球会冲过目标再回落）、'
    + '成环与收回（收回比成环快 1.5 倍）、每片各自的呼吸、亮度质心的晃动、液面轮廓四条谐波各自慢转 —— '
    + '静止的格子里这些全都看不见。② 点第 ③ ④ 带的任意一格，活的那两带会跳到那一态，好逐态细看它的动；'
    + '点「跟随序列」回到自动循环。③ <b>浅色底那半边是硬验收项</b>：球是透明置顶窗口，'
    + '会叠在白色壁纸上，而花冠用加色合成 —— 加色在白底上不产生对比，所以球必须自带一层暗。'
    + '<br><b>当前已知的两处不足，看的时候可以直接对着骂：</b>'
    + '<code>thinking</code> 的六片在 360px 上仍然读作「6 个形状」而不是「6 团光」（尺度所限，'
    + '生产尺寸下每片只有约 13×8 像素）；<code>gated</code> 与 <code>idle</code> 在<b>静止一帧</b>上'
    + '几乎只差颜色（它原来的精确圆与硬边琥珀环都已按要求删掉）。';
}

/* ---- 参数控件。改 `TUNE` 的下一帧就生效（活的两带每帧重画），但**静止的八格是一次性
   画的**，所以要在参数变化时重画它们 —— 否则调滑块只有上面两带跟着动，下面八格不动，
   看起来像「有的地方没生效」。 ---- */
const tuneHost = document.getElementById('tune');
if (tuneHost) mountTune(tuneHost, () => { redrawCases(); });

/* core.ts / style.css 一改，vite 会把这一页整页刷新 —— 所以「改一步就能看到」成立。 */
if (import.meta.hot) import.meta.hot.accept(() => location.reload());
