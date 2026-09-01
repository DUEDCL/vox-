import './style.css';
import {
  CorollaBreath, bloomLevel, bloomAt, petalCount, spinRate, skew, blobCount,
  breathAmp, breathRate, coreGlow, ringLevel, ringRate, bloomSpring, contourRadii, blend,
  type CoreFrame, type CoreState, type Palette,
} from './core';
import {
  loadSheets, newMotion, setState as seqSetState, stepMotion, drawOrb,
  type SeqState, type Sheets,
} from './sequence';

/* 唤醒球不是浏览器页面：右键菜单、文本拖拽鬼影一律关掉。 */
window.addEventListener('contextmenu', (e) => e.preventDefault());
window.addEventListener('dragstart', (e) => e.preventDefault());
window.addEventListener('selectstart', (e) => e.preventDefault());

/* Vox — main.ts
   六态球体 + 流式回复 + 工具确认卡(FR-6.13)。
   测试钩子保持兼容现有 SIM 测试。 */

/* 调参入口：`?orb=240`（Rust 侧由 `VOX_ORB_SIZE` 带过来，见 src-tauri/src/main.rs）。
   只改 CSS 变量，几何全部按比例跟着走 —— 命中区是量出来的，不需要同步改。
   范围与 Rust 侧一致地钳制一次：URL 是可以手改的，前端不假设它干净。 */
/* 后台开关一览（都由 Rust 侧从环境变量拼进 URL，见 src-tauri/src/main.rs）：
     VOX_ORB_SIZE=140   球 + 外发光的布局盒尺寸（96–420）
     VOX_SHOW_TEXT=1    平时也显示回复文字。**默认关** —— 只有报错/拒绝会出文字。
   走 URL query 而不是新加 IPC 命令：IPC 面就是那四个 `vox_*`，为开关扩大它不值得。 */
(() => {
  const raw = new URLSearchParams(location.search).get('orb');
  const px = raw === null ? NaN : Number(raw);
  if (Number.isFinite(px) && px >= 96 && px <= 420) {
    document.documentElement.style.setProperty('--orb', `${Math.round(px)}px`);
  }
})();

const app = document.querySelector<HTMLElement>('#app')!;
const orb = document.querySelector<HTMLButtonElement>('#orb')!;
const canvas = document.querySelector<HTMLCanvasElement>('#core')!;
const status = document.querySelector<HTMLElement>('#status')!;
const panel = document.querySelector<HTMLElement>('#panel')!;
const reply = document.querySelector<HTMLElement>('#reply')!;
const confirmCard = document.querySelector<HTMLElement>('#confirm')!;
const confirmCmd = document.querySelector<HTMLElement>('#confirm-cmd')!;
const confirmReason = document.querySelector<HTMLElement>('#confirm-reason')!;
const confirmDeny = document.querySelector<HTMLButtonElement>('#confirm-deny')!;
const confirmAllow = document.querySelector<HTMLButtonElement>('#confirm-allow')!;

const labels: Record<string, string> = {
  idle: '待机',
  listening: '聆听中',
  thinking: '思考中',
  speaking: '正在回复',
  cancelled: '已取消',
  error: '需要处理',
};

const STATES: CoreState[] = ['idle', 'listening', 'thinking', 'speaking', 'cancelled', 'error'];

/** 手写渲染器现在只是**退路** —— 雪碧图缺失 / fetch 失败 / 内存不足时它接手。球是常驻
    挂件，「画不出来」不能等于「窗口里一片空白」。它直接绑主画布：不再需要与序列层交叉
    淡化（`thinking` 已改为走序列的高速段），所以离屏那一层没有理由存在。 */
const core = new CorollaBreath(canvas);
let state: CoreState = 'idle';
let amplitude = 0.35;
let lanes = 1;            // thinking 时在跑的 agent 路数(task.progress.agents.length)
let gated = false;        // 有命令待确认:光球停止呼吸与变形,收成一个规整的圆
/** 当前聚合度 0–1。活物不会瞬间张开或收拢,所以它向 bloomLevel() 逼近 */
let bloom = 0.52;
/** 当前成环度 0–1,向 ringLevel() 逼近。**必须插值** —— 使用者问「圆片的收缩
    为什么我在预览里看不到」,原因是这个量此前直接取目标值,变化发生在一帧之内。
    0.07/帧 ≈ 700ms:比聚合度的 400ms 慢一档,因为「一朵花散成一圈独立元素」是个
    比「张开一点」更大的动作,走快了同样读不出过程。 */
let ring = 0;
/** 聚合度的**速度**(弹簧的状态)。切态的性格全在它上面:过冲、回弹、缓落
    都是同一条弹簧在不同 k/d 下的行为,而不是六套关键帧。 */
let bloomVel = 0;
/** 逐句吐纳 0–1。每条 `tts.chunk`（真实事件，一句一条）置 1，之后按 0.055/帧 衰减
    ≈ 900ms 走完 —— 比一句话短，所以句子密时会连成一片起伏，句子长时一句一下。 */
let surge = 0;
/** 生长度 0–1。0 = 一个点。唤醒 0→1 约 350ms，一轮结束 1→0 约 700ms 再隐藏窗口。 */
let seed = 1;
/** 生长目标与速率。速率不同向：铺张比收回快一倍（醒得快、睡得慢）。 */
let seedTarget = 1;
/** 颜色渐变。切态时把旧色留在 `paletteFrom`，向新色 `paletteTo` 走 300ms。
    两个端点都从 CSS 读，所以颜色的唯一来源没变，只有中间帧由 JS 算。 */
