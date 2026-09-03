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
  /** 循环的起始帧。见 `STABLE`。 */
  from: number;
  /** 循环的帧数。0 = 整段。 */
  span: number;
};

/* ── 稳定窗 ──────────────────────────────────────────────────────────────────
   **这是「思考像鬼畜」「颜色淡看不清」两条报告的共同解，而它是量出来的不是调出来的。**

   两张雪碧图逐帧量平均亮度（`max(R,G,B)` 的画面均值）：

   | 段 | 帧数 | 一个循环 | 亮度范围 | 摆动 | 均值 |
   |---|---|---|---|---|---|
   | flow 整段 | 64 | 2.67 s | 10 – 69 | **6.66×** | 33.7 |
   | burst 整段 | 28 | 1.17 s | 29 – 69 | **2.41×** | 54.0 |
   | **稳定窗** | 12 | 0.50 s | 56 – 66 | **1.17×** | **62.3** |

   两条结论：

   1. **思考用 burst 整段 = 每秒一次 2.41× 的亮度脉冲。** 那不是「在想」，那是频闪 ——
      使用者说的「跟鬼畜了一样」。上一轮把 rate 从 2.20 降到 1.15 只是把频闪放慢，脉冲还在，
      所以他重新构建之后说「依旧像鬼畜」。**降速改不掉一个亮度脉冲，只能不播那一段。**
   2. **聆听用 flow 整段 = 每 3.8 秒里有 1.4 秒球几乎是暗的**（帧 40–63 亮度平坦在 17，
      峰值是 69）。浅色桌面上那 1.4 秒就是「看不清」。

   稳定窗是 `burst[6..17]` ≡ `flow[10..21]`（两段出自同一次 AE 渲染，实测这两个窗逐帧
   完全相同）：亮度只摆 1.17×，**均值反而是整段的 1.85 倍**，而窗内相邻帧像素差 8.0 ——
   动是真的在动，只是不再忽明忽暗。

   接缝不用担心：首尾帧像素差 10.5，而窗内相邻帧最大差 10.2 —— 循环回头那一下**落在正常
   帧间步长的范围内**，看不出来。这是选这个窗而不是更长那个（`burst[6..23]`，接缝 12.3）
   的理由。

   代价说清楚：聆听不再有「涌起→消退」那条能量弧。那条弧本来是「在等」的表达，但它同时是
   球消失 1.4 秒的原因；而使用者对聆听的要求原话是「跟随真实音量和语句进行运动」——
   能量该来自**说话人的声音**（`amp`，已接线），不是来自一段罐头动画。

   **合成后实测**（`demo.html?state=…&big=1&light=1`，浅色桌面，从 canvas 直接读回像素，
   40+ 个采样点跨 2 秒；球体 = 半径 0.9 以内，环 = 半径 1.06–1.35）：

   | 态 | 球体亮度 | 逐时摆动 | 饱和度 | 球外环 |
   |---|---|---|---|---|
   | 聆听 | 83.9（73.8–90.4） | **1.23×** | 0.531 | 亮度 0，alpha **0.01** |
   | 思考 | 86.8（76.0–93.3） | **1.23×** | 0.519 | 亮度 0，alpha **0.02** |

   三条都对上了：摆动从素材整段的 2.41×/6.66× 落到 1.23×（不再脉动）、饱和度从素材的
   0.285 抬到 0.52（`SATURATE` 真的生效了，不是静默降级）、球外一圈 alpha ≈ 0（没有边边）。 */
const STABLE_FROM = { flow: 10, burst: 6 } as const;
const STABLE_SPAN = 12;

