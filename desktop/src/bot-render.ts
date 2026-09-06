/* 唤醒球的第十二代渲染层 —— bloub 引擎（`src/bot/`）画到 canvas 上。

   为什么换：前十一代都在**画一团光**（花瓣 / 薯片 / AE 雪碧图），而一团光没有注意力方向。
   bloub 是一个有脸的实体，它的 14 个态里有 5 个几乎正好是 Vox 需要的（睁大眼在听、三点
   在想、感叹号出错、球带一个点在等确认、缩成小点被取消）。引擎是 2837 行纯 TS、零外部
   依赖、`sample(t)` 是时间的纯函数 —— 所以几何又变成可断言的了，这是雪碧图那条路丢掉的。

   **不引 Vue，不引 SVG DOM。** 上游的 `BloubBot.vue` 用 Vue 模板生成 SVG，但引擎吐的是
   纯数据：`bodyPath` 是 `M`+`C`+`Z`、眼睛是 `M`+`A`+`L`+`Z`、环是 `M`+`L` 折线，全部在
   `new Path2D(d)` 的支持范围内，环的渐变是两点线性 + 3 个 stop 直译成 `createLinearGradient`。
   所以现有的 `#core` canvas、`resizeMain()`、`measureHitRegion()` 那套接线一个字不用动。

   **眼睛是洞，不是叠上去的白色形状。** 这是上游的架构约束，理由是洞会被球的轮廓自动裁剪
   —— 眼睛滑到边缘时不需要任何裁剪代码。canvas 上的落法是三步，第二步是关键：

     ① 离屏：填 ink 的 bodyPath → `destination-out` 挖掉眼睛（带 alpha，所以半透明挖 =
        半透明洞，眼睛转到球侧面时的淡出得以保留）
     ② 主画布：**用同一个 bodyPath 填眼睛色** —— 眼睛色因此只出现在「球轮廓内 且 被挖空」
        的地方，天然被轮廓裁剪，零额外几何。这正是上游在球下面压一层 `paper` 的那一步。
     ③ drawImage(离屏) 盖上去

   **洞不能真透出桌面。** 上游填的是页面背景色，而球是透明置顶窗口 —— 填透明的话深色壁纸
   上就是「黑球 + 黑洞」，眼睛消失。所以 `EYE` 是钉死的常量，不跟随桌面。

   **球色不能用素材的纯黑。** #0a0a0c 在深色桌面上看不见。`INK` 取 bloub 自带 12 色里的
   turquoise：相对亮度 0.42 是有彩色里最接近中值的一档（深浅两底都有对比），色相离朱红与
   琥珀两个安全语义色最远，而且避开了「语音助手一律是蓝的」那个俗套。 */

import { BotEngine, type BotFrame } from './bot/engine';
import { DEMI_VIEWBOX, RAYON } from './bot/repere';
import type { StateId } from './bot/states';

/** 渲染态。与 `sequence.ts` 的 `SeqState` 同名同义，好让调用方切换渲染器时不改映射。 */
export type BotState =
  | 'hidden' | 'listening' | 'thinking' | 'speaking' | 'cancelled' | 'error' | 'gated';

/* ── 颜色。唯一来源就是这里，不要在别处再抄一份（本仓库这条规则已经被违反过一次）。 ── */

/** 球体。turquoise，见文件头。 */
const INK = '#2fbfa0';
/** 眼睛的洞里填什么。creme，暖白，与 INK 的亮度差 0.42→0.92 够读。 */
const EYE = '#f1efe9';
/** 出错。与旧渲染层的 `error` 同色，安全语义不参与换代。 */
const VERMILION = '#E23A2E';
/** 待确认。与确认卡顶那条琥珀斜纹带、与旧渲染层的 `gated` 同色。 */
const AMBER = '#D99A2B';

/** 每态的球色。`null` = 用 INK。 */
const TINT: Partial<Record<BotState, string>> = {
  error: VERMILION,
  gated: INK,          // 球本身不染色 —— 琥珀在那个点上，见 `notifColor`
  cancelled: '#6b8f86' // 光垮了：同色相压暗去饱和，不换色相（换了会读成另一种告警）
};

