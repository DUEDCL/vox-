/* 唤醒球的序列渲染层 —— 播 AE 渲出来的帧，不在 Canvas 2D 里手写复刻。

   为什么换：`core.ts` 那条路被否了六轮，根因不是参数 —— Canvas 2D 没有逐像素 UV 采样、
   没有逐像素法向、没有 z-buffer、没有线性色空间，而素材那团光的质感全部来自这四样
   （Element 3D 的羽化 Alpha 贴图 + 球面法向明暗 + 深度雾 + 六层高斯）。使用者的判断：
   「不要重复造轮子了，直接用他渲染出的东西」。

   **黑底 + 加色，不用 alpha。** 雪碧图是黑底 RGB —— 加色（`lighter`）下「黑」= 不贡献
   ≡ alpha 0，两者在屏幕上等价，PNG 还小得多。浅色桌面上加色不产生对比，所以球下面那层
   暗底仍由 canvas 画，序列叠在它之上。

   **没有待机态。** 使用者 2026-08-31 的要求：「把待机状态概念彻底删除，现在就只有唤醒后
   直接聆听，后接思考最后是说」。所以 `hidden` 不是一个长相，是**不画** —— 球在未唤醒时
   不存在于屏幕上，没有「待机时轻轻呼吸」这回事。契约侧的六态没动（`VoiceState` 在字节
   冻结的 schema 里），这一层只决定「每个态画什么」。

   **听 / 思 / 说是同一条能量曲线的三段，不是三张图。**
     · 听 —— 球刚出现，片体在球内缓慢流动，中心不亮（还没有内容）。视觉是「在等」。
     · 思 —— 使用者的定义：「那几个圆片转换状态在球内部转圈的一个过程」。同一段序列**播快**，
       中心渐亮。视觉是「在忙」，而且可以持续加速（`thinkingRamp`）。
     · 说 —— 换到爆发段（素材帧 42–75，能量 47–59M、逐帧差异是稳态段的 5 倍）。呼吸幅度
       3.0 倍、频率 3.2 倍 —— 听与说是同一个动作的两档，这条承自第十代的实测。
   听→思只改速率（同一张图，天然平滑）；思→说要换图，所以必须**交叉淡化**，两层各拿
   `gain × 权重`，权重和恒为 1 ⇒ 过渡期总亮度守恒，不会鼓一下。

   **错误 / 待确认是打断，不在那条曲线上** —— 稳态段 + 语义色（朱红 / 琥珀）。这两个色
   不参与素材取样，是安全语义的红线。染色走 `'color'` 混合：保留形态明暗只换色相，直接
   叠一层半透明色会把素材的六色洗成一片。 */

/** 渲染态。`hidden` = 不画（没有待机态）。与契约六态的映射由调用方做，这一层不碰契约。 */
export type SeqState =
  | 'hidden' | 'listening' | 'thinking' | 'speaking' | 'cancelled' | 'error' | 'gated';

/** 画这一态用哪张雪碧图。两段都从同一次 AE 渲染里切出来，共用一个裁剪框。 */
export type SheetName = 'flow' | 'burst';

export type Sheet = {
  img: HTMLImageElement;
  cell: number;
  cols: number;
  rows: number;
  frames: number;
  fps: number;
};

/** 一态的长相。全是无量纲倍率，与像素尺寸无关，所以可断言。 */
export type Look = {
  sheet: SheetName;
  /** 播放速率倍率。1 = 素材原速。 */
  rate: number;
  /** 直径基准倍率（呼吸的中心值） */
  scale: number;
  /** 呼吸幅度（直径的 ± 比例） */
  breath: number;
  /** 呼吸频率 rad/s。1.57 是那条 4 秒心跳。 */
  breathHz: number;
  /** 亮度倍率 */
  gain: number;
  /** 安全语义色。null = 用素材本身的六色。 */
  tint: string | null;
};