const LOOK: Record<SeqState, Look> = {
  hidden:    { sheet: 'flow',  rate: 0.00, scale: 0.00, breath: 0.000, breathHz: 0.00, gain: 0.00, tint: null, from: 0, span: 0 },
  // 聆听 = 稳定窗慢转。0.45 → 12 帧 / (24 × 0.45) = **1.11 秒一圈**，比原来 3.81 秒一圈快，
  // 但因为不再有那条 6.66× 的能量弧，读起来是「稳稳地在听」而不是「一波一波地喘」。
  listening: { sheet: 'flow',  rate: 0.45, scale: 0.96, breath: 0.030, breathHz: 1.57, gain: 1.00, tint: null, from: STABLE_FROM.flow, span: STABLE_SPAN },
  // 思考 = **同一个稳定窗播快**，不是换一段素材。使用者的定义是「那几个圆片转化为六个圆点
  // 绕球中心做圆周运动」+「他的思考态是很快速的一个过程」，而工程里这件事就是六片各自以
  // 不同角速度绕球心旋转（`预合成 1` 的 `time*80` / `time*45` / `-time*30`）。所以这一态
  // 靠**速率**表达，不换几何 —— 这一点没变。
  //
  // 变的是**播哪几帧**：0.62 → 12 帧 / (24 × 0.62) = **0.81 秒一圈**，久想之后最快
  // 0.65 秒一圈。听（1.11 s）与思（0.81 s）差得出来，而亮度全程稳在 56–66，
  // 那个每秒一次的 2.41× 脉冲没了。
  thinking:  { sheet: 'burst', rate: 0.62, scale: 0.97, breath: 0.022, breathHz: 2.40, gain: 1.02, tint: null, from: STABLE_FROM.burst, span: STABLE_SPAN },
  // 说话**保留整段**：那个 2.41× 的能量摆动在这里是想要的 —— 它是声音本身。使用者没有
  // 报过说话态的问题，而把它也换成稳定窗会把「在说」压成「在想」。
  speaking:  { sheet: 'burst', rate: 1.00, scale: 1.00, breath: 0.090, breathHz: 5.02, gain: 1.00, tint: null, from: 0, span: 0 },
  cancelled: { sheet: 'flow',  rate: 0.26, scale: 0.86, breath: 0.026, breathHz: 1.20, gain: 0.44, tint: null, from: STABLE_FROM.flow, span: STABLE_SPAN },
  // 染色走乘法，亮度会被乘掉一大档 —— `gain` 提到 1 以上是在补它，不是在「调亮一点」。
  // 素材是紫粉蓝（B 高 R 中 G 低），乘朱红 (226,58,46) 只剩 R×0.89 而 G/B 各剩 0.23/0.18，
  // 实测 gain 1.6 时球只有中心一小团高于可见阈值 —— 读作「球不见了」而不是「球变红了」。
  //
  // 这两态也用稳定窗：一条报错信息不该在自己的动画低谷里变得看不见。
  error:     { sheet: 'flow',  rate: 1.55, scale: 0.94, breath: 0.050, breathHz: 3.60, gain: 3.20, tint: '#E23A2E', from: STABLE_FROM.flow, span: STABLE_SPAN },
  gated:     { sheet: 'flow',  rate: 0.09, scale: 0.92, breath: 0.048, breathHz: 1.57, gain: 2.60, tint: '#D99A2B', from: STABLE_FROM.flow, span: STABLE_SPAN },
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

/** 思考越久，搅得越紧。**上限刻意很低** —— 见 `STABLE` 那段算术。
 *
 *  **导出是为了让读数页用同一份公式。** `demo.html` 那一栏此前自己写了
 *  `1 + 0.7 * min(1, thinkingFor / 6)` —— 一份和生产不同的加速曲线，于是读数页显示
 *  ×1.35 而生产在同一时刻是 ×1.25。「色值只有一个来源」这条规则同样适用于运动参数。 */
export function thinkingBoostOf(thinkingFor: number): number {
  return 1 + 0.25 * Math.min(1, thinkingFor / 8);
}

function thinkingBoost(m: Motion): number {
  return thinkingBoostOf(m.thinkingFor);
}

/** 当前该播第几帧。素材是 24fps；`rate` 是倍率。
 *
 *  `from` / `span` 把循环限制在一个窗里（见 `STABLE`）。`span <= 0` = 整段 ——
 *  说话态用的就是那条路。窗越界时**钳到整段而不是取模**：一个越界的窗说明配置写错了，
 *  而取模会让它悄悄变成另一个窗、渲出来是一段谁都没想要的动画。 */
export function frameAt(t: number, sheet: Sheet, rate: number, from = 0, span = 0): number {
  const total = Math.max(1, sheet.frames);
  const start = Math.floor(from);
  const width = Math.floor(span);
  const windowed = width > 0 && start >= 0 && start + width <= total;
  const base = windowed ? start : 0;
  const n = windowed ? width : total;
  return base + (((Math.floor(t * sheet.fps * rate) % n) + n) % n);
}

/** 呼吸后的直径倍率。载体是体积不是亮度 —— 这条承自第十代的实测。 */
export function scaleAt(t: number, look: Look, amp: number): number {
  return look.scale * (1 + look.breath * (0.55 + 0.45 * amp) * Math.sin(t * look.breathHz));
}

/* ── 画 ─────────────────────────────────────────────────────────────────── */

/** 染色用的离屏画布 —— 每帧新建 canvas 会让 GC 抖动，留一块复用。 */
let tintBuf: HTMLCanvasElement | null = null;

/** 饱和度倍率。**「颜色有点淡」的另一半是真的淡，不是被压暗的。**
 *
 *  量过素材：球体像素（亮度 > 30）的平均饱和度 flow 0.315 / burst 0.285，平均 RGB
 *  分别是 (83,102,113) 与 (109,95,105) —— 通道之间只差 30/255 ≈ 12%，那是**灰蓝**而不是
 *  注释里写的「紫粉蓝」。AE 那边六层高斯把色相摊平了，而这一层此前一个字都没管过它。
 *
 *  1.55 是保守值：再高中心那团白热光核会开始出现彩边（它已经接近 255，提饱和只能靠压低
 *  两个弱通道）。染色态（`tint`）不参与 —— 它们的色相是安全语义，不许被这一层动。 */
const SATURATE = 1.55;

/** `ctx.filter` 在这个渲染器上到底生效没有。**必须报出来，不能静默降级。**
 *
 *  Chromium 支持它（WebView2 就是 Chromium），但软件渲染或老 WebView 下它可能是个空操作，
 *  而那时球会**回到没提饱和的样子**并且哪里都不说 —— 上一轮 `'color'` 混合模式就是这么
 *  静默退化的（三个染色态渲出来一模一样）。`filterOk` 让 `describe` 那类调用方能看到它。 */
export let filterOk: boolean | null = null;

function useSaturate(ctx: CanvasRenderingContext2D, on: boolean): void {
  if (!('filter' in ctx)) {
    filterOk = false;
    return;
  }
  ctx.filter = on ? `saturate(${SATURATE})` : 'none';
  if (filterOk === null) filterOk = ctx.filter !== 'none' || !on;
}

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
  const k = frameAt(t, sheet, look.rate * rateMul, look.from, look.span);
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
  // 提饱和只对**素材本色**那几态做。染色态的色相是安全语义（朱红 = 出错、琥珀 = 待确认），
  // 不许被这一层动 —— 一个「稍微更红一点」的琥珀会开始像出错。
  useSaturate(ctx, look.tint === null);
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
  useSaturate(ctx, false);
}