/** `notify` 那颗点的颜色。上游默认蓝，这里是「有一件事等你」，所以走琥珀。 */
const notifColor = (s: BotState): string => (s === 'gated' ? AMBER : '#3b93f0');

/* ── 契约态 → bloub 态 ──────────────────────────────────────────────────────
   五个是现成的，`speaking` 是发明出来的：bloub 的 14 个态里没有「在说话」，而它没有嘴。
   落法是 `idle` 的基形 + 音量驱动的体积起伏（见 `squashOf`）—— 于是 listening 与 speaking
   在**静止一帧**上也分得开（眼睛大小），动起来更分得开（起不起伏），FR-6.6 成立。

   `hidden` 不在表里：它不是一个长相，是**不画**。使用者 2026-08-31 的要求「把待机状态概念
   彻底删除」在这一代继续有效，判据也没变（`index.html?state=idle` 画布亮像素为 0）。
   注意 bloub 自己也有一个叫 `idle` 的态，它在这里只作为 speaking 的基形出现 —— 两个 idle
   不是一回事，别在维护时把它们合起来。 */
const STATE_MAP: Record<Exclude<BotState, 'hidden'>, StateId> = {
  listening: 'wide',      // 睁大眼
  thinking: 'thinking',   // 三点从左到右脉冲
  speaking: 'idle',       // 普通眼 + 音量起伏
  cancelled: 'sleep',     // 垮缩成一个小点
  error: 'alert',         // 斜感叹号横穿球体 + 2.5 Hz 震动
  gated: 'notify'         // 球 + 右上一个点，眼睛看向点的反方向
};

/** 画面半边，viewBox 单位（球半径 = RAYON = 100）。

    **不是上游的 `DEMI_VIEWBOX`（158）。** 那个余量是给 `orbit`/`comet` 的环留的，而这一代
    用到的六个态一个都不带 arcs —— 最外的东西是 `notify` 那颗点和 `thinking` 的侧点，都在
    1.2 个半径内。按 158 铺满画布的话球本体只剩显示宽的 63%（140px 的盒子里球只有 88px、
    眼睛 8px 宽），偏小到读不出表情。130 让球占 77%（约 108px，眼睛 10×22px）。
    **要是哪天启用 `orbit` 表达多路 thinking，这个数必须改回 158，否则环会被裁掉。** */
const VIEW_HALF = 130;

/** 说话时球的纵向起伏。**这是 speaking 唯一的动态签名**，所以它必须比引擎自带的呼吸
    （`liveliness().breath`，±0.5%）大一个数量级才读得出来。频率沿用旧渲染层 speaking 那档
    5.02 rad/s —— 听与说是同一个动作的两档，这条承自第十代的实测，换代不换结论。

    只有 speaking 吃它。其余态返回 1，把起伏留给引擎自己那 ±0.5%（那是「活着」不是「在说」）。 */
function squashOf(state: BotState, amp: number, t: number): number {
  if (state !== 'speaking') return 1;
  return 1 + amp * 0.14 * Math.sin(t * 5.02);
}

/** `matrix(a,b,c,d,e,f)` → 六个数。引擎为 SVG 吐的字符串，canvas 侧要还原成 DOMMatrix
    才能喂 `Path2D.addPath`。不改引擎的输出格式：那是上游的公开形状，改了就得跟着上游走。 */
function parseMatrix(m: string): DOMMatrix2DInit {
  const n = m.replace(/[^-0-9.,e]/gi, '').split(',').map(Number);
  return { a: n[0] ?? 1, b: n[1] ?? 0, c: n[2] ?? 0, d: n[3] ?? 1, e: n[4] ?? 0, f: n[5] ?? 0 };
}

export class BotRenderer {
  private readonly engine = new BotEngine(RAYON, 'sleep');
  private state: BotState = 'hidden';
  /** 挖洞用的离屏画布。每帧新建会让 GC 抖动，留一块复用（`tintBuf` 同款做法）。 */
  private off: HTMLCanvasElement | null = null;

  get current(): BotState {
    return this.state;
  }