let paletteFrom: Palette | null = null;
let paletteK = 1;
let retractTimer: number | null = null;
/** 一次性脉冲:0 无,1 满。点击的「收到」走它 —— 不新增状态,也不常驻 */
let pulse = 0;
let expandTimer: number | null = null;
let confirmPending: {cmd: string; reason?: string; settle: (v: boolean) => void} | null = null;


const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

/* 颜色的唯一来源是 CSS(见 style.css 的六态节)。自定义属性不参与插值,
   所以切态后立刻读到的就是新值;每帧读 getComputedStyle 太贵,只在切态时读一次。 */
function readPalette(): Palette {
  const s = getComputedStyle(app);
  const pick = (k: string, fb: string) => s.getPropertyValue(k).trim() || fb;
  return {
    core: pick('--lum-core', '#fff8ee'),
    mid: pick('--lum-mid', '#f0d3a0'),
    far: pick('--lum-far', '#7fb8b4'),
    alt: pick('--lum-alt', '#c8a882'),
    glass: pick('--glass', 'rgba(16,44,37,.20)'),
    edge: pick('--edge', 'rgba(8,30,25,.54)'),
  };
}
let palette: Palette = readPalette();

/** 当前帧。bloomLevel() 也吃它,所以聚合度目标与画出来的帧永远同源。 */
function coreFrame(): CoreFrame {
  // 颜色在切态后的 300ms 内是两套 CSS 色值之间的插值
  const pal = paletteFrom && paletteK < 1
    ? {
        core: blend(paletteFrom.core, palette.core, paletteK),
        mid: blend(paletteFrom.mid, palette.mid, paletteK),
        far: blend(paletteFrom.far, palette.far, paletteK),
        alt: blend(paletteFrom.alt, palette.alt, paletteK),
        glass: blend(paletteFrom.glass, palette.glass, paletteK),
        edge: blend(paletteFrom.edge, palette.edge, paletteK),
      }
    : palette;
  return {state, t, amplitude, lanes, gated, palette: pal, bloom, ring, surge, seed, pulse} satisfies CoreFrame;
}

/** 立刻画一帧。切态、闸门变化与 reduced-motion 都靠它,不必等下一个 rAF。
    动画停着时(reduced-motion)聚合度必须直接落到目标 —— 否则花冠会停在上一态的开度。 */
function drawCore(): void {
  resizeMain();
  if (reduceMotion.matches) {
    bloom = bloomLevel(coreFrame());
    ring = ringLevel(coreFrame());
    bloomVel = 0;
  }
  paintOrb();
}

/** 主画布的位图尺寸。以前这一步由 `core.resize()` 顺手做（手写渲染器当时绑在主画布上），
    现在它绑离屏了，主画布得自己管。沿用同一个口径：位图边长 = **球的布局盒** × DPR，
    而 CSS 上 `#core { inset:-22% }` 让它显示成 144% —— 位图比显示小是既有取舍（省算力，
    代价是外圈辉光略糊），换成按显示尺寸开位图会让每帧的填充面积涨一倍。 */
function resizeMain(): void {
  const px = Math.max(1, Math.round((orb.offsetWidth || 208) * (window.devicePixelRatio || 1)));
  if (canvas.width !== px) { canvas.width = px; canvas.height = px; }
}

/* ============ 渲染层：AE 预渲染序列，手写渲染器作为退路 ============
   为什么换：`core.ts` 在 Canvas 2D 里复刻 Element 3D 被否了六轮，根因是这条路上没有
   逐像素 UV、没有逐像素法向、没有 z-buffer、没有线性色空间，而素材那团光的质感全部
   来自这四样。资产由 `scripts/build_orb_assets.py` 从 AE（`aerender`）渲出的帧生成。

   **`core.ts` 不删** —— 雪碧图缺失、fetch 失败、内存不足都退回它。球是常驻挂件，
   「画不出来」不能等于「窗口里一片空白」。 */
let sheets: Sheets | null = null;
const motion = newMotion();

/** 契约态 → 渲染态。两处不是一一对应：
      · `idle` → `hidden`：**没有待机形态**（使用者定的）。球在 idle 时不画，随后窗口隐藏。
      · `gated` 不是契约里的态，是「有命令待确认」这个布尔量，它盖过当前态 —— 一道闸落下时
        球在等人，那比它原本在听还是在说更重要。 */
function mapSeq(): SeqState {
  if (gated) return 'gated';
  return state === 'idle' ? 'hidden' : (state as SeqState);
}

function syncSeq(): void {
  seqSetState(motion, mapSeq());
}

/** 画球。有序列就播序列，没有就退回手写渲染器。
    `appear` 由生产侧的 `seed`（生长/收回）驱动，不用序列层自己那份 —— 生长度是「一轮
    对话的生命周期」的一部分（idle 收回点、唤醒铺张），序列层看不到那条时间轴。 */
function paintOrb(): void {
  const ctx = canvas.getContext('2d');
  if (ctx === null) return;
  if (sheets === null) {
    // 退路：资产没就绪，只有手写层。**它也要守「没有待机形态」这条** —— `core.draw()` 的
    // idle 是一颗正常的球，不清的话它会在启动的那几百毫秒里闪一下使用者点名删掉的长相。
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (mapSeq() === 'hidden') core.clear(); else core.draw(coreFrame());
    return;
  }
  motion.appear = seed;
  drawOrb(ctx, sheets, motion, t, amplitude);
}