/** 浅色桌面上唯一能产生对比的是减色 —— 球必须自带一层暗底，加色画不出边界。
    渐变必须**单调衰减**：把更不透明的一档放在外圈会得到一道暗环。

    **2026-09-03 两轮才对。** 第一版以 `R * 0.99`（几乎整张画布）为半径铺一层
    `rgba(8,11,20,0.16)`，而球体只占约 0.69 的画布半径 —— 球外面那一圈是一块没有任何东西
    盖住的灰，就是使用者在浅色背景上圈出来的「暗灰色的边边」。

    第二版把半径改成跟着球走（`base/2`）并在 **0.62** 处归零。边边没了，但那一刀砍过头了：
    量出来的球体亮度剖面（稳定窗，按半径分箱取均值）是

      r  0.0   0.2   0.4   0.5   0.6   0.7   0.8   0.9
      L  218   208   142   108    75    47    29     7

    亮度 > 25 一直到 **r = 0.92**。所以 0.62 之后那一大圈球身是**贴在裸桌面上的加色** ——
    白底上加色 ≈ 什么都没加，读作「颜色有点淡有点看不清」。使用者重新构建之后说「球的颜色
    依旧淡」，指的就是这一段。

    现在暗底一直铺到球体边缘（1.0）再归零，profile 跟着上面那条亮度剖面走：外圈的暗度必须
    小于素材在那里的加色量，否则又是一道环。核对（白底、素材加色 vs 暗底压暗）：

      r = 0.70  暗 0.09 → 压 ≈ 22 级，素材加 47   ✅
      r = 0.85  暗 0.03 → 压 ≈  7 级，素材加 ≈ 18 ✅
      r = 0.95  暗 0.01 → 压 ≈  2 级，素材加 ≈  4 ✅

    每一档都被盖住，所以有对比而没有环。 */
export function drawUnderlay(
  ctx: CanvasRenderingContext2D,
  cx: number, cy: number, r: number, k: number,
): void {
  if (k <= 0) return;
  const a = (value: number): string => `rgba(7,12,23,${(value * k).toFixed(4)})`;
  const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
  g.addColorStop(0.00, a(0.20));
  g.addColorStop(0.45, a(0.16));
  g.addColorStop(0.70, a(0.09));
  g.addColorStop(0.85, a(0.03));
  // 1.0 必须是全透明：这里就是球体的边缘，再往外是桌面。
  g.addColorStop(1.00, 'rgba(7,12,23,0)');
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
  // filter 也会泄漏（它和 globalAlpha 一样是 ctx 上的状态），而泄漏的 saturate 会让
  // 手写渲染器画出来的球跟着变色。清一次比在每个出口清便宜也更难漏。
  if ('filter' in ctx) ctx.filter = 'none';
  ctx.clearRect(0, 0, cv.width, cv.height);
  const cx = cv.width / 2, cy = cv.height / 2;
  const R = Math.min(cv.width, cv.height) / 2;
  if (m.appear <= 0 && m.state === 'hidden') return;

  const ease = m.appear * m.appear * (3 - 2 * m.appear);   // smoothstep：出现不要线性
  const base = R * 2 * (256 / 280) * (0.86 + 0.14 * ease);
  // **暗底跟着球体走，不铺满画布，而且要铺满球体。** `base` 是这一帧真正画出来的直径，
  // 所以 `base / 2` 就是球体半径。乘 1.02 留一点余量给素材自己的柔边；渐变在 1.0 处归零，
  // 所以最外圈不会有可见的灰。两个方向都踩过：铺满画布 → 边边；只铺到 0.62 → 颜色淡。
  drawUnderlay(ctx, cx, cy, (base / 2) * 1.02, ease);

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