  /** 切态。引擎自己做态间 morph（`blendPose` + 各态自己的 `morph` 时长），所以这一层
      不需要交叉淡化 —— 那是雪碧图那条路才需要的，因为两张位图之间没有中间形态。

      **从 hidden 出来时先把引擎按回 `sleep`**，让球用引擎自己的半径插值从一个点长成目标态。
      不这么做的话 hidden 期间引擎的 morph 早已走完，再显示就是「目标姿态突然出现」，
      只剩外层 `appear` 那个缩放在演「长出来」。 */
  setState(next: BotState, now: number): void {
    if (next === this.state) return;
    const from = this.state;
    this.state = next;
    if (next === 'hidden') return;
    if (from === 'hidden') this.engine.reset('sleep', now);
    this.engine.setState(STATE_MAP[next], now);
  }

  /** 跳过入场 morph，并把状态自己的动画时间**对齐到绝对时间**。

      两件事都必须做，而只做前一件会静默画错。`reset(id, now)` 会把状态起点设在 `now`，
      于是 `sample(now)` 拿到的 `local` 是 **0** —— 每个态都停在自己的第一帧上。实测代价：
      `thinking` 的 `emerge` 在 local=0 时是 0.3，三个点只张开到 ±0.167 半径而不是 ±0.557，
      渲出来是两团挤在一起的光斑而不是三个分开的点，而读数（态、点数、颜色）**全是对的**。
      所以起点固定在 0，`local` 就等于取证给的那个 `t`，同一个 URL 每次渲同一帧。

      **静态取证必须调它。** 不调的话 ratio 为 0，`blendPose` 返回的是**上一个态** ——
      从 hidden 出来时那是 `sleep`，截图里只有一个直径十几像素的点（实测 962 个非透明像素，
      该有约 36000）。序列层那句 `motion.w = 1` 修的是同一件事的前半。 */
  settle(): void {
    if (this.state === 'hidden') return;
    this.engine.reset(STATE_MAP[this.state], 0);
  }

  /** 清空。`hidden` 走这里而不是画一个「待机的球」—— 判据是画布亮像素为 0。 */
  clear(ctx: CanvasRenderingContext2D): void {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
  }

  /** 挖洞用的离屏画布，按主画布尺寸复用。 */
  private offscreen(w: number, h: number): CanvasRenderingContext2D | null {
    if (this.off === null) this.off = document.createElement('canvas');
    if (this.off.width !== w || this.off.height !== h) {
      this.off.width = w;
      this.off.height = h;
    }
    const c = this.off.getContext('2d');
    if (c === null) return null;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.globalCompositeOperation = 'source-over';
    c.globalAlpha = 1;
    c.clearRect(0, 0, w, h);
    return c;
  }