/** 闸门只由确认卡的出现/落定驱动:形态与颜色同时变,余光里就能看出球在等人。 */
function setGated(next: boolean): void {
  if (gated === next) return;
  gated = next;
  if (next) app.dataset.confirm = 'true';
  else delete app.dataset.confirm;
  palette = readPalette();
  syncSeq();
  drawCore();
}

/* ============ 状态切换 ============ */
function setState(next: string, amp = 0.35) {
  if (!STATES.includes(next as CoreState)) {
    console.warn(`[wake] invalid state: ${next}`);
    return;
  }
  const prev = state;
  state = next as CoreState;
  amplitude = Math.max(0.12, Math.min(1, amp));
  app.dataset.state = state;
  app.style.setProperty('--amplitude', String(amplitude));
  status.textContent = labels[state] ?? state;
  orb.setAttribute('aria-label', `Vox 状态: ${labels[state] ?? state}`);
  // 离开 thinking 就把路数收回 1:陈旧的路数会让下一轮的段数显示上一轮的路数
  if (state !== 'thinking') lanes = 1;
  // 颜色渐变:把旧色留成起点,再读新色。两端都来自 CSS。
  paletteFrom = palette;
  paletteK = 0;
  palette = readPalette();
  syncSeq();
  drawCore();

  /* ============ 生命周期 ============
     使用者定的:没有待机形态 —— idle 时隐藏窗口,命中唤醒后从一个点铺张为聆听,
     一轮结束停 3 秒再收回点、然后隐藏。所以「窗口可见」由状态驱动,不再由启动参数定。 */
  if (retractTimer) { clearTimeout(retractTimer); retractTimer = null; }
  if (state === 'idle') {
    // 停 3 秒让人看完,再 700ms 收回点,收完隐藏窗口
    retractTimer = window.setTimeout(() => {
      seedTarget = 0;
      retractTimer = window.setTimeout(() => {
        if (state === 'idle' && !confirmPending) bridgeInvoke('vox_set_visible', {visible: false});
      }, 760);
    }, 3000);
  } else {
    // 任何非 idle 状态都要看得见,并从当前生长度长回满
    seedTarget = 1;
    if (prev === 'idle') {
      // 从待机醒过来:确保从一个点开始铺张(而不是从上一轮残留的开度)
      seed = Math.min(seed, 0.06);
      bridgeInvoke('vox_set_visible', {visible: true});
    }
  }

  // 一次性动作：进入时放一次
  if (state === 'cancelled' && prev !== 'cancelled') {
    orb.classList.remove('collapsing');
    void orb.offsetWidth;
    orb.classList.add('collapsing');
    setTimeout(() => orb.classList.remove('collapsing'), 400);
  }
  if (state === 'error' && prev !== 'error') {
    orb.classList.remove('shaking');
    void orb.offsetWidth;
    orb.classList.add('shaking');
    setTimeout(() => orb.classList.remove('shaking'), 500);
  }
}

/** 派发中的 agent 路数。thinking 时光团分裂成几个就是它,所以它必须来自真实事件。 */
function setLanes(n: number): void {
  const next = Math.max(1, Math.min(4, Math.floor(n)));
  if (next === lanes) return;
  lanes = next;
  drawCore();
}

/* ============ 展开与收起 ============ */
function expand() {
  app.dataset.expanded = 'true';
  panel.setAttribute('aria-hidden', 'false');
  reportLayout();
  if (expandTimer) clearTimeout(expandTimer);
  expandTimer = window.setTimeout(() => {
    app.dataset.panelIn = 'true';
    reportLayout();
  }, 20);
}

function collapse() {
  app.dataset.panelIn = 'false';
  if (expandTimer) clearTimeout(expandTimer);
  expandTimer = window.setTimeout(() => {
    app.dataset.expanded = 'false';
    panel.setAttribute('aria-hidden', 'true');
    reportLayout();
  }, 300);
}

/* ============ 流式回复 ============ */
let replyText = '';
let caretNode: HTMLElement | null = null;

/** 回复文字是否显示。**默认关。** 后台开关：`VOX_SHOW_TEXT=1`(Rust 侧拼成
    `index.html?text=1`,与 `VOX_ORB_SIZE` 同一条路,不扩 IPC 面)。
    使用者的要求:平时不出文字,**只在报错、提示问题时才有文字**。
    关掉的时候文字仍然在 `replyText` 里累积(测试钩子与将来的历史面板要读它),
    只是不显示、也不把面板展开 —— 展开会让窗口凭空长大一块。 */
const SHOW_TEXT = new URLSearchParams(location.search).get('text') === '1';
/** 本轮是不是「有问题要讲」。`task.failed` / `tool.refused` 会把它置 true,
    此后这一轮的文字一律显示,不管总开关。回合结束时复位。 */
let problem = false;

/** 该不该让文字露出来。 */
function textVisible(): boolean {
  return SHOW_TEXT || problem;
}

function startReply() {
  replyText = '';
  reply.textContent = '';
  problem = false;
  if (caretNode) caretNode.remove();
  caretNode = document.createElement('span');
  caretNode.className = 'caret';
  reply.appendChild(caretNode);
  if (textVisible()) expand();
}