const LOOK: Record<SeqState, Look> = {
  hidden:    { sheet: 'flow',  rate: 0.00, scale: 0.00, breath: 0.000, breathHz: 0.00, gain: 0.00, tint: null },
  listening: { sheet: 'flow',  rate: 0.70, scale: 0.96, breath: 0.030, breathHz: 1.57, gain: 0.95, tint: null },
  // 思考 = **快速搅动**，不是「六个点绕环」。使用者的定义是「那几个圆片转化为六个圆点绕
  // 球中心做圆周运动」+「他的思考态是很快速的一个过程」，而工程里这件事的实现就是六片各自
  // 以不同角速度绕球心旋转（`预合成 1` 的表达式 `time*80` / `time*45` / `-time*30`…），
  // 叠上辉光之后视觉上是一团光在搅。**我量过三遍确认没有「六个分离的点等分在一圈上」那一相**：
  // `预合成 3` 逐帧 0–205、`合成 1` 全 30 秒抽样、`预合成 1` 全 30 秒抽样，环带角向峰数
  // 最多 4–5 个且起伏只有均值的 17%。所以这一态靠**速率**表达，不换几何。
  // 用 `burst` 而不是 `flow`：flow 段含一次完整的涌起→消退，播快之后会落在能量低谷上，
  // 渲出来像「内容被删掉了」—— 那正是上一版被否的原因。burst 段能量摆动只有 2.38×，稳。
  thinking:  { sheet: 'burst', rate: 2.20, scale: 0.97, breath: 0.022, breathHz: 2.40, gain: 0.98, tint: null },
  speaking:  { sheet: 'burst', rate: 1.00, scale: 1.00, breath: 0.090, breathHz: 5.02, gain: 1.00, tint: null },
  cancelled: { sheet: 'flow',  rate: 0.26, scale: 0.86, breath: 0.026, breathHz: 1.20, gain: 0.44, tint: null },
  // 染色走乘法，亮度会被乘掉一大档 —— `gain` 提到 1 以上是在补它，不是在「调亮一点」。
  // 素材是紫粉蓝（B 高 R 中 G 低），乘朱红 (226,58,46) 只剩 R×0.89 而 G/B 各剩 0.23/0.18，
  // 实测 gain 1.6 时球只有中心一小团高于可见阈值 —— 读作「球不见了」而不是「球变红了」。
  error:     { sheet: 'flow',  rate: 1.55, scale: 0.94, breath: 0.050, breathHz: 3.60, gain: 3.20, tint: '#E23A2E' },
  gated:     { sheet: 'flow',  rate: 0.09, scale: 0.92, breath: 0.048, breathHz: 1.57, gain: 2.60, tint: '#D99A2B' },
};

export function lookOf(state: SeqState): Look {
  return LOOK[state] ?? LOOK.hidden;
}

/** 加载一张雪碧图。元数据由 `scripts/build_orb_assets.py` 一起写出，不在这边猜格数。 */
export async function loadSheet(base: string, name: SheetName): Promise<Sheet> {
  const meta = await fetch(`${base}/${name}.json`).then((r) => {
    if (!r.ok) throw new Error(`meta ${name}: HTTP ${r.status}`);
    return r.json() as Promise<{ cell: number; cols: number; rows: number; frames: number; fps: number }>;
  });
  const img = await new Promise<HTMLImageElement>((resolve, reject) => {
    const el = new Image();
    el.onload = () => resolve(el);
    el.onerror = () => reject(new Error(`sheet ${name}: image load failed`));
    el.src = `${base}/${name}.png`;
  });
  return { img, ...meta };
}

export type Sheets = Record<'flow' | 'burst', Sheet>;

export async function loadSheets(base = '/orb'): Promise<Sheets> {
  const [flow, burst] = await Promise.all([loadSheet(base, 'flow'), loadSheet(base, 'burst')]);
  return { flow, burst };
}

/* ── 态机 ────────────────────────────────────────────────────────────────────
   过渡是这一层唯一的状态。跳变会把「一条能量曲线」读成「换了张图」，所以每次切态都走
   一段交叉淡化；同一张雪碧图之间（听↔思）淡化的是速率与亮度，换图（思→说）淡化的是两层
   的权重。**播放相位不重置** —— 两个 sheet 各自用同一个 `t` 算帧号，所以换图时片体的
   流动是接着走的，不会回到序列开头。 */

/** 切态的淡化时长（秒）。0.34 是实测能盖住换图接缝的最短值，再短会看见「闪一下」。 */
const FADE = 0.34;