  /** 画一帧。`appear` 是出现进度 0→1（由生产侧的 seed 驱动，不是这一层自己的动画），
      `amp` 是实时音量 0–1。 */
  draw(ctx: CanvasRenderingContext2D, now: number, appear = 1, amp = 0): void {
    this.clear(ctx);
    if (this.state === 'hidden' || appear <= 0) return;

    const cv = ctx.canvas;
    const f = this.engine.sample(now);
    const ease = appear * appear * (3 - 2 * appear);   // smoothstep：出现不要线性
    const k = (Math.min(cv.width, cv.height) / 2 / VIEW_HALF) * (0.86 + 0.14 * ease);
    const squash = squashOf(this.state, amp, now);
    const cx = cv.width / 2;
    const cy = cv.height / 2;
    const ink = TINT[this.state] ?? INK;
    const alpha = ease * f.bodyAlpha;
    /** viewBox 单位 → 像素。压扁只作用在 y 上，所以 speaking 的起伏是体积而不是位移。 */
    const place = (c: CanvasRenderingContext2D): void => {
      c.setTransform(k, 0, 0, k * squash, cx, cy);
    };
    const body = new Path2D(f.bodyPath);

    // ① 离屏：整只球，然后按每只眼睛自己的 alpha 挖洞。半透明挖 = 半透明洞，所以眼睛
    //    转到球侧面时的淡出（`e.depth` 驱动）保留了下来 —— 这是上游用 `<mask>` 的 opacity
    //    表达的东西，`evenodd` 填充规则做不到（洞是全有或全无）。
    const off = this.offscreen(cv.width, cv.height);
    if (off === null) return;
    place(off);
    off.fillStyle = ink;
    off.fill(body);
    off.globalCompositeOperation = 'destination-out';
    for (const eye of f.eyes) {
      const p = new Path2D();
      p.addPath(new Path2D(eye.d), parseMatrix(eye.matrix));
      off.globalAlpha = Math.max(0, Math.min(1, eye.alpha));
      off.fill(p);
    }
    if (f.notch !== null) {
      off.globalAlpha = 1;
      const n = new Path2D();
      n.arc(f.notch.x, f.notch.y, f.notch.r, 0, Math.PI * 2);
      off.fill(n);
    }

    // ② 主画布，按 SVG 那边的 z 序合成。
    const arc = (d: string, a: BotFrame['arcs'][number]): void => {
      if (d === '') return;
      const g = ctx.createLinearGradient(a.grad.x1, a.grad.y1, a.grad.x2, a.grad.y2);
      const last = Math.max(1, a.grad.stops.length - 1);
      a.grad.stops.forEach((c, i) => g.addColorStop(i / last, c));
      ctx.strokeStyle = g;
      ctx.lineWidth = a.width;
      ctx.lineCap = 'round';
      ctx.globalAlpha = alpha * a.opacity;
      ctx.stroke(new Path2D(d));
    };
    const dot = (p: BotFrame['dots'][number]): void => {
      // 上游的深度雾化是「往纸色混」，而这里没有纸 —— 透明窗口上「远」只能是「更淡」。
      ctx.globalAlpha = alpha * p.opacity * (p.depth ?? 1);
      ctx.fillStyle = p.color ?? ink;
      if (p.d !== undefined) {
        // 泪滴等自带形状：path 以球半径为单位、中心在原点，所以要自己摆位并放大到 RAYON。
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(((p.rot ?? 0) * Math.PI) / 180);
        ctx.scale(RAYON, RAYON);
        ctx.fill(new Path2D(p.d));
        ctx.restore();
      } else {
        const c = new Path2D();
        c.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fill(c);
      }
    };

    place(ctx);
    ctx.globalCompositeOperation = 'source-over';
    for (const a of f.arcs) arc(a.back, a);            // 环的后半：先画，被球挡住
    if (f.dotsBehind) for (const p of f.dots) dot(p);
    // 眼睛色底：**同一个 bodyPath**，所以它只在球轮廓内出现，见文件头第 ② 步。
    ctx.globalAlpha = alpha;
    ctx.fillStyle = EYE;
    ctx.fill(body);
    // 带洞的球盖上来。离屏是 1:1 的位图，所以画它必须先回到像素坐标系。
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = alpha;
    ctx.drawImage(this.off as HTMLCanvasElement, 0, 0);
    place(ctx);
    if (!f.dotsBehind) for (const p of f.dots) dot(p);
    if (f.notif !== null) {
      ctx.globalAlpha = alpha;
      ctx.fillStyle = notifColor(this.state);
      const n = new Path2D();
      n.arc(f.notif.x, f.notif.y, f.notif.r, 0, Math.PI * 2);
      ctx.fill(n);
    }
    for (const a of f.arcs) arc(a.front, a);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
  }

  /** 取证用：吐当前帧的几何读数。序列层丢掉的「可断言」在这一代回来了 —— 引擎是时间的
      纯函数，所以固定 `now` 下这些数字是可复现的。`render_bot_to_text` 从 `main.ts` expose。 */
  probe(now: number): Record<string, number | string> {
    const f = this.engine.sample(now);
    const nums = f.bodyPath.match(/-?\d+(\.\d+)?/g) ?? [];
    const xs = nums.filter((_, i) => i % 2 === 0).map(Number);
    const ys = nums.filter((_, i) => i % 2 === 1).map(Number);
    const span = (v: number[]): number =>
      v.length === 0 ? 0 : Math.round((Math.max(...v) - Math.min(...v)) * 10) / 10;
    return {
      state: this.state,
      bot: this.state === 'hidden' ? 'none' : STATE_MAP[this.state],
      w: span(xs), h: span(ys),
      eyes: f.eyes.length,
      dots: f.dots.length,
      arcs: f.arcs.length,
      notif: f.notif === null ? 0 : 1,
      alpha: Math.round(f.bodyAlpha * 100) / 100
    };
  }
}