function appendReplyChunk(chunk: string) {
  replyText += chunk;
  if (caretNode && caretNode.parentNode) caretNode.remove();
  reply.textContent = textVisible() ? replyText : '';
  if (caretNode && textVisible()) reply.appendChild(caretNode);
  reply.scrollTop = reply.scrollHeight;
}

/** 报问题:把这一轮切成「要出文字」并立刻展开。走这条的只有 `task.failed`
    与 `tool.refused` —— 它们是**已经发生的坏事**,不出文字用户就只看到球变红。 */
function reportProblem(text: string) {
  problem = true;
  replyText = (replyText ? replyText + String.fromCharCode(10) : '') + text;
  reply.textContent = replyText;
  if (caretNode) caretNode.remove();
  caretNode = null;
  expand();
}

function endReply() {
  if (caretNode) caretNode.remove();
  caretNode = null;
}

function clearReply() {
  replyText = '';
  reply.textContent = '';
  if (caretNode) caretNode.remove();
  caretNode = null;
}

/* ============ 工具确认(FR-6.13) ============
   任何让确认卡消失的路径都必须 settle，且一律 settle 成 false。
   悬空的 Promise 会让调用方永久挂起，而「挂起」在安全语义上等价于「未拒绝」—— 不可接受。 */
function settlePending(decision: boolean) {
  if (!confirmPending) return;
  const p = confirmPending;
  confirmPending = null;
  p.settle(decision);
}

function showConfirm(cmd: string, reason?: string): Promise<boolean> {
  // 前一张卡被顶掉 = 用户没同意过它，按拒绝结算
  settlePending(false);

  return new Promise((resolve) => {
    let done = false;
    const settle = (v: boolean) => {
      if (done) return;
      done = true;
      confirmCard.hidden = true;
      setGated(false);           // 卡走了闸门就得开，任何落定路径都经过这里
      confirmAllow.onclick = null;
      confirmDeny.onclick = null;
      resolve(v);
    };

    confirmCmd.textContent = cmd;
    confirmCmd.scrollTop = 0;
    confirmReason.textContent = reason || '';
    confirmCard.hidden = false;
    // 确认卡是唯一「点错了会有后果」的界面(FR-6.13):它到达时**强制显示窗口**
    // 并打断自动收回 —— 一张看不见的确认卡等于让调用方永久挂起。
    if (retractTimer) { clearTimeout(retractTimer); retractTimer = null; }
    seedTarget = 1;
    bridgeInvoke('vox_set_visible', {visible: true});
    setGated(true);
    expand();

    confirmPending = {cmd, reason, settle};
    confirmAllow.onclick = () => settlePending(true);
    confirmDeny.onclick = () => settlePending(false);
    // 焦点默认落在「拒绝」上：回车的默认后果必须是不执行
    confirmDeny.focus({preventScroll: true});
  });
}

function hideConfirm() {
  settlePending(false);
  confirmCard.hidden = true;
  setGated(false);
}

/* ============ 几何上报 ============
   窗口该多大、哪块区域该吃鼠标，都由**前端量**出来告诉 Rust。
   理由：圆心与半径是 CSS 布局的结果，任何一次样式改动都会让 Rust 侧硬编码的坐标漂掉，
   而漂掉的表现是「球点不动」或「空白处点不下去」——两者都难排查。量一次比猜一次便宜。 */
type HitRegion = {
  width: number; height: number;
  circle: {cx: number; cy: number; r: number} | null;
  rects: {x: number; y: number; w: number; h: number}[];
};

let lastLayoutKey = '';
let lastRegion: HitRegion | null = null;

/* 取**布局盒**而不是 getBoundingClientRect()：
   球每帧都在被 rAF 写 translate+scale，用渲染盒会让命中区每帧抖动、IPC 每帧都发。
   offsetLeft/offsetWidth 不受 transform 影响，是稳定的。 */
function layoutBox(el: HTMLElement) {
  let x = 0, y = 0;
  let node: HTMLElement | null = el;
  while (node) {
    x += node.offsetLeft;
    y += node.offsetTop;
    node = node.offsetParent as HTMLElement | null;
  }
  return {x, y, w: el.offsetWidth, h: el.offsetHeight};
}

// 漂浮位移峰值 = 3.5+2+0.6 ≈ 6.1px，呼吸缩放 ≈ ±1.5%(≈1.1px)。留 8px 余量把动画全包住。
const FLOAT_MARGIN = 8;