export type Motion = {
  state: SeqState;
  prev: SeqState;
  /** 0 → 1 的淡化进度 */
  w: number;
  /** 思考持续时长（秒），用来让转圈越来越快 —— 「在忙」是可以升级的 */
  thinkingFor: number;
  /** 出现动画进度 0 → 1（唤醒那一下），`hidden` 时归零 */
  appear: number;
};

export function newMotion(): Motion {
  return { state: 'hidden', prev: 'hidden', w: 1, thinkingFor: 0, appear: 0 };
}

export function setState(m: Motion, next: SeqState): void {
  if (next === m.state) return;
  // 淡化中途再切：把当前的混合结果当作新的起点，否则会看见它先淡回去再淡过来
  m.prev = m.w < 1 ? m.prev : m.state;
  m.state = next;
  m.w = 0;
  if (next !== 'thinking') m.thinkingFor = 0;
}

export function stepMotion(m: Motion, dt: number): void {
  m.w = Math.min(1, m.w + dt / FADE);
  if (m.state === 'thinking') m.thinkingFor += dt;
  const rising = m.state !== 'hidden';
  // 出现比消失快：唤醒要「立刻在那儿」，收起可以从容
  m.appear = Math.min(1, Math.max(0, m.appear + dt / (rising ? 0.28 : -0.42)));
}

/** 思考越久转得越快 —— 上限 1.7 倍，到顶要 6 秒。「在忙」是可以升级的，但不能无限升。 */
function thinkingBoost(m: Motion): number {
  return 1 + 0.7 * Math.min(1, m.thinkingFor / 6);
}

/** 当前该播第几帧。素材是 24fps；`rate` 是倍率。 */
export function frameAt(t: number, sheet: Sheet, rate: number): number {
  const n = Math.max(1, sheet.frames);
  return ((Math.floor(t * sheet.fps * rate) % n) + n) % n;
}

/** 呼吸后的直径倍率。载体是体积不是亮度 —— 这条承自第十代的实测。 */
export function scaleAt(t: number, look: Look, amp: number): number {
  return look.scale * (1 + look.breath * (0.55 + 0.45 * amp) * Math.sin(t * look.breathHz));
}

/* ── 画 ─────────────────────────────────────────────────────────────────── */

/** 染色用的离屏画布 —— 每帧新建 canvas 会让 GC 抖动，留一块复用。 */
let tintBuf: HTMLCanvasElement | null = null;

/** 把某态的一帧叠上去。`weight` 是交叉淡化的权重，两层的权重和恒为 1。 */
function layer(
  ctx: CanvasRenderingContext2D,
  sheets: Sheets,
  state: SeqState,
  t: number, cx: number, cy: number, base: number,
  amp: number, weight: number, rateMul: number,
): void {
  const look = lookOf(state);
  if (look.gain <= 0 || weight <= 0) return;
  const sheet = sheets[look.sheet];
  const c = sheet.cell;
  const k = frameAt(t, sheet, look.rate * rateMul);
  const sx = (k % sheet.cols) * c;
  const sy = Math.floor(k / sheet.cols) * c;

  let src: CanvasImageSource = sheet.img;
  let ssx = sx, ssy = sy;
  if (look.tint !== null) {
    if (tintBuf === null) tintBuf = document.createElement('canvas');
    if (tintBuf.width !== c) { tintBuf.width = c; tintBuf.height = c; }
    const b = tintBuf.getContext('2d');
    if (b !== null) {
      b.globalCompositeOperation = 'source-over';
      b.clearRect(0, 0, c, c);
      b.drawImage(sheet.img, sx, sy, c, c, 0, 0, c, c);
      // **用 `'multiply'` 不用 `'color'`** —— `'color'`（保底的明暗、换顶的色相）在概念上
      // 正是想要的，但它在 headless / 软件渲染下静默退化，实测三个态染出来一模一样，
      // 而「静默一样」比「颜色不够准」糟得多。乘法一定生效：素材是紫粉蓝，乘朱红只剩红
      // 通道、乘琥珀偏黄。亮度被乘掉的那一档由 `gain` 补回来（见 LOOK 表）。
      b.globalCompositeOperation = 'multiply';
      b.fillStyle = look.tint;
      b.fillRect(0, 0, c, c);
      // **必须把 alpha 收回来。** `multiply` 走的仍是 source-over 的覆盖规则，所以整格的
      // `fillRect` 会在**透明区域**留下不透明的纯色 —— 渲出来是一整块朱红 / 琥珀的方块，
      // 球彻底不见了。`destination-in` 用原帧的 alpha 再裁一次，形状就回来了。
      b.globalCompositeOperation = 'destination-in';
      b.drawImage(sheet.img, sx, sy, c, c, 0, 0, c, c);
      b.globalCompositeOperation = 'source-over';
      src = tintBuf; ssx = 0; ssy = 0;
    }
  }

  const d = base * scaleAt(t, look, amp);
  ctx.globalCompositeOperation = 'lighter';   // 黑底 ≡ 零 alpha
  // **亮度大于 1 必须画多遍，不能靠 globalAlpha** —— 它的上限是 1，`gain: 1.6` 会被静默
  // clamp 成 1.0，于是「染色后补亮度」这一步等于没写（染完的球比原来暗一档，读作「球不见了」）。
  // 加色下重画一遍就是亮度 ×2，所以整数部分画整遍、小数部分画一遍。
  let left = Math.max(0, look.gain * weight);
  const x0 = cx - d / 2, y0 = cy - d / 2;
  while (left > 0) {
    ctx.globalAlpha = Math.min(1, left);
    ctx.drawImage(src, ssx, ssy, c, c, x0, y0, d, d);
    left -= 1;
  }
}