function measureHitRegion(): HitRegion {
  const ob = layoutBox(orb);
  const rects: HitRegion['rects'] = [];
  // **不再参考 `#status`。** 状态读数已删（display:none），而 display:none 的元素
  // offsetParent 是 null、offsetTop/Height 全是 0 —— 拿它算出的 bottom 是 0，
  // 窗口高度因此退化成 16px。而命中判定的失败路径一律倒向「窗口吃鼠标」，
  // 表现就是一块尺寸不对的透明矩形挡住桌面（使用者报的「遮挡」）。
  let bottom = ob.y + ob.h;
  // **用球的固有宽度兜底，不能只用它的位置。** `#orbzone` 是 justify-self:center，
  // 一旦窗口比球窄，居中会让 offsetLeft 变成**负数**，于是 ob.x + ob.w 反而更小、
  // 反推出来的窗口更窄 —— 一个稳定但错误的不动点（实测锁死在 102×16）。
  let right = Math.max(ob.x + ob.w, 16 + ob.w);

  if (app.dataset.expanded === 'true') {
    for (const el of [reply, confirmCard]) {
      if (el.hidden || el.offsetParent === null) continue;
      const b = layoutBox(el);
      if (b.w < 1 || b.h < 1) continue;
      rects.push({x: b.x, y: b.y, w: b.w, h: b.h});
      bottom = Math.max(bottom, b.y + b.h);
      right = Math.max(right, b.x + b.w);
    }
    // 面板宽度受 max-width 限制，而 max-width 又永远够不着——窗口不先变宽，
    // 面板就永远只有当前窗口那么宽，反推出的宽度于是原地踏步。
    // 所以展开态直接按 CSS 上限要宽度，让窗口先长出来，高度下一帧再量。
    const cap = parseFloat(getComputedStyle(panel).maxWidth);
    if (Number.isFinite(cap)) right = Math.max(right, 16 + cap);
  }

  return {
    // 窗口尺寸 = 内容外接盒 + 右/下内边距
    width: Math.ceil(right + 16),
    height: Math.ceil(bottom + 16),
    circle: ob.w > 0
      ? {cx: ob.x + ob.w / 2, cy: ob.y + ob.h / 2, r: ob.w / 2 + FLOAT_MARGIN}
      : null,
    rects,
  };
}

function reportLayout(force = false) {
  const region = measureHitRegion();
  const key = JSON.stringify(region);
  if (!force && key === lastLayoutKey) return region;   // 不变就不喊，避免每帧过 IPC
  lastLayoutKey = key;
  lastRegion = region;
  bridgeInvoke('vox_report_layout', {region});
  return region;
}

/* Tauri 不在时（浏览器里跑 SIM 测试）静默降级，不抛异常 */
function bridgeInvoke(cmd: string, args: Record<string, unknown>) {
  const internals = (window as any).__TAURI_INTERNALS__;
  if (!internals || typeof internals.invoke !== 'function') return;
  try {
    internals.invoke(cmd, args);
  } catch (e) {
    console.warn(`[wake] invoke ${cmd} failed`, e);
  }
}

/* ============ 拖动 ============
   不用 `data-tauri-drag-region`：那条路要 `core:window:allow-start-dragging` 权限，
   等于为了拖窗口把整个 core:window 插件暴露给前端。自己包一个命令，IPC 面仍只有四个。

   4px 阈值而不是「按下即拖」—— 球是个按钮，按下即拖会让每次点击都把窗口蹭歪。 */
const DRAG_THRESHOLD = 4;
let dragOrigin: {x: number; y: number} | null = null;
let dragged = false;

orb.addEventListener('pointerdown', (ev) => {
  if (ev.button !== 0) return;
  dragOrigin = {x: ev.clientX, y: ev.clientY};
  dragged = false;      // 上一轮若残留 true，在这里清掉，不会波及本轮
});

window.addEventListener('pointermove', (ev) => {
  if (!dragOrigin || dragged) return;
  if (Math.hypot(ev.clientX - dragOrigin.x, ev.clientY - dragOrigin.y) < DRAG_THRESHOLD) return;
  dragged = true;
  // start_dragging 之后鼠标被 OS 接管，webview 收不到 pointerup，所以在这儿就地清账
  dragOrigin = null;
  bridgeInvoke('vox_start_drag', {});
});

window.addEventListener('pointerup', () => { dragOrigin = null; });

/* ============ 点击：脉冲 + squish ============ */
orb.addEventListener('click', (ev) => {
  // 拖完 OS 会补一个 click，要吞掉；但键盘激活的 click 的 detail 是 0，不能连它一起吞
  if (dragged && ev.detail > 0) { dragged = false; return; }
  dragged = false;

  // 一道从光团边缘向外扩散的薄环。这是这个球最直接的一句「收到」——
  // 一次性、有因果、不新增状态。上一代的 8 颗飞散粒子已删:它们不报告任何事。
  void pulseOnce(320);

  orb.classList.remove('squishing');
  void orb.offsetWidth;
  orb.style.transform = '';
  orb.classList.add('squishing');
  setTimeout(() => orb.classList.remove('squishing'), 300);

  // 事件桥接：点击可切态（测试用途）
  window.dispatchEvent(new CustomEvent('vox-orb-click', {detail: {state}}));
});

/* ============ 脉冲 ============
   球没有眼皮,也没有快门 —— 它是一团活的光,所以「收到」的反馈是从团边缘跑出去
   的一道薄光。**呼吸不在这里**:呼吸是常驻的,写在 core.ts 的 breathAmp/breathRate
   里,因为它是这个球「活着」的证据 —— 第五代以「不携带信息」为由删掉它是判断
   失误,对一个语音助手来说 presence(我还在这儿)本身就是要传达的状态。 */
function setPulse(v: number): void {
  pulse = v < 0 ? 0 : v > 1 ? 1 : v;
  if (!rafId) drawCore();   // 主循环停着时(reduced-motion / 隐藏)也要立刻反映
}

function pulseOnce(ms: number): Promise<void> {
  return new Promise((done) => {
    const t0 = performance.now();
    const step = () => {
      const dt = performance.now() - t0;
      if (dt >= ms) { setPulse(0); done(); return; }
      setPulse(1 - dt / ms);   // 线性衰减:扩散的环越远越淡,不需要缓动修饰
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

/* ============ 漂浮 ============
   只留两样,两样都有出处:
   ① 漂浮位移 —— 三条不同频率的正弦相加(周期互不整除,所以看不出循环)。它是
      「这颗球悬在桌面上、不是一张贴图」的证据,属于宪法第 4 条的物理可信度例外。
   ② 呼吸与形变 —— **不在这里**,它们在 core.ts 里(光团自己在呼吸、自己在变形),
      因为那才是「它活着」的载体。CSS 这一层不再做缩放:光团的呼吸已经把涨落表达
      掉了,外面再缩一层会变成两个不同步的呼吸。
   旋转与横向抖动已删:一个圆形的东西一旦在转,就会被读成加载指示器。 */
let t = 0;

function render() {
  const floatY = Math.sin(t * 1.3) * 3.2 + Math.sin(t * 0.67) * 1.8 + Math.sin(t * 2.3) * 0.5;

  if (!orb.classList.contains('squishing') && !orb.classList.contains('shaking')) {
    orb.style.transform = `translateY(${floatY.toFixed(2)}px)`;
  }

  t += 0.016;
  // 聚合度向目标逼近。0.12/帧 ≈ 400ms 走完 —— 花瓣张开是有质量的,
  // 而跳变读起来是「界面刷新了」,不是「它动了」。
  // **每态自己的弹簧**,不再是一条通用的指数逼近(见 core.ts 的 `bloomSpring`)。
  // 离散弹簧:速度先吃「离目标多远」再吃阻尼,位置再吃速度。进 listening/speaking
  // 允许过冲(醒过来、开口都有一股劲),进 cancelled/error/idle 一律不过冲。
  const sp = bloomSpring(state);
  bloomVel += (bloomLevel(coreFrame()) - bloom) * sp.k - bloomVel * sp.d;
  bloom += bloomVel;
  // 颜色渐变 300ms ≈ 19 帧
  if (paletteK < 1) paletteK = Math.min(1, paletteK + 1 / 19);
  // 逐句吐纳衰减
  if (surge > 0) surge = Math.max(0, surge - 0.055);
  // 生长/收回。铺张 350ms(≈0.046/帧)、收回 700ms(≈0.023/帧) —— 醒得快、睡得慢
  if (seed !== seedTarget) {
    const step = seedTarget > seed ? 0.046 : 0.023;
    seed += Math.sign(seedTarget - seed) * step;
    if (Math.abs(seedTarget - seed) < step) seed = seedTarget;
  }
  // 过冲不许把聚合度顶出量程:顶出去之后弹簧会从一个不可能的位置往回拉,
  // 表现是「先卡一下再动」。钳制的同时把速度吃掉,才不会贴着边界抖。
  if (bloom > 1) { bloom = 1; if (bloomVel > 0) bloomVel = 0; }
  if (bloom < 0) { bloom = 0; if (bloomVel < 0) bloomVel = 0; }
  // 合拢/散开同样是有过程的:进 thinking 时六片向同一朝向聚拢,离开时散回一朵花
  // 0.10/帧 ≈ 480ms:素材进出成环相实测只用 10–15 帧(0.30–0.45s),0.07 那一档
  // (700ms)比素材慢一半。**再加一次吸附**:指数逼近永远到不了 1,而差 0.6% 就等于
  // 「各片仍略有不同」,角距因此不完全相等 —— 稳态必须精确落在 0 或 1 上。
  const ringTarget = ringLevel(coreFrame());
  ring += (ringTarget - ring) * ringRate(ringTarget, ring);
  if (Math.abs(ringTarget - ring) < 0.004) ring = ringTarget;
  // 光团是每帧的主角。这里只 draw 不 resize：resize 要读 offsetWidth,
  // 每帧读会多一次强制重排,而尺寸只在切态/窗口变化时才可能变。
  stepMotion(motion, 0.016);   // 与 `t` 同步长；序列层的交叉淡化靠它推进
  paintOrb();

  // 面板高度随流式文本增长，每 6 帧量一次；reportLayout 内部按内容去重，不变就不过 IPC
  if ((frame = (frame + 1) % 6) === 0) reportLayout();
}
let frame = 0;

let rafId = 0;

function loop() {
  render();
  rafId = requestAnimationFrame(loop);
}

/* 降级(FR-6.6)是运行时可切的：用户改系统设置就该立刻生效，不能只在启动时读一次。
   停下来时留一帧静态代表帧 —— 半径、形变幅度、瓣数、团数、单侧拉扯五项本身就能
   分辨六态，静止不等于失去状态信息。 */
function startMotion() {
  if (rafId) return;
  rafId = requestAnimationFrame(loop);
}
function stopMotion() {
  if (rafId) cancelAnimationFrame(rafId);
  rafId = 0;
  drawCore();
}
/* 托盘上那个「动画：开 / 关」。默认开。
   **和系统的降级偏好是两个独立的关**，取与：任一侧要求静止就静止。反过来（托盘的开
   能压过 reduceMotion）会让一次菜单点击撤掉一项无障碍设置，而那个设置存在的理由不是
   审美。托盘那一侧只改自己这个布尔，不知道另一个开关的存在。 */
let trayAnimated = true;

function applyMotionPreference() {
  if (reduceMotion.matches || !trayAnimated) {
    stopMotion();
    setPulse(0);            // 别把脉冲停在某一帧上
  } else {
    startMotion();
  }
}

/* 托盘 -> 渲染层。**这一条不经 Python**（见 main.rs 的 "animation" 分支）：动画开关纯粹是
   渲染层的事，绕一趟父进程只会让「点了多久生效」取决于那一侧忙不忙。 */
window.addEventListener('vox-tray', (ev) => {
  const d = (ev as CustomEvent).detail ?? {};
  if (typeof d.animated === 'boolean') {
    trayAnimated = d.animated;
    applyMotionPreference();
  }
});

/* ============ 与后端的桥 ============
   后端（或 SIM 测试）只需派发 vox-voice-state 事件，前端不关心它从哪来。 */
window.addEventListener('vox-voice-state', (ev) => {
  const d = (ev as CustomEvent).detail ?? {};
  if (typeof d.state === 'string') setState(d.state, typeof d.amplitude === 'number' ? d.amplitude : 0.35);
});

window.addEventListener('vox-reply-start', () => startReply());
window.addEventListener('vox-reply-chunk', (ev) => {
  const d = (ev as CustomEvent).detail ?? {};
  if (typeof d.text === 'string') appendReplyChunk(d.text);
});
window.addEventListener('vox-reply-end', () => endReply());

/* 运行时显隐。此前可见性由 `VOX_WAKE_VISIBLE` 在启动时静态决定，隐藏后没有回来的路。
   隐藏前先收面板：下次显示时若停在展开态，会先闪一帧大窗口再缩回去。 */
function setVisible(visible: boolean) {
  if (!visible) {
    settlePending(false);        // 看不见的确认卡不能算「还在等你点」
    confirmCard.hidden = true;
    collapse();
  }
  bridgeInvoke('vox_set_visible', {visible});
}
window.addEventListener('vox-set-visible', (ev) => {
  const d = (ev as CustomEvent).detail ?? {};
  setVisible(d.visible !== false);
});

/* ============ 事件信封 -> 界面(P8 接线) ============
   Rust 侧把 stdin 收到的整行原样投成 `vox-bridge`，**不解析类型**。
   契约里加一种事件不该需要改 Rust，所以类型分派落在这里 —— UI 语义在的地方。

   只处理界面真的有位置显示的那几种。剩下的（memory.* / agent.* / tool.requested）
   有意不接：给它们编一个视觉表示，等于在界面上断言一件没测过的事。 */
type Envelope = {type?: unknown; id?: unknown; payload?: Record<string, unknown>};

function applyEnvelope(env: Envelope): void {
  const type = typeof env.type === 'string' ? env.type : '';
  const p = (env.payload ?? {}) as Record<string, unknown>;
  switch (type) {
    case 'state.changed':
      // 两种产出点的 payload 形状不同（状态机带 from/reason，插件带 running/paused），
      // 但 `to` 是两边都有的那一个键 —— 只读它。
      if (typeof p.to === 'string') setState(p.to);
      break;
    case 'turn.started':
      startReply();
      break;
    case 'task.progress':
      // 派发集合的**大小**驱动 thinking 的结重数。payload 里只有 agents 与 first_chunk_ms,
      // 没有获胜者;所以这里读的是「几路在跑」,不是「谁在答」——后者在这一层并不存在。
      if (Array.isArray(p.agents) && p.agents.length > 0) setLanes(p.agents.length);
      break;
    case 'tts.chunk':
      // 逐句吐纳:一句一条,真实事件。回复的「深」落在这儿而不是体积波动上。
      surge = 1;
      break;
    case 'llm.delta':
      if (typeof p.text === 'string') appendReplyChunk(p.text);
      break;
    case 'turn.done':
    case 'turn.cancelled':
      endReply();
      break;
    case 'task.failed':
      // 失败要看得见 —— 走 reportProblem 而不是普通文字流：**不管文字总开关**都显示。
      // payload 只有 error 与 task_id，没有正文可漏。
      if (typeof p.error === 'string') reportProblem(`[失败] ${p.error}`);
      endReply();
      break;
    case 'tool.refused':
      if (typeof p.reason === 'string') reportProblem(`[已拒绝] ${p.reason}`);
      break;
    case 'tool.confirm_required':
      askConfirm(env, p);
      break;
    default:
      break; // 未接的类型静默通过，不是错误
  }
}

/** 确认卡是唯一需要回话的事件：答复带着提问那条信封的 id 回到 Python。 */
function askConfirm(env: Envelope, p: Record<string, unknown>): void {
  const id = typeof env.id === 'string' ? env.id : '';
  const cmd = typeof p.command === 'string' ? p.command : '';
  if (!id || !cmd) return;   // 没有命令原文的确认卡不该出现(FR-6.13)
  // 契约里 tool.confirm_required 没有 reason 字段，所以这里写的是它真有的两个键，
  // 不替策略编一个理由。
  const origin = typeof p.origin === 'string' ? p.origin : '';
  const tool = typeof p.tool === 'string' ? p.tool : '';
  const reason = [tool, origin && `来自 ${origin}`].filter(Boolean).join(' · ');
  showConfirm(cmd, reason).then((approved) => {
    bridgeInvoke('vox_confirm_reply', {id, approved});
  });
}

window.addEventListener('vox-bridge', (ev) => {
  const msg = (ev as CustomEvent).detail ?? {};
  if (msg.kind === 'event' && msg.event && typeof msg.event === 'object') {
    applyEnvelope(msg.event as Envelope);
  } else if (msg.kind === 'visible') {
    setVisible(msg.visible !== false);
  }
});

/* ============ 键盘：Esc 优先拒绝确认，其次收起面板 ============ */
window.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Escape') return;
  if (confirmPending) {
    settlePending(false);      // 默认拒绝，不是默认执行
  } else if (app.dataset.expanded === 'true') {
    collapse();
  }
});

/* ============ 测试钩子 ============ */
Object.assign(window, {
  __READY__: true,
  render_state_to_text: () => JSON.stringify({state: app.dataset.state, label: status.textContent}),
  step: (_ms: number) => render(),
  setVoiceState: setState,
  // P8 新增，供 SIM 断言
  setExpanded: (v: boolean) => (v ? expand() : collapse()),
  showConfirm,
  hideConfirm,
  // P8 事件通道：SIM 测试直接喂信封，走的是真机同一条分派
  applyEnvelope,
  startReply,
  appendReplyChunk,
  endReply,
  clearReply,
  setVisible,
  // 派发路数：thinking 时游标被分成几段由它决定，SIM 可直接驱动
  setLanes,
  // 命中区域由前端量、Rust 消费；导出供 SIM 断言
  measureHitRegion,
  reportLayout: () => reportLayout(true),
  render_layout_to_text: () => JSON.stringify(lastRegion ?? measureHitRegion()),
  /* 花冠的几何指纹。全部无量纲或单位半径,所以指纹与球的像素尺寸无关;固定 t=1,
     所以「七态互不相同」是确定性可测的 —— 截图证明的是渲染,这里证明的是几何。
     **七个量都要报**,而且要逐对检查:
     `bloom` 是这一代的主量(聚合度),`petals` 是可数的第二重;
     `breath=0` 且 `spin=0` 是闸门独有的一帧(活物突然冻住了);
     `skew` 只有 error 非零;`blobs` 只有 thinking 会 >1;
     `hot` 是白热核半径 —— 它证明核确实长在 bloom 上,低聚合时为 0。 */
  render_core_to_text: () => {
    const f: CoreFrame = {state, t: 1, amplitude, lanes, gated, palette, bloom, pulse: 0};
    const target = bloomLevel(f);
    const probe = {...f, bloom: target};
    return JSON.stringify({
      state, lanes, gated,
      bloom: +target.toFixed(3),
      petals: petalCount(f),
      spin: +spinRate(f).toFixed(3),
      skew: +skew(f).toFixed(3),
      breath: +breathAmp(f).toFixed(3),
      rate: +breathRate(f).toFixed(2),
      blobs: blobCount(f),
      // 合拢度(第九代新增):thinking 是唯一非 0 的一态 —— 花瓣收拢成一叠亮片在转。
      // 素材实测这一相的角向主谐波在 45/47 帧上是 2 次(一片穿过球心),不是散开的花
      ring: +ringLevel(f).toFixed(2),
      // 液面轮廓的角向标准差(占半径的比例)。闸门时是 0 —— 它退回一个精确的圆,
      // 与「不呼吸、不自转」是同一句话的第三遍
      wobble: +(() => {
        const rs = contourRadii(probe, 96);
        const m = rs.reduce((s, v) => s + v, 0) / rs.length;
        return Math.sqrt(rs.reduce((s, v) => s + (v - m) ** 2, 0) / rs.length) / m;
      })().toFixed(4),
      // t=1 那一帧呼吸后的实际聚合度与核半径:证明呼吸真的在改几何
      at_t1: +bloomAt(probe).toFixed(4),
      hot: +coreGlow(probe).toFixed(4),
    });
  },
  render_panel_to_text: () => JSON.stringify({
    expanded: app.dataset.expanded === 'true',
    reply: replyText,
    confirm: confirmCard.hidden ? null : {cmd: confirmCmd.textContent, reason: confirmReason.textContent},
  }),
});

if (!new URLSearchParams(location.search).has('test')) {
  applyMotionPreference();
  reduceMotion.addEventListener('change', applyMotionPreference);
}

setState('idle');
drawCore();
reportLayout(true);

/* `?state=listening` 直接落在某一态上 —— 生产页平时是 idle（不画、随后隐藏窗口），
   所以不给一个入口的话，实机窗口上和 headless 取证都只能看到空白。与 `?orb=` 同一立场：
   走 URL 不新增 IPC 命令，而且只影响初始态，事件一到就被覆盖。 */
(() => {
  const p = new URLSearchParams(location.search);
  const s = p.get('state');
  if (s === null) return;
  setState(s, Number(p.get('amp') ?? 0.5));
  // 生长度直接拉满：`seed` 每帧只走 0.046，而 headless 取证的虚拟时间只跑几帧，
  // 不拉满的话球停在 appear≈0 上 —— 截图里什么都没有，看起来像渲染层坏了。
  if (s !== 'idle') { seed = 1; seedTarget = 1; }
  motion.w = 1;
  drawCore();
})();

/* 资产是异步的，而球必须立刻可见 —— 所以先用手写渲染器画着，序列到位再切过去。
   加载失败**不报错给用户**：退路已经在画了，弹一句「资产缺失」只会让人以为球坏了。
   控制台留一条，取证时看得到。 */
loadSheets('/orb')
  .then((s) => { sheets = s; syncSeq(); motion.w = 1; drawCore(); })
  .catch((e) => { console.warn('[orb] 序列资产未就绪，回退手写渲染器：', e); });

/* DPI 变化(拖到另一块屏)在 WebView2 上伴随 resize 到达：位图要跟着 DPR 重开，
   否则波形在高 DPI 屏上是被放大的糊线。 */
window.addEventListener('resize', () => {
  drawCore();
  reportLayout(true);
});