/** 浅色桌面上唯一能产生对比的是减色 —— 球必须自带一层暗底，加色画不出边界。
    渐变必须**单调衰减**：把更不透明的一档放在外圈会得到一道暗环。 */
export function drawUnderlay(
  ctx: CanvasRenderingContext2D,
  cx: number, cy: number, r: number, k: number,
): void {
  if (k <= 0) return;
  const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
  g.addColorStop(0, `rgba(8,11,20,${(0.16 * k).toFixed(4)})`);
  g.addColorStop(0.72, `rgba(7,12,23,${(0.10 * k).toFixed(4)})`);
  g.addColorStop(1, 'rgba(7,12,23,0)');
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  ctx.fillStyle = g;
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fill();
}

/** 画一帧。`amp` 是实时音量 0–1（序列里的形变是固定的，音量只能走呼吸与亮度）。

    直径按 `cell/280` 归一：格子是 256 而生产的球本体是 140px @2× = 280 物理像素，
    所以序列要放大 1.09 倍才对上球的可见半径。 */
export function drawOrb(
  ctx: CanvasRenderingContext2D,
  sheets: Sheets,
  m: Motion,
  t: number,
  amp = 0,
): void {
  const cv = ctx.canvas;
  // **必须先复位变换。** 这个 ctx 与手写渲染器（`core.ts`）共用一张 canvas，而那边
  // `draw()` 里做过 `translate(half, half)` 且不复位 —— 变换泄漏过来之后 `clearRect`
  // 只清到右下 1/4、球也被平移出画布大半，屏幕上是「手写球 + 右下一个黑方块」。
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  ctx.clearRect(0, 0, cv.width, cv.height);
  const cx = cv.width / 2, cy = cv.height / 2;
  const R = Math.min(cv.width, cv.height) / 2;
  if (m.appear <= 0 && m.state === 'hidden') return;

  const ease = m.appear * m.appear * (3 - 2 * m.appear);   // smoothstep：出现不要线性
  drawUnderlay(ctx, cx, cy, R * 0.99, ease);

  const base = R * 2 * (256 / 280) * (0.86 + 0.14 * ease);
  const boost = m.state === 'thinking' ? thinkingBoost(m) : 1;
  ctx.save();
  if (m.w < 1 && m.prev !== m.state) {
    layer(ctx, sheets, m.prev, t, cx, cy, base, amp, (1 - m.w) * ease, 1);
    layer(ctx, sheets, m.state, t, cx, cy, base, amp, m.w * ease, boost);
  } else {
    layer(ctx, sheets, m.state, t, cx, cy, base, amp, ease, boost);
  }
  ctx.restore();
}
