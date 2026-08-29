/* Vox — 花冠呼吸(Corolla Breath)

   第九代。第八代的三处误判由参考素材的**逐帧度量**推翻(度量脚本与读数见
   docs/design/AI_STATES.md 第 1.4 节),三条都不是审美偏好而是量出来的:

     ① **球壳不存在。** 径向亮度剖面从球心单调衰减,r=0.91 只剩峰值的 3.9%、
        r=1.00 是 0.4% —— 边界上**没有任何亮描边**。真正存在的是 r=0.66→0.78
        那条回升的软带(0.288 → 0.306 → 0.304 → 0.185),而且亮度峰值角在一轮里
        扫过 0–354°:定角镜面高光不存在,那条带在**流动**。轮廓的角向标准差是
        半径的 3.3%,谐波按 1/2/3/4 次递减 —— 低阶、小幅、连续,就是液面。
        所以这一代删掉了:白色内描边、左上镜面弧、右下暖色弧、.gloss 前表面反光。
     ② **亮片是弯曲面,不是平面色片。** 亮度梯度的横向分量占比平均 0.271,
        散开相冲到 0.48–0.54(纯径向渐变该接近 0);脊线含量 0.076、散开相 0.15。
        换算成实现:填充渐变的焦点要**横跨短轴偏出**,而且偏的方向在**世界坐标**里
        一致(所有片体迎同一束光),迎光那条长边再挑一道亮线。
     ③ **思考相是一叠合拢的片在转,不是散开的花。** 散开相只剩单一色簇(245°)、
        横向梯度最高、脊线翻倍 —— 那是几片叠在一起的一个整体。

   第八代量对了的三个数仍然成立,它们是「呼吸」的来源:

     · 中心能量在一轮里摆动 **3.8 倍**(而不是我们此前的 1.1 倍)

     · 中心能量在一轮里摆动 **3.8 倍**(而不是我们此前的 1.1 倍)
     · 等效半径摆动 **±10%**(此前 ±2–7%,只有一半)
     · 呼吸主周期 **≈4 秒 ≈ 1.57 rad/s**

   量出来的结构性结论只有一条,但它推翻了前七代的做法:**聚合度必须是一个连续参数,
   而且花瓣的长度与中心外移要反相** —— 聚合时花瓣变长且往里挪(于是彼此透叠、中心
   被逼出一团过曝光),散开时变短且往外挪(于是分离成小片、中心全暗)。
   前几代只有一层形体,所以只能「变形」,做不出这个。

   **配色改为按素材实测取样**(使用者判定素材已买断并三次要求直接取用,记在 DD-026)。
   取样结果是三簇:205–245°(主,权重 1.0–3.4)、255–295°(次)、315–325°(弱),
   随聚合度移动 —— 越聚合越偏青且整体变亮,散开相收成单一深蓝紫。这与 ANTI_SLOP
   原先禁的紫粉蓝重叠,**那条禁令在本产品已被使用者的取样要求覆盖**,不再声称成立;
   仍然守住的是两处安全语义:`error` 保持朱红、`gated` 保持琥珀 —— 它们不参与取样,
   因为「出错」和「等你确认」不许读作正常态。色值的唯一来源仍是 style.css。

   状态仍由可断言的纯函数承担(FR-6.6 静止一帧可辨):
     bloomLevel 聚合度   petalCount 花瓣数   blobCount 分裂几朵   ringLevel 合拢度
     skew       单侧拉扯 breathAmp/breathRate 呼吸幅度与快慢   spinRate 自转
     contourRadii 液面轮廓(替代原来的精确圆)

   ADR 001 要求渲染器待在接口之后,v2 的 WebGL 实现替换本文件而不动调用方。
   版面在 style.css,颜色的唯一来源也在那里。 */

export type CoreState =
  | 'idle' | 'listening' | 'thinking' | 'speaking' | 'cancelled' | 'error';

/** 颜色来自 CSS(唯一来源),调用方在切态时读一次传进来,不是每帧读。
    **花瓣只用 far / mid / alt 三色轮转,`core` 只给中心白热核** ——
    三个基色用 `lighter` 叠加就能生成全部中间色(两两相叠出三种,三者相叠过曝成白),
    所以不需要在 JS 里插值造第四个色,颜色仍然只有 CSS 一个来源。
    三个色的具体取值是本项目自己的色相家族,见 style.css 的六态节。 */
export type Palette = {
  glass: string;  // 球体底色(带 alpha)。**不随态变**
  edge: string;   // 球壁边缘。**不随态变**
  core: string;   // 白热核
  mid: string;    // 花瓣色 1
  far: string;    // 花瓣色 2
  alt: string;    // 花瓣色 3
};

export type CoreFrame = {
  state: CoreState;
  t: number;          // 秒,单调递增。呼吸、自转、每片的摆动都吃它
  amplitude: number;  // 0.12–1.0,由调用方钳制
  lanes: number;      // 在跑的 agent 路数(task.progress.agents.length)
  gated: boolean;     // 有命令待确认:呼吸与自转归零,冻在半开
  palette: Palette;
  /** 实际聚合度 0–1,由调用方向 bloomLevel() 插值逼近 —— 活物不会瞬间张开或收拢 */
  bloom: number;
  /** 实际合拢度 0–1,由调用方向 ringLevel() 插值逼近。**必须是可选的**:与 `bloom`
      不同,`merge = 0` 是五个态的合法值,没法用「0 当没设」的阈值把两者区分开,
      所以这里用 `undefined` 明确表示「调用方不跑插值,请用目标值」——
      只填目标的对照页(compare/side/replay)因此仍然直接画合拢后的形态。 */
  ring?: number;
  /** 逐句吐纳 0–1:每收到一条 `tts.chunk`(真实事件,一句一条)置 1 再衰减。
      使用者要回复的「深」落在逐句吐纳上而不是体积波动上,所以它只推径向落位。 */
  surge?: number;
  /** 生长度 0–1。0 = 一个点(idle,窗口即将隐藏),1 = 完整的球。
      唤醒时 0→1 约 350ms「从一个点铺张为聆听」;一轮结束后 1→0 收回再隐藏窗口。 */
  seed?: number;
  /** 一次性脉冲:0 无,1 满。点击的「收到」走它 */
  pulse: number;
};

/** 一片花瓣:朝向 + 长度 + 宽度 + 中心外移 + 用哪个色。全部是单位半径。 */
export type Petal = {
  angle: number;
  len: number;
  wid: number;
  off: number;
  bend: number;  // 沿长轴的弯曲量(0 = 直轴椭圆)—— 弧形片体才有机
  /** 宽度包络的指数。**0.5 = 正圆端点**(宽度像 sqrt 趋零);
      越小越钝(接近平头),越大越尖。0.40–0.66 之间每片各不相同。 */
  taper: number;
  /** 最宽处落在长度的哪个比例(0.30 = 靠根部的宽叶,0.78 = 靠尖端的匙形)。
      素材的片体在这一项上差别最大:有宽泪滴、有细月牙、有末端翘起的钩。 */
  waist: number;
  /** 二次谐波的**带符号**卷曲量。正负决定卷向,所以片体不再是同一个模子。 */
  curl: number;
  /** 片体**自身**相对「摆放方向」的转角。0 = 长轴沿半径(花的形态);
      π/2 = 长轴沿切向(成环相的弧形透镜片,长轴跟着环走)。 */
  tilt: number;
  /** 沿环的配色插值系数 0–1(青→磁)。成环相用它,其余形态为 −1 表示走 `tint` 轮转。 */
  mixK: number;
  tint: 0 | 1 | 2;
  lit: number;   // 这一片当前的亮度倍数(各自呼吸的相位差就体现在这里)
};

const TAU = Math.PI * 2;
/**
 * 切态的**弹簧参数**(刚度 k / 阻尼 d,单位:每帧)。
 * 上一版所有切态共用一条 0.12/帧 的指数逼近 —— 每一次切态因此长得一模一样,
 * 而语义完全不同:「被唤醒」该是急的,「被打断」该是垮的,「回到待机」该是慢的。
 *
 * 离散弹簧:`vel += (target − bloom)·k − vel·d;  bloom += vel`
 * **过冲由 d 相对 2√k 决定**:d < 2√k 过冲,d ≥ 2√k 不过冲。所以「要不要回弹」
 * 是一个可以逐条断言的判据,不是手感:
 *   · 进 listening / speaking —— 允许过冲(醒过来、开口都有一个冲上去的劲)
 *   · 进 cancelled / error   —— **禁止过冲**(垮掉和出错不许回弹,回弹读作弹性玩具)
 *   · 进 idle                —— 最慢(呼气),也不过冲
 *   · 进 thinking            —— 中速不过冲;这一态的转场戏在成环,不在聚合度
 */
export function bloomSpring(to: CoreState): { k: number; d: number } {
  switch (to) {
    case 'listening': return { k: 0.14, d: 0.42 };   // 2√k=0.748 vs d=0.42 ⇒ 明显过冲
    case 'speaking':  return { k: 0.12, d: 0.40 };   // 2√k=0.693 vs d=0.40 ⇒ 明显过冲
    case 'thinking':  return { k: 0.10, d: 0.72 };   // 2√k=0.632 < d ⇒ 不过冲
    case 'cancelled': return { k: 0.17, d: 0.90 };   // 2√k=0.825 < d ⇒ 不过冲,但落得更快
    case 'error':     return { k: 0.22, d: 1.00 };   // 2√k=0.938 < d ⇒ 不过冲,最快
    case 'idle':      return { k: 0.045, d: 0.70 };  // 2√k=0.424 < d ⇒ 不过冲,最慢
  }
}

/** 成环/收回的速率(每帧)。**收回比成环快**:成环是「开始想」,收回是
    「想完了要说话」—— 后者接着就要出声,拖着走会让首字延迟看起来更长。 */
export function ringRate(target: number, current: number): number {
  return target > current ? 0.10 : 0.15;
}

/** 球半径占画布半径的比例。画布(`#core`)每边比球的布局盒大 22% ⇒ 1/1.44。
    多出来的余量给 `halo()` 画沿液面轮廓的外发光 —— CSS 的 `box-shadow` 做不到,
    它的形状只能跟着 `border-radius` 走(删掉 50% 之后光晕立刻变成方的)。 */
const BALL = 120 / 140;
/** 泪滴轮廓的采样点数。40 点在 148px 上每点约 2.5px,已经细过柔边的宽度 */
const SAMPLES = 56;
/** 轮廓正面(+y 侧)的段数。迎光那条长边要单独描亮线,得知道从哪儿切 */
export function outlineHalf(samples = SAMPLES): number {
  return Math.max(6, Math.floor(samples / 2));
}

/** 世界坐标里的光向(rad)。左上角,与 body() 的渐变原点 (-0.24R,-0.32R) 同一处 ——
    **所有片体必须迎同一束光**,这是「一叠弯曲面」与「一堆随机渐变」的唯一区别。 */
const LIGHT = Math.atan2(-0.32, -0.24);

/** 花瓣配色的轮转次序。**不是三色均分** —— 素材实测三个色相簇的权重极不均:
    主簇 1.0–3.4、次簇 0.3–0.9、弱簇 0.05–0.4,大致 60/25/15。
    五格里 far 占三格、mid 一格、alt 一格,比 `i % 3` 的 33/33/33 贴合得多。
    `tints` 数组的次序是 [far, mid, alt](见 corolla),所以 0 是主簇。 */
const TINT_ORDER: (0 | 1 | 2)[] = [0, 0, 1, 0, 2];

/** 每片的大小倍数。**素材的片体明显不等大** —— 并排渲染看出来的最刺眼的一处差别是
    我的七片一模一样,读作风车/矢量花;素材是两三片大的加几片小的,交叠出层次。
    固定表而不是随机数:随机数会让静止帧不可复现,几何指纹就断言不了。
    七个值的平均是 1.0,所以整体尺度不变、只有相对大小在变。 */
const SIZE: number[] = [1.12, 0.86, 1.04, 0.90, 1.16, 0.84, 1.08];

/** 每片的**额外朝向偏移**(rad)。等角分布画出来是一个径向对称的风车/矢量花,
    而素材是两三片大的斜着交叉、间距明显不均。并排渲染里这是最后一处结构差别。
    七个值和为 0.05(接近 0),所以整体重心不偏;固定表同样是为了静止帧可复现。 */
const ANG: number[] = [0, 0.34, -0.22, 0.41, -0.37, 0.18, -0.29];

/** **形状表**:每片一组三个形状参数 `[taper, waist, curl]`。
    使用者的判断是「亮片形状过于固定,体现不出多态变换性」—— 上一版七片共用同一个
    轮廓公式,只有长宽在变,所以七片其实是同一个模子的七次缩放。素材不是这样:
    同一帧里能同时看到**宽泪滴、细月牙、末端翘起的钩、near-圆的裂片**。
    三个参数各自的作用见 `Petal` 的字段注释;两端的取值刻意拉开:
      taper 0.40(钝头)→0.66(尖头) · waist 0.32(宽在根部)→0.78(匙形)
      curl  −0.42 →+0.44(卷向相反)
    仍然是**固定表**:随机数会让静止帧不可复现,几何指纹就断言不了。 */
/** 形状**随时间自己走**的速率(rad/s),每片一个。使用者第二次指出「形状还是太固定，
    没有体现出原素材的形态多变性」—— 上一版的形状只随聚合度漂 ±0.08/±0.15,那是个
    很小的量,而且它跟着呼吸走,所以每一片其实一直是同一个样子。素材不是这样:
    **同一片亮片在一轮里会从细月牙morph成宽泪叶再morph成末端翘起的钩**。
    这里让三个形状参数各自沿一条正弦走满整个量程,七个速率互不整除、也与呼吸/自转/
    慢摆(0.29)不整除,所以形态永不回到同一帧。仍然是 t 的纯函数:静止帧可复现。 */
const MORPH: number[] = [0.13, 0.21, 0.09, 0.25, 0.17, 0.31, 0.11];

const SHAPE: [number, number, number][] = [
  [0.44, 0.70, 0.30],
  [0.62, 0.38, -0.34],
  [0.48, 0.62, 0.44],
  [0.58, 0.32, -0.18],
  [0.42, 0.76, 0.16],
  [0.66, 0.46, -0.42],
  [0.52, 0.58, 0.24],
];

/** 液面轮廓的角向谐波。**这一版的七个数是逐帧量出来的，不是估的**（.vox-ref/report.mjs，
    560px 宽裁切 —— 上一版用 360px 裁切，球的外圈光被裁掉了，量出来的 4 次/8 次谐波
    其实是**方形裁切边**漏进来的假信号，那组数作废）。

    真实读数：边界半径稳定在 0.93R（**几乎不随呼吸胀缩**），角向不圆度 2.03% 半径，
    其中 **1 次谐波占 1.50%** —— 而 1 次谐波不是形状，它是**整团光偏离球心**，
    所以这一项改由 `sloshAt()` 承担（见下）。剩下的形状涟漪按 2..8 次摊开，
    每一项只有 0.1–0.5%，RMS 0.69%：**液面是一个几乎正圆、只有不到 1% 涟漪的面**，
    不是一颗会晃成椭圆的果冻。相位漂移速率也照抄实测：3 次 +0.96、7 次 −1.77 最快，
    换算成图形自转是 19.6s / 24.8s 一圈 —— 慢，但不是不动。 */
const WOBBLE: [number, number, number][] = [
  // [谐波次数, 幅度(单位 R), 相位漂移(rad/s)] —— 全部来自实测
  [2, 0.0022, 0.172],
  [3, 0.0027, 0.964],
  [4, 0.0042, -0.067],
  [5, 0.0046, 0.104],
  [6, 0.0011, -0.428],
  [7, 0.0049, -1.774],
  [8, 0.0042, -0.013],
];
const WOBBLE_SUM = WOBBLE.reduce((s, [, a]) => s + a, 0);

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

function mix(a: number, b: number, k: number): number {
  return a + (b - a) * k;
}

function clampR(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/** 给一个 CSS 色值套上 alpha,用来做连续的渐变衰减。
    **这不是第二个颜色来源** —— 它只改传进来那个值的 alpha,色相仍然只在 style.css 里。
    支持 `#rgb` / `#rrggbb` / `rgb()` / `rgba()`;认不出来的原样返回(降级是不透明,
    不是消失)。 */
export function blend(c1: string, c2: string, k: number): string {
  const p = (css: string): [number, number, number] => {
    const t = css.trim();
    if (t.startsWith('#')) {
      const h = t.slice(1);
      const q = h.length === 3 ? h.split('').map((c) => c + c) : [h.slice(0, 2), h.slice(2, 4), h.slice(4, 6)];
      return q.map((v) => parseInt(v, 16)) as [number, number, number];
    }
    const m = t.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i);
    return m ? [+m[1], +m[2], +m[3]] : [255, 255, 255];
  };
  const a1 = p(c1), a2 = p(c2), u = clamp01(k);
  return `rgb(${a1.map((v, i) => Math.round(v + (a2[i] - v) * u)).join(',')})`;
}

export function alpha(css: string, a: number): string {
  const k = clamp01(a);
  const s = css.trim();
  if (s.startsWith('#')) {
    const h = s.slice(1);
    const p = h.length === 3 ? h.split('').map((c) => c + c) : [h.slice(0, 2), h.slice(2, 4), h.slice(4, 6)];
    if (p.length === 3 && p.every((v) => /^[0-9a-fA-F]{2}$/.test(v))) {
      const [r, g, b] = p.map((v) => parseInt(v, 16));
      return `rgba(${r},${g},${b},${k.toFixed(3)})`;
    }
    return s;
  }
  const m = s.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i);
  if (m) return `rgba(${m[1]},${m[2]},${m[3]},${k.toFixed(3)})`;
  return s;
}

/**
 * 液面轮廓:球的边界不是一个精确的圆。返回 n 个等角采样的半径(单位 R),
 * 峰值恰好落在 1.0(基准 = 1 − 幅度和),所以位图边界不会被切掉。
 * **闸门时归零退回精确圆** —— 其余六态的边界都在流动,所以「突然变成一个规整的圆」
 * 与「突然不呼吸不自转」是同一句话的第三遍,余光里最抢眼。
 */
export function contourRadii(f: CoreFrame, n = 96): number[] {
  const out: number[] = [];
  // **闸门不再退回精确圆。** 上一代刻意让它变成「这个软塌塌的世界里唯一一个精确的圆」,
  // 使用者要求删掉所有标准圆的几何元素、并点名闸门那一道 —— 这条设计决定因此被推翻
  // (记在 DD-033)。闸门的辨识仍然成立:它是唯一 `breath` 与 `spin` **同时为 0** 的一帧,
  // 加上整圈琥珀,而琥珀环现在也长在液面轮廓上。
  const flow = 1;
  // **只有 listening 的轮廓真的胀缩。** 使用者:「只有聆听时胀缩」。
  // 素材实测外沿只摆 ±0.4%,所以其余七态的轮廓只有形状涟漪、没有整体缩放;
  // listening 那一档按呼吸给到 ±6%,那是「跟着你的声音在动」的载体。
  // 闸门也不缩放:它的形态是冻住的(呼吸只走亮度)。
  const swell = f.state === 'listening' && !f.gated
    ? 1 + 0.06 * Math.sin(f.t * 1.57)
    : 1;
  for (let i = 0; i < n; i++) {
    const a = (TAU * i) / n;
    let d = 0;
    for (const [k, amp, rate] of WOBBLE) d += amp * Math.cos(k * a + f.t * rate);
    out.push((1 - WOBBLE_SUM * flow + d * flow) * swell);
  }
  return out;
}

/**
 * 成环度(0–1):**这一代的第三重状态量**。0 = 花瓣按各自的大小/朝向/形状摊开成
 * 一朵**互相透叠**的花;1 = 全部**形态统一**（同大小、同形状、同相位）、**各自缩小**、
 * **推到一圈半径上彼此不再重叠**，整圈随自转**环绕**球心走。
 *
 * 三次读错同一句话，记在 DD-031：使用者先说「化为统一的缩小形态转圈」，我把
 * 「统一」读成了「合并成一个」，于是做了「收拢成一片 + 一个亮团公转」；使用者补充
 * 「变成独立互不重叠的独立元素进行环绕动作」之后才清楚：**统一 = 各片长得一样
 * （uniform），不是并成一个（merged）**。参考素材的散开相实测正是这个样子 ——
 * 一圈**分离的**亮片、等效半径 0.680（全轮最大值）、中心全暗。
 *
 * **闸门归零**:闸门要冻在半开的花上,散成一圈会读作「它还在忙」。
 */
export function ringLevel(f: CoreFrame): number {
  return !f.gated && f.state === 'thinking' ? 1 : 0;
}

/** 当前这一帧的成环度 = **调用方插值到的值**,没插值时退回目标值。
    与 `bloomAt()` 同构,理由也同一条:`ringLevel()` 是**目标**,直接用它会让
    「散开成环」变成一帧之内的跳变 —— 使用者问过「圆片的收缩为什么我在预览里
    看不到」,看不到的原因就是它当时根本没有过程,只有结果。 */
export function ringAt(f: CoreFrame): number {
  return f.ring === undefined ? ringLevel(f) : clamp01(f.ring);
}

/**
 * **晃动(slosh)**:整团光在球里偏离球心多少(单位 R)。使用者的判断是「它整体并不是
 * 一个固体球，而是一个有微动的液体球」，而这就是那个「微动」的**主载体** ——
 * 逐帧实测:亮度质心偏心**平均 0.031R、最大 0.078R**，而边界半径几乎不动(±0.4%)。
 * 也就是说素材的液态感不在「边界晃成椭圆」，在**装在里面的东西在晃**;
 * 边界的 1 次谐波(占不圆度的 1.50%，其余各次合起来只有 0.69%)正是这一晃的投影。
 *
 * 三条互不整除的慢正弦，峰值 0.077R、均值约 0.038R，与实测同量级。
 * **闸门归零**:一个冻住的东西连晃都不晃 —— 这是闸门第四项归零(呼吸/自转/涟漪/晃动)。
 */
export function sloshAt(f: CoreFrame): { cx: number; cy: number } {
  if (f.gated) return { cx: 0, cy: 0 };
  const t = f.t;
  const x = 0.030 * Math.sin(t * 0.23) + 0.022 * Math.sin(t * 0.41 + 1.7) + 0.014 * Math.sin(t * 0.67 + 0.4);
  const y = 0.028 * Math.cos(t * 0.19 + 0.9) + 0.020 * Math.sin(t * 0.37 + 2.3) + 0.013 * Math.cos(t * 0.61);
  return { cx: x * 0.85, cy: y * 0.85 };
}

/**
 * 成环时每片被推到的半径(单位 R)与角向占位。返回的 `span` 是**单片允许的最大角宽**
 * ——「互不重叠」是可算的:一圈 n 片、每片角宽 span，只要 `span < TAU/n` 就不重叠。
 * 这里取 0.62 的填充率，所以留出 38% 的空隙,余光里数得出几片。
 */
/**
 * 吸气 / 吐气的**径向偏置**(单位 R,加在片体中心的落位上)。
 * 使用者报的缺陷是「聆听和说话状态区分度不大」,而两者的呼吸深度差(0.20 vs 0.11)
 * 在余光里分不出来。落法由使用者定:**聆听是吸、回复是吐**。
 *   · listening 负 —— 片体再往球心收一点,彼此透叠更狠,中心白热核最亮(在收集)
 *   · speaking  正 —— 片体向外铺到中段、**仍然互相透叠、不成环**(在输出)
 *     「不成环」是硬要求:成环是 thinking 的签名,回复借它就撞了
 *   · 其余五态 0,落位仍由聚合度自己决定
 */
export function spreadAt(f: CoreFrame): number {
  if (f.gated) return 0;
  switch (f.state) {
    case 'listening': return -0.085;
    case 'speaking': return 0.26;
    default: return 0;
  }
}

export function ringSlot(n: number): { radius: number; span: number; fill: number } {
  // **两个数都要按各自的球半径归一,这是上一版放太靠里的根因。**
  // 素材成环相(帧 166–196)实测:块心 0.459 RMAX、块长轴 0.18 RMAX,而**同帧球的可见
  // 边界**(方位平均亮度跌到峰值 10% 处)在 0.78 RMAX。所以相对球半径是
  //   块心 0.459/0.78 = 0.589 · 长轴 0.18/0.78 = 0.231
  // 本渲染器的球可见边界在液面轮廓 ≈0.95R,换算过来:
  //   slot.radius = 0.589 × 0.95 = **0.56R** · 元素长 0.231 × 0.95 = 0.219R
  // 上一版直接把 0.459 RMAX 当成 0.44R 用(等于假设球填满整幅裁切),元素因此
  // 偏内约 21% —— 使用者说的「没有分布在球体的靠最外层」就是这 21%。
  // 填充率:r=0.56 处 6 块的角节距 2π·0.56/6 = 0.586R,块长 0.219R ⇒ 0.374,取 0.40。
  const fill = 0.40;
  return { radius: 0.56, span: (TAU / Math.max(1, n)) * fill, fill };
}

/**
 * 聚合度基准(0–1)。**这是这一代的第一重状态量** —— 参考动画里全部的戏都在它身上:
 * 0 = 花瓣分离成小片、中心全暗;1 = 花瓣覆盖整球、彼此透叠、中心过曝成白。
 * 采集时最集中(能量最高),取消时散开且没有核,闸门冻在半开。
 */
export function bloomLevel(f: CoreFrame): number {
  if (f.gated) return 0.48;
  switch (f.state) {
    case 'idle': return 0.52;
    case 'listening': return 0.80 + f.amplitude * 0.12;
    case 'thinking': return 0.70;
    case 'speaking': return 0.74 + f.amplitude * 0.10;
    case 'cancelled': return 0.26;   // 花瓣散开、无白热核 —— 光垮了
    case 'error': return 0.58;
  }
}

/**
 * 呼吸幅度(bloom 的摆动量)。参考动画实测中心能量摆动 **3.8 倍**、等效半径 ±10%,
 * 换算成聚合度就是 ±0.1 量级 —— 前几代给的 ±2–7% 只有它的一半,这正是
 * 「生命波动不够」的量化原因。**闸门是 0:它不呼吸。**
 */
export function breathAmp(f: CoreFrame): number {
  // **闸门不再归零。** 使用者:「即便是 gated 了,也应该是个会动的存在」——
  // 冻住的是**形态**(自转、成环、形状漂移),呼吸留着,而且比待机更浅更慢一档,
  // 读作「它活着,但被拦住了」而不是「它死了」。
  if (f.gated) return 0.05;
  switch (f.state) {
    // 深度排序由使用者定:**聆听最深**(在收集) > 思考 > 回复 > 待机 > 闸门 > 取消最浅。
    // `f.amplitude` 在真机路径上恒为默认值 —— 语音契约的 9 种事件里没有连续音量,
    // 而使用者选择不动那份字节冻结的契约。所以这里**不声称**波动反映音量,
    // 它是自走的呼吸;amplitude 只在 SIM/调参页里被人为拨动。
    case 'listening': return 0.20;
    case 'thinking': return 0.13;
    case 'speaking': return 0.11;
    case 'idle': return 0.08;
    case 'cancelled': return 0.035;
    case 'error': return 0.14;
  }
}

/** 呼吸快慢(rad/s)。参考动画的主周期约 4 秒 ≈ 1.57 rad/s,listening 就取在那儿;
    待机更慢像睡着的人,思考更快像在忙,异常最快像喘。 */
export function breathRate(f: CoreFrame): number {
  // **六态共用一条心跳。** 1.57 rad/s ≈ 4 秒一次,就是参考素材的主周期。
  // 使用者选的是「共用一条心跳、只改幅度」:开心心跳快、紧张心跳快 —— 但还是同一颗心。
  // 上一版每态一个频率(0.85/1.57/2.30/2.97/0.50/3.10),切态时节奏会跳,
  // 读作换了一个东西而不是同一个东西换了情绪。
  // **闸门也用这一条**(只有幅度更浅):它是「被拦住的同一个活物」。
  // 唯一的例外是 error 的**漏拍**,那不在频率里,见 `beatGate()`。
  return HEART;
}

/** 心跳频率(rad/s)。1.57 ≈ 4 秒一轮,取自参考素材的主周期实测。 */
const HEART = 1.57;

/**
 * 心跳的**漏拍闸**(0–1,乘在呼吸上)。只有 `error` 非 1 ——
 * 使用者要 error 同时有「单侧拉扣」和「心跳漏拍」。
 * 做法:每 3 个心跳周期里,有约 12% 的一段把呼吸压到 0.15 倍,像心跳漏了一下。
 * 它是 `t` 的纯函数,所以静止帧仍然可复现;其余七态恒为 1,一点开销都不带。
 */
export function beatGate(f: CoreFrame): number {
  if (f.state !== 'error' || f.gated) return 1;
  // 周期 = 1.5 个心跳(≈6 秒)。3 个心跳(≈12 秒)漏一次太稀,
  // 而 error 通常只停留几秒 —— 用户很可能一次都看不到。
  const phase = ((f.t * HEART) / (TAU * 1.5)) % 1;
  return phase > 0.66 && phase < 0.80 ? 0.12 : 1;
}

/** 当前这一帧的聚合度 = **调用方插值到的基准** + 呼吸。纯函数,给定 f 就确定。
    基准取 `f.bloom` 而不是 `bloomLevel(f)`:后者是目标值,用它会让切态变成跳变,
    而主循环里那句 `bloom += (target-bloom)*0.12` 就成了死代码(这是个真缺陷,已修)。
    `f.bloom` 为 0 时(对照页只填目标、不跑插值)退回目标值。 */
export function bloomAt(f: CoreFrame): number {
  const amp = breathAmp(f);
  // **基准要给呼吸留出顶部余量。** listening 的基准是 0.902、呼吸幅度 0.165,
  // 相加 1.067 —— 上界被 clamp01 削平,一轮里约有 23% 的时间卡在 1.0 不动。
  // 后果是呼吸被**削顶**:实测稳态能量摆动只有 1.80×,而此前记的 5.78× 是因为
  // 度量例程没有预热、把「切态时 bloom 从上一态爬过来」的过渡也算进了摆动里。
  // 这里把基准压到 1−amp:峰值仍然是全表最高(1.0),但整个正弦都在量程内。
  const base = Math.min(f.bloom > 0.001 ? f.bloom : bloomLevel(f), 1 - amp);
  return clamp01(base + amp * beatGate(f) * Math.sin(f.t * breathRate(f)));
}

/** 花瓣数。参考动画是 7 片;这里按态变,因为它同时是可数的状态量。 */
export function petalCount(_f: CoreFrame): number {
  // **八态统一 6 个。** 使用者选了「生命感优先」+「亮片数全部统一为 6 个」——
  // 上一版按态变(5/7/6/4/3/6)是把「可数」当成状态签名之一,而那条签名要求形状
  // 不许一直流动,两者不可兼得。区分现在靠**签名动作**(见 AI_STATES 的动作表)。
  return 6;
}

/** 整组自转(rad/s)。**闸门是 0:它不转。** 都很慢 —— 转快了会读成加载指示器。
    thinking 是 0.46,比别的态快一档:合拢成一片之后「在转」本身就是这一态的语义
    (一叠亮片在球内旋转),而一片不对称的东西转起来才读得出来。0.46 rad/s ≈ 13.7 秒
    一圈,比加载指示器慢一个数量级还多,不会被读成 spinner。 */
export function spinRate(f: CoreFrame): number {
  if (f.gated) return 0;
  switch (f.state) {
    case 'idle': return 0.06;
    case 'listening': return 0.13;
    // 成环相的公转速度是**量出来的**:帧 163–197 的六条轨迹角速度
    // +1.22 / +1.35 / +1.76 / +1.85 / +1.89 / +1.92 rad/s,平均 ≈1.67 ⇒ 3.8 秒一圈。
    // 比旧值 0.46 快 3.6 倍。这一条越过了「不许转到看得出在转」那条老指引 ——
    // 使用者要的正是看得出在绕，而 1.67 rad/s 仍比加载指示器(~6)慢近四倍。
    case 'thinking': return 1.67;
    case 'speaking': return 0.10;
    case 'cancelled': return 0.03;
    case 'error': return 0.42;
  }
}

/** 单侧拉扯(**只有 error 非零**):一侧花瓣被拉长、另一侧压短。 */
export function skew(f: CoreFrame): number {
  return !f.gated && f.state === 'error' ? 0.22 : 0;
}

/** 分裂成几朵。thinking 时 = 在跑的 agent 路数(1–4),界面上唯一直接可数的真实数字。 */
export function blobCount(f: CoreFrame): number {
  if (f.gated || f.state !== 'thinking') return 1;
  return Math.max(1, Math.min(4, Math.floor(f.lanes)));
}

/**
 * 每片花瓣的几何。**生命波动全在这个函数里**,四件事叠加:
 *   ① `bloom` 同时驱动**长度**与**中心外移**,而且两者**反相** —— 聚合时花瓣变长
 *      且往里挪(于是彼此透叠、中心过曝),散开时变短且往外挪(于是分离成小片)。
 *      参考动画整段戏就是这一条,前几代把它做成了「半径抖动几个百分点」。
 *   ② 每片的呼吸相位错开(`ph * 1.9`)—— 花瓣轮流胀缩,整体因此是**波动**不是同步缩放。
 *   ③ 整组慢自转 + 每片额外的慢摆(0.29 rad/s,与呼吸频率不整除)。
 *   ④ 每片自己的亮度倍数 `lit` 也跟着相位走 —— 参考动画里总有几片比别的亮。
 * 三个频率(breathRate / spinRate / 0.29)互不整除,所以永不回到同一帧,
 * 不需要噪声也不需要随机数(随机数会让静止帧不可复现,指纹就断言不了)。
 */
export function petals(f: CoreFrame): Petal[] {
  const n = petalCount(f);
  const b = bloomAt(f);
  const spin = f.t * spinRate(f);
  const sk = skew(f);
  const rate = breathRate(f);
  const rg = ringAt(f);
  const slot = ringSlot(n);
  // 成环时每片自己的慢摆**精确归零**(而不是留 8%)。使用者报的缺陷是「亮片间的距离
  // 没有均匀，导致有些亮片相连」—— 慢摆、SIZE 偏差、形状偏差、呼吸相位偏差这四项
  // 只要还剩一点,相邻两片的角距就不再相等,最近的那一对就会碰上。
  const swing = (0.10 + f.amplitude * 0.09) * (1 - rg);
  const out: Petal[] = [];
  for (let i = 0; i < n; i++) {
    const ph = (TAU * i) / n;
    // **「统一」= 各片长得一样(uniform),不是并成一个(merged)。**
    // `vr` 是「保留多少片间差异」:成环时只剩 6%,所以 SIZE、ANG、三个形状参数、
    // 每片的呼吸相位全部趋同 —— 一圈**同大小、同形状、同相位**的元素。
    // **精确归零而不是留 6%。** 留一点就等于「各片略有不同」,而略有不同 = 角距不等
    // = 最近的一对贴上。均匀是可算的:vr=0 时六片完全同形同大同相位,角距恒为 60°。
    const vr = 1 - rg;
    // 这是「几路在跑」在成环相的载体 —— 一圈里数得出几段,而不是拆成几个环。
    // 一路时整圈**等分**,没有缺口 —— 缺口是「分了几组」的信号,只有一组就不该有缺口
    // **一律等分。** 使用者的要求是「保证亮片间具有相同的距离」,而上一版按 lanes
    // 把环切成 N 段、段间留空,读出来就是「有些亮片相连」(3 路时角距 48/72/48/72/48/72)。
    // 分组因此删掉:等分是硬要求,路数改由 `blobCount()` 只在指纹里承担,
    // 界面上不再用角距编码它 —— 用一个会破坏等距的信号去表达可数性,代价大于收益。
    const ringPh = (TAU * i) / n;
    // 从「各自朝向的花」连续过渡到「环上的定位」
    const phGeo = mix(ph + ANG[i % ANG.length], ringPh, rg);
    // 每片自己的呼吸:±0.16 的聚合度偏移,所以同一帧里有的花瓣already张开、有的还收着。
    // 成环时按 vr 收 —— 一圈统一的元素要一起胀缩,各自错相就不再「统一」
    const own = clamp01(b + 0.16 * vr * Math.sin(f.t * rate + ph * 1.9));
    const angle = phGeo + spin + Math.sin(f.t * 0.29 + ph) * swing;
    // 长度与外移反相:这是「呼吸」的全部
    // 长度随聚合度长到**横跨整球**(1.70:参考在高聚合相的等效半径 0.674,
    // 而我上一版只有 0.347 —— 花瓣必须真的铺满球,不能挤在中间三分之一)。
    // off 取 -len 的一个比例:高聚合时片体居中跨过球心,低聚合时转正推出去并缩小。
    // 越过 0 的那一点 own = 0.32/0.80 = **0.40,是量出来的**:素材的核亮度比
    // (r<0.12 均值 / 全帧峰值)在 agg<0.40 时是 0.33–0.39、agg>0.40 时直接跳到 0.97 ——
    // 0.4 一线以下片体必须让开球心,一线以上必须盖住它。
    // 试过把交叉点推到 0.50(想让散开相更暗),逐帧实测反而全面变差:eqR 平均差
    // 0.059→0.086、横向梯度 0.152→0.197、质心 71→90 —— 因为 0.40–0.45 那一段
    // 素材的核**已经亮了**,推到 0.50 等于把亮的一段也让开了。0.40 就是 0.40。
    // 上限 1.30 而不是 1.70:并排渲染看出来 1.70 的片体外缘会顶到 1.21R,被液面轮廓
    // **切平**,于是球没有边界、只有一圈参差的花瓣尖。素材的球有一条清楚的软边环,
    // 片体全在环之内。1.30 × SIZE 上限 1.26 的外缘约 0.89R,正落在环(0.86R)附近。
    // SIZE 的偏差同样按 vr 收:合拢时七片趋于等大,才叠得成一片
    const sz = 1 + (SIZE[i % SIZE.length] - 1) * vr;
    // 合拢时更长更窄:素材那一相是**一片穿过球心的长透镜**(角向主谐波 2 次),
    // 不是一团六瓣的花
    // 成环时**缩到 0.42 倍**:一圈 n 片、每片只能占 TAU/n 的 62%,片体不缩下来
    // 就一定压在一起,而「互不重叠」是这一相的定义。0.42 是按 slot.span 反算的:
    // 半径 0.55R 处、6 片、填充率 0.62 ⇒ 单片弧长约 0.36R,片长取它的量级
    // 成环时缩到 **0.16 倍**:实测每块长轴只有 0.18R,而 thinking 的 own≈0.70 下
    // 未收缩的片长是 1.11R —— 0.16 倍正好落在 0.18R。上一版只收到 0.42 倍(0.46R),
    // 那是素材大片相的尺寸，不是成环相的
    // 上限 **1.02** 而不是 1.52。片体的外缘 ≈ 0.52·len·SIZE,1.52 时是 1.0R —— 顶到球沿,
    // 于是 0.72R 那条软边环被完全盖住。素材的亮片**全在环之内**(剖面 0.66 有谷、
    // 0.70–0.74 才是环),1.02 让外缘落到约 0.67R,正好在环之内。
    // 成环时的收缩系数是**按实测反算**的，不是调出来的：素材块长轴 0.18 RMAX、
    // 同帧球可见边界 0.78 RMAX ⇒ 相对球半径 0.231 ⇒ 本渲染器 ×0.95 = **0.219R**。
    // thinking 的 own = 0.70 ⇒ 未收缩片长 = mix(0.14,1.02,0.70) = 0.756R，
    // 所以系数 = 0.219/0.756 = 0.29 ⇒ (1 − 0.71·rg)。
    // 上一版 0.84 让片长只有 0.12R（实测的 55%），元素因此又小又暗。
    const len = mix(0.14, 1.02, own) * (1 + sk * Math.cos(angle)) * sz * (1 - 0.541 * rg);   // 0.459 倍 ⇒ 0.347R,即实测 0.219R 的 **1.5 倍**(使用者要求)
    // 0.40 一线以下推出去、以上拉进来。**不是线性**:线性在 own=0.27 只推出 0.10len,
    // 而素材那一相是一圈分离的亮片、中心全黑。指数 0.7 让它在阈值下方快速张开。
    let push = own < 0.40
      // −0.85 而不是 −0.55:逐帧对照实测我的核亮度比**下限卡在 0.513**,而素材能低到
      // 0.022 —— 低聚合相我的球心一直是亮的,因为片体只被推出 0.55len,羽化的内尾仍然
      // 盖住球心。推到 0.85len 才真的让开。(改的是**推出量**,不是 0.40 那个阈值 ——
      // 阈值动过一次,逐帧全面变差,那条结论不变。)
      ? -0.85 * Math.pow(1 - own / 0.40, 0.7)
      : 0.48 * ((own - 0.40) / 0.60);
    // **成环时一律往外推到环半径上**,不管聚合度是多少。中心必须让空 ——
    // 「一圈独立元素」与「一朵有花心的花」的区别就在这里,而 thinking 的 bloom 是
    // 0.70(阈值之上),按原式子它会被拉进球心、彼此透叠,正是上一版的样子。
    // 成环的落位不在这里算（见下面的 off）：把它混进一个比值再乘 len，
    // 就必须在这里再抄一份 len 的收缩系数，两处一旦不同步元素就落错半径。
    // **每片穿过球心的深浅不同**(0.74–1.26,固定表 SIZE 复用作偏置)。全部等量拉到
    // 球心的后果是所有片体把最亮的那一段叠在同一个点上,高聚合相因此中心过曝、
    // 边缘失色 —— 而素材的高聚合相是几片交叉的亮面,粉色一直铺到边上。
    // 合拢时按 vr 收:一片统一的亮片本来就该同心。
    const offFlower = -len * push * (1 + (SIZE[(i + 3) % SIZE.length] - 1) * 1.6 * vr);
    // **成环时直接把「元素中心」放到环半径上。**
    // 上一版把落位混成一个比值再乘 len，而那个比值里硬抄了一份 len 的收缩系数
    // (0.58)。收缩系数后来改成 0.84，两处就不一致了 —— 元素实际落在约 0.21R
    // 而不是 0.56R，这正是使用者说的「没有分布在球体的靠最外层」。
    // 片体沿自己的轴从 0 长到 len，所以中心在 off + len/2 ⇒ off = radius − len/2。
    // 只有一份系数，改 len 的收缩不会再让落位漂掉。
    // 吸/吐偏置 + 逐句吐纳。两者都只作用在**径向落位**上,不动大小与形状 ——
    // 使用者要求 speaking「不需要体积波动」,所以吐纳走位置不走体积。
    const bias = (spreadAt(f) + (f.surge ?? 0) * 0.08) * (1 - rg);
    const off = mix(offFlower + bias, slot.radius - len / 2, rg);
    const [tp, ws, cl] = SHAPE[i % SHAPE.length];
    // 这一片的形态相位。两条速率互为无理比(×1.41),所以 taper 与 waist 不同步走
    const mr = f.t * MORPH[i % MORPH.length] + ph * 1.3;
    const m1 = Math.sin(mr);
    const m2 = Math.sin(mr * 1.41 + 1.9);
    out.push({
      angle,
      len,
      // 越聚合越细长:参考高聚合相是几片交叉的长透镜片,不是圆胖的花瓣
      // 高聚合时不收那么细:参考的聚合相是几片**饱满的透镜**,不是细长刀片
      // 成环时**变圆**:实测长短轴比只有 1.22–1.64(近乎圆的小片),
      // 而未成环时是 3.75 的长透镜。×1.55 把 0.45len 抬到 0.70len ⇒ 比 1.43
      // 成环时加宽到实测长短比：实测长轴/短轴 = 1.64–1.67，而 mix(0.74,0.46,0.70)
      // 只给到 1/0.544 = 1.84。0.219R × 0.544 × 1.10 = 0.1315R ⇒ 长短比 1.67。
      // 加宽后切向占位仍只有节距(0.586R)的 22%，离重叠还远。
      wid: len * mix(0.74, 0.46, own) * (1 + 0.10 * rg),
      // 弯曲量。参考实测横向亮度梯度占比 0.271(散开相 0.48–0.54),纯平面径向渐变
      // 该接近 0 —— 片体必须真的是弯的,量程因此从 0.02–0.20 提到 0.06–0.30
      // 弯曲量。参考实测横向亮度梯度占比 0.254(散开相 0.44–0.54),纯平面径向渐变
      // 该接近 0 —— 片体必须真的是弯的。弯量本身也跟着形态相位走,而且**符号会翻**:
      // 一张弯曲面转过去就是反向弯的,这是「面在动」而不是「图形在缩放」的直接证据
      // 成环时**弯曲与卷曲一起归零**:使用者问「形状统一了吗」—— 带钩的叶片被
      // rotate 到六个不同朝向之后,读出来就是六个不同的形状。对称的椭圆片旋转
      // 之后仍然是同一个形状,这才是「统一」。
      bend: mix(0.06, 0.30, own) * (0.45 + 0.55 * m2) * (cl < 0 ? -1 : 1) * (1 - rg)
        + 0.24 * rg,   // 成环相给一个**统一**的弯曲:弧形透镜片,六片同弯同向所以仍然「形状统一」
      off,
      // **形状自己沿时间走满整个量程**,不只是随聚合度漂一点。使用者两次指出「形状
      // 过于固定，体现不出原素材的形态多变性」—— 上一版只让三个参数随 own 漂 ±0.08/
      // ±0.15,那个量太小而且跟呼吸同相,所以每一片一直是同一个样子。
      // 现在:固定表给每片一个**身份**(中心值),`MORPH` 给它一条自己的慢正弦
      // (走满量程),`own` 再给一点与呼吸相关的偏置。三者相加、按 vr 在合拢时收掉,
      // 于是同一片会从细月牙 morph 成宽泪叶再 morph 成末端翘起的钩。
      // 全部是 t 的纯函数:静止帧仍然可复现,几何指纹仍然可断言。
      // 成环时 taper 收到 0.5(正圆端点)、waist 收到 0.5(最宽处在正中) ⇒ 左右对称
      taper: mix(clampR(tp + (m1 * 0.17 + (own - 0.5) * 0.10) * vr, 0.36, 0.70), 0.50, rg),
      waist: mix(clampR(ws + (m2 * 0.24 + (own - 0.5) * 0.18) * vr, 0.22, 0.84), 0.50, rg),
      curl: (cl * mix(1.40, 0.60, own) + m1 * 0.32) * vr * (1 - rg),
      tint: TINT_ORDER[i % TINT_ORDER.length],
      // 成环相:长轴转到**切向**(π/2),片体因此是一枚跟着环走的弧形透镜片,
      // 而不是一根指向球心的辐条。使用者选的形状方向就是这个。
      tilt: rg * Math.PI / 2,
      // 沿环的青→磁插值。用**环上的位置**(i/n)而不是时间:整圈转起来颜色跟着位置走,
      // 所以「球内有明显变色的亮片」是由位置承担的,静止一帧也看得见渐变。
      // 非成环相给 −1,走原来的三色轮转。
      mixK: rg > 0.5 ? i / Math.max(1, n - 1) : -1,
      // 亮度量程 0.02→1.26:参考素材在散开相是近乎全黑(归一化能量 0.012),
      // 而 +0.06 的下限逐帧实测仍然把散开相垫得太亮(核亮度比卡在 0.618 下不去,
      // 素材是 0.33)。指数 1.15 而不是 1.6:1.6 把中段压得太狠(参考 agg 0.4
      // 已到 0.396,我只有 0.271)
      // 指数 1.15 → **1.50**:柔化那一轮(模糊 0.007R→0.012R、羽化 0.62→0.52、
      // 单片 alpha 降一档)把 listening 的能量摆动从 4.23× 压到 2.76×,掉到了
      // ≥4.0× 判据之下。**能量摆动的主载体是 lit 的量程**,不是清晰度 —— 把指数抬高
      // 等于让低聚合的片体更暗,一轮里的明暗比因此重新拉开,而清晰度一点没动。
      // 指数 1.28:1.15 那一版在柔化之后把 listening 的能量摆动压到 2.76×(判据 ≥4.0×),
      // 1.50 又冲到 5.47× —— **比素材整轮的 3.8× 还高**,那已经不是呼吸而是搏动。
      // 1.28 落在 4.5× 附近,清晰度一点没动:能量摆动的载体是 lit 的量程,不是锐度。
      // 指数在补偿改键之后重新定标:1.28 配上「按态定标」的 alpha 补偿给出 6.62×,
      // **比素材整轮的 3.8× 高出快一倍**,那是搏动不是呼吸。1.02 落在 4.5× 附近 ——
      // 高于 ≥4.0× 判据、贴近素材。清晰度全程没动:能量摆动的载体是 lit 的量程
      lit: Math.pow(own, 1.02) * 1.32 + 0.015,
    });
  }
  return out;
}

/**
 * 花瓣的柔边轮廓(单位:len / wid)。**一头收窄、一头饱满的弯曲叶片**:
 * 尖端在原点、最宽处落在 `waist`、圆头在 (len, 0),中轴被弯成一条不对称的弧。
 * u ∈ [0,1] 走一遍正面、再走一遍反面闭合。
 * **不用纯椭圆**:椭圆两头一样粗,几片一叠读作交叉的镜片而不是花瓣(第七代踩过);
 * 收窄的那一头朝内,花瓣才像从中心长出来的。
 *
 * **四个形状参数,不是一个模子**(第九代第二轮):`taper` 管端点钝/尖、`waist` 管
 * 最宽处在哪、`bend` 管主弯、`curl` 管卷向与卷量。上一版只有 `bend` 可变,七片其实
 * 是同一个轮廓的七次缩放 —— 使用者判为「形状过于固定,体现不出多态变换性」。
 * 参数由 `petals()` 从固定表取,并随该片的聚合度 `own` 连续漂移。
 */
export function petalOutline(
  len: number, wid: number, bend = 0,
  taper = 0.5, waist = 0.65, curl = 0.30,
  samples = SAMPLES,
): { x: number; y: number }[] {
  const half = outlineHalf(samples);
  const pts: { x: number; y: number }[] = [];
  // **指数 0.5 是「正圆端点」的充要条件**:宽度像 sqrt 趋零,端点就是半圆;
  // 线性趋零(指数 1)会得到尖角,那正是第八代读作星芒/矢量花瓣的根因。
  // 这里让它按片可变(0.40 钝头 → 0.66 尖头),但**不许到 1**:圆端是硬约束。
  const tp = taper < 0.34 ? 0.34 : taper > 0.72 ? 0.72 : taper;
  // 最宽处的位置:把 sin(πu) 的峰从 0.5 挪到 `waist`,用一条分段的幂变换 ——
  // u < waist 时压缩、u > waist 时拉伸,峰因此落在 waist 上而两端仍归零。
  const ws = waist < 0.18 ? 0.18 : waist > 0.86 ? 0.86 : waist;
  const skewU = (u: number) => (u <= ws ? 0.5 * (u / ws) : 0.5 + 0.5 * ((u - ws) / (1 - ws)));
  const w = (u: number) => wid * Math.pow(Math.sin(Math.PI * skewU(u)), tp);
  // 中轴:一次谐波给主弯,二次谐波给卷边,`curl` 带符号所以卷向按片不同。
  // 两项在 u=0/1 都是 0,所以端点的半圆收口不受影响。
  const axis = (u: number) => bend * len * (Math.sin(Math.PI * u) + curl * Math.sin(TAU * u));
  for (let i = 0; i <= half; i++) {
    const u = i / half;
    pts.push({ x: len * u, y: axis(u) + w(u) });
  }
  for (let i = half - 1; i > 0; i--) {
    const u = i / half;
    pts.push({ x: len * u, y: axis(u) - w(u) });
  }
  return pts;
}

/**
 * 分裂后每朵花的中心与缩放(单位 R)。一朵时在正中;多朵时绕中心排开并各自缩小 ——
 * 要**互相渗透**而不是排成一圈小点(排太远读作省略号或加载点,实测过三个值)。
 */
export function blobCenters(f: CoreFrame): { cx: number; cy: number; scale: number }[] {
  const n = blobCount(f);
  const rg = ringAt(f);
  // 晃动只作用在**光**上:body/film/shell 画的是球本身,球没有晃,是里面的东西在晃。
  // 这正是实测的形状 —— 边界半径 ±0.4%(几乎不动)而亮度质心偏心到 0.078R。
  const sl = sloshAt(f);
  // **成环时不再拆成几朵。** 成环相本身就是「一圈独立元素」,再把它拆成 N 朵小环
  // 就变成了 N 个环 —— 使用者要的是一圈互不重叠的独立元素,不是几团各自的小花。
  // 路数**不再用角距编码**:等距是硬要求(使用者原话「保证亮片间具有相同的距离」),
  // 而任何分组都会破坏等距。`blobCount()` 仍如实返回路数,所以几何指纹里
  // thinking(1 路) 与 thinking(3 路) 照旧可分,只是界面上不再画出来。
  if (n === 1 || rg > 0.5) return [{ cx: sl.cx, cy: sl.cy, scale: 1 }];
  const ring = 0.40;
  const scale = 1 / (n * 0.30 + 0.70);
  const out: { cx: number; cy: number; scale: number }[] = [];
  for (let i = 0; i < n; i++) {
    const a = -Math.PI / 2 + (TAU * i) / n;
    out.push({ cx: Math.cos(a) * ring + sl.cx, cy: Math.sin(a) * ring + sl.cy, scale });
  }
  return out;
}

/** 白热核半径(单位 R)。**低聚合时没有核** —— 参考动画的分裂段中心是全暗的,
    核是花瓣挤到一起才被逼出来的那团过曝光。
    0.40 这个阈值是量出来的:素材的核亮度(r<0.12 的均值 / 全帧峰值)在 agg<0.40 时
    是 0.33–0.39,agg>0.40 时**直接跳到 0.97 并饱和**。所以过了阈值不能线性慢慢长 ——
    上一版 `(b-0.40)*0.60` 到 b=1.0 才 0.36,而素材那一档已经过曝。
    改成开方:阈值之上迅速抬起再趋饱和,形状与素材的阶跃一致。 */
export function coreGlow(f: CoreFrame): number {
  // **成环相中心必须是暗的。** 元素全被推到 0.55R 的环上,中心什么都没有;
  // 留一颗白热核会把「一圈独立元素」读成「一朵有花心的花」。素材的散开相实测
  // 中心全暗(核亮度比 0.33 而峰值在环上),所以这一项按成环度线性让开。
  const rg = ringAt(f);
  if (rg > 0.999) return 0;
  const b = bloomAt(f);
  if (f.state === 'cancelled' || b < 0.40) return 0;
  // 上限用 0.86 次幂压一道软膝:sqrt 在 b→1 时长得最快,而那正是片体交叠也最密的
  // 一段,两者叠加就是验收页上 listening 那团白。0.86 次幂让核在高聚合相收一点
  return Math.pow((b - 0.40) / 0.60, 0.86) * 0.42 * (1 - rg);
}

/** Canvas 2D 生产渲染器(FR-6.5)。v2 换 WebGL 时替换本类,调用方不动。 */
export class CorollaBreath {
  /** 球半径 / 画布半径。默认 `BALL`;调参页(size.html)直接改它来试最佳大小。
      它是**渲染器的旋钮**不是状态量,所以不进 `CoreFrame`。 */
  ballRatio = BALL;
  private readonly ctx: CanvasRenderingContext2D;
  private css = 0;
  private dpr = 0;

  constructor(private readonly canvas: HTMLCanvasElement) {
    this.ctx = canvas.getContext('2d') as CanvasRenderingContext2D;
  }

  get ready(): boolean {
    return !!this.ctx;
  }

  resize(css: number, dpr = window.devicePixelRatio || 1): void {
    if (!this.ctx) return;
    const px = Math.max(1, Math.round(css * dpr));
    if (this.css === css && this.dpr === dpr && this.canvas.width === px) return;
    this.css = css;
    this.dpr = dpr;
    this.canvas.width = px;
    this.canvas.height = px;
  }

  draw(f: CoreFrame): void {
    if (!this.ctx) return;
    const px = this.canvas.width;
    if (px < 4) return;
    const { ctx } = this;
    const half = px / 2;
    // 画布比球大一圈(CSS `#core { inset:-22% }` ⇒ 144%),多出来的余量用来画
    // **沿液面轮廓**的外发光。球半径因此是画布半径的 1/1.44。
    // `seed` 把整颗球缩到一个点(唤醒时从点铺张、一轮结束后收回点再隐藏窗口)。
    // 下限 0.06 而不是 0:完全归零时 R=0,所有渐变的半径都退化,Canvas 会报
    // IndexSizeError 而不是画出「什么都没有」。0.06 在 120px 上是 7px,读作一个点。
    const seed = clampR(f.seed ?? 1, 0.06, 1);
    const R = half * this.ballRatio * seed;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, px, px);
    ctx.translate(half, half);
    this.halo(R, half, f);
    ctx.save();
    // 液面轮廓,不是精确的圆。参考素材实测边界上没有任何亮描边,而轮廓的角向标准差
    // 是半径的 3.3% —— 边界是一条在流动的液面,所以裁剪路径本身就得会流动。
    this.contour(R, f);
    ctx.clip();
    this.body(R, f);
    this.volume(R, f);
    this.corolla(R, f);
    this.hotCore(R, f);
    this.film(R, f);
    this.pulse(R, f);
    ctx.restore();
    this.gate(R, f);
  }

  /** 球外的光。**沿液面轮廓**向外羽化,所以它和球是同一个形状 ——
      这是把外发光从 CSS `box-shadow` 搬进 canvas 的唯一理由:box-shadow 的形状
      跟着 `border-radius` 走,只能是圆角矩形,给不出液面的轮廓。
      两层:近处紧一点、远处散一点,对应原来那对 `--glow` / `--glow-far`。 */
  private halo(R: number, outer: number, f: CoreFrame): void {
    const { ctx } = this;
    const b = bloomAt(f);
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    // 两层都收进 `--orb` 的 10px 余量里(球半径 60 ⇒ outer/R ≈ 1.167)。
    // alpha 压到上一版的 **1/5**:使用者在浅色桌面上圈出的「明显泛光」就是它。
    for (const [reach, a0] of [[1.075, 0.044], [1.165, 0.022]] as [number, number][]) {
      if (R * reach > outer + 0.5) continue;
      const g = ctx.createRadialGradient(0, 0, R * 0.94, 0, 0, R * reach);
      g.addColorStop(0, alpha(f.palette.far, a0 * (0.35 + b * 0.65) * (0.7 + f.amplitude * 0.5)));
      g.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = g;
      this.contour(R, f, reach);
      ctx.fill();
    }
    ctx.restore();
  }

  /** 把液面轮廓铺成当前路径(不描不填,调用方决定)。中点二次曲线连接 ——
      96 个采样点用直线连在 296px 上仍能看出棱,而这条边界要读作液面。 */
  /** 液面轮廓上 [a0,a1] 的一段**开口**路径。流动带的 48 段用它,
      所以那条带也长在液面上,而不是画在一个精确圆上。 */
  private contourArc(R: number, f: CoreFrame, scale: number, a0: number, a1: number): void {
    const { ctx } = this;
    const rs = contourRadii(f);
    const n = rs.length;
    const rAt = (ang: number) => {
      // 在两个采样点之间线性取值:段边界一般不落在采样点上
      const u = ((ang % TAU) + TAU) % TAU / TAU * n;
      const i = Math.floor(u) % n;
      return mix(rs[i], rs[(i + 1) % n], u - Math.floor(u));
    };
    const steps = Math.max(3, Math.ceil((a1 - a0) / 0.035));
    ctx.beginPath();
    for (let k = 0; k <= steps; k++) {
      const ang = a0 + (a1 - a0) * (k / steps);
      const r = rAt(ang) * R * scale;
      const x = Math.cos(ang) * r, y = Math.sin(ang) * r;
      if (k === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
  }

  private contour(R: number, f: CoreFrame, scale = 1, cx = 0, cy = 0): void {
    const { ctx } = this;
    const rs = contourRadii(f);
    const n = rs.length;
    const pt = (i: number) => {
      const a = (TAU * i) / n;
      return { x: cx + Math.cos(a) * rs[i] * R * scale, y: cy + Math.sin(a) * rs[i] * R * scale };
    };
    const mid = (a: { x: number; y: number }, b: { x: number; y: number }) =>
      ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
    let m = mid(pt(n - 1), pt(0));
    ctx.beginPath();
    ctx.moveTo(m.x, m.y);
    for (let i = 0; i < n; i++) {
      const p = pt(i);
      m = mid(p, pt((i + 1) % n));
      ctx.quadraticCurveTo(p.x, p.y, m.x, m.y);
    }
    ctx.closePath();
  }

  /** 球体底色,**不随态变**。参考实测 r=0.91 只剩峰值的 3.9%、r=1.00 是 0.4%,
      所以**最外圈必须淡出到透明**,不能像上一版那样把 `edge` 压在 stop 1 上 ——
      那等于画了一圈暗描边,而暗描边和亮描边一样是「精确球壳」的长相。
      最暗的一档移到 0.88(边界之内),边界本身交给液面轮廓的裁剪。 */
  private body(R: number, f: CoreFrame): void {
    const { ctx } = this;
    const b = bloomAt(f);
    // **渐变原点回到球心**(原来偏在左上 −0.24R,−0.32R)。那个偏心 + 一点白
    // 就是「一个会反光的玻璃球壳」的全部来源 —— 使用者点名要删的正是它。
    // 偏心一去,球体不再有受光方向,它只是一层几乎看不见的散射。
    // 亮片自己的迎光高光**保留**:那是「亮片是弯曲面而不是平贴纸」的唯一证据
    // (实测横向亮度梯度占比 0.254,纯径向渐变该≈0)。
    const g = ctx.createRadialGradient(0, 0, R * 0.06, 0, 0, R * 1.02);
    g.addColorStop(0.62, f.palette.glass);
    g.addColorStop(0.97, f.palette.edge);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g;
    // 玻璃盘的**可见半径随聚合度长**。素材实测边缘半径在一轮里走 0.00→0.94,
    // 而上一版这里画的是一个固定大小的灰盘 —— 并排看最明显的一处:我的球
    // 「多大」几乎不随 agg 变,而素材的球会真的胀缩。等效半径高出 0.09 也来自这里。
    ctx.globalAlpha = clamp01(0.30 + b * 0.70);
    this.contour(R, f, 0.62 + b * 0.38);
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  /**
   * 液态流动带 —— 这一代**取代球壳**的那一层。参考素材的径向亮度剖面在
   * r=0.66→0.78 有一段回升(0.288 → 0.306 → 0.304 → 0.185),而边界上什么都没有;
   * 亮度峰值角在一轮里扫过 0–354°,所以那条带不是定角高光,它在**绕着流**。
   *
   * 画法:一圈粗描边(宽 0.13R)切成 48 段,每段的 alpha 由三条互不整除的角向谐波
   * 合成,整体缓慢旋转。段与段之间刻意重叠 6% 再加模糊,所以看不出接缝。
   * 闸门时不流动(与呼吸、自转、轮廓一起归零),但仍然画 —— 球得有边界。
   */
  private film(R: number, f: CoreFrame): void {
    const b = bloomAt(f);
    if (b < 0.22) return;
    const { ctx } = this;
    const flow = f.gated ? 0 : f.t;
    // 48 段。试过 144 段(想让角向调制更平滑),逐帧实测三项度量一字不变
    // (横向梯度 0.4061 → 0.4064),而每帧多 96 次描边 —— 一个 148px 常驻 60fps
    // 的挂件不该为一个量不出来的差别付这个代价。
    const seg = 48;
    // **0.72R,这是量出来的位置。** 素材的径向亮度剖面在 r=0.66 有一个谷(0.288)、
    // 0.70–0.74 回升到 0.306/0.304、0.79 掉到 0.185、0.91 只剩 0.039 —— 那条软边环
    // 就在 0.70–0.74,而 r>0.85 基本是空的。
    // 上一版放到 0.90R 是为了「不被片体盖住」,方向错了:该改的是**片体太长**
    // (素材的亮片全在环之内),不是把环推到球沿。放在 0.90R 的后果是并排渲染里
    // 我的每一格都看不见边界,而素材每一格都有。
    const rr = R * 0.72;
    // 强度从阈值起长,不是常数 —— 常数会在散开相把中心以外的整圈点亮,
    // 而素材的散开相整颗球是暗的
    const k = (b - 0.22) / 0.78;
    ctx.save();
    ctx.globalCompositeOperation = f.gated || f.state === 'cancelled' ? 'source-over' : 'lighter';
    ctx.filter = `blur(${(R * 0.05).toFixed(2)}px)`;
    // 带宽 0.21R 而不是 0.155R:素材的软边带覆盖 r=0.66→0.79(宽约 0.13R 的**峰**,
    // 但它两侧的肩一直铺到 0.61–0.83)。带子加宽之后它才真的承担外圈的亮度权重 ——
    // 逐帧实测等效半径量程从 0.376–0.439 打开(素材 0.00–0.544)。
    // **三道同心、宽度递增、alpha 递减的描边叠加**，而不是一道 0.24R 的等宽带。
    // 使用者要求「不要出现类似于包裹球体的实体线（虚线也不行）」——
    // 等宽带在低透明度下仍有两条可辨的边，叠三档之后横截面是一条平滑的钟形，
    // 没有边界可描。模糊也从 0.03R 提到 0.05R。
    ctx.strokeStyle = f.palette.far;
    const bands: [number, number][] = [[0.16, 1.0], [0.27, 0.55], [0.38, 0.24]];
    for (let i = 0; i < seg; i++) {
      const a0 = (TAU * i) / seg, a1 = (TAU * (i + 1.06)) / seg;
      const a = (a0 + a1) / 2;
      // 三条谐波:1 次给「一侧亮一侧暗」,2/3 次给带子上的疏密,转速互不整除
      const m = 0.34
        + 0.30 * Math.cos(a + flow * 0.19)
        + 0.22 * Math.cos(2 * a - flow * 0.13)
        + 0.14 * Math.cos(3 * a + flow * 0.27);
      // **指数 2.2 而不是线性**:0.90R 上一圈固定半径的亮环会把亮度加权等效半径
      // 按住(实测 listening 半径摆动掉到 ±6.6%,判据要 ±8%)。带子不能挪 —— 它得在
      // 片体之外才读作边界 —— 但它的**权重**可以随呼吸收放:低相位时几乎熄掉,
      // 质心就重新跟着片体走。这与上一代「体积光把半径钉死」是同一个缺陷的第二次出现
      // 指数从 2.2 收到 **1.5**、基座从 0.03 抬到 0.09:2.2 那一版把边界压得只在高聚合
      // 相看得见（agg 0.44 时 alpha 只有 0.058），并排渲染里素材**每一格都有一圈清楚的
      // 软边环**而我几乎没有 —— 球因此读作「一团飘着的光」而不是「一个装着光的球」。
      // 这一层是这一代唯一承担边界的东西（球壳已按实测删掉），它必须一直看得见。
      // 仍然保留指数（不是常数）：常数会把亮度加权半径钉死，那是上一代的缺陷。
      // 指数 1.8 / 系数 0.62:片体缩到环之内以后,**外圈的权重全靠这一层**。
      // 逐帧实测片体缩短把等效半径的量程压成 0.376–0.436(素材 0.00–0.544),
      // 判据 ≤0.05 因此被打破(0.065)。让这一层在高聚合相变强,外圈的亮度重新长回来,
      // 量程就重新打开 —— 而低聚合相仍然有 0.06 的基座保证边界看得见。
      const alpha = clamp01(m) * (0.06 + Math.pow(k, 1.8) * 1.05);
      if (alpha < 0.004) continue;
      for (const [w, wa] of bands) {
        ctx.globalAlpha = alpha * wa;
        ctx.lineWidth = R * w;
        this.contourArc(R, f, rr / R, a0, a1);
        ctx.stroke();
      }
    }
    ctx.restore();
    ctx.filter = 'none';
  }

  /** 体积光:高聚合时**整个玻璃体都在发光**,不只是中心。
      对照实测缺的就是这一项 —— 没有它,等效半径只有参考的七成,光全挤在球心,
      外圈是暗的;而一个装满光的玻璃球应该整体透亮。强度随 bloom 长,散开相为 0。 */
  private volume(R: number, f: CoreFrame): void {
    const b = bloomAt(f);
    // 阈值从 0.24 提到 **0.40** —— 与白热核同一条线,而那条线是量出来的。
    // 逐帧对照实测我的核亮度比下限卡在 0.48(素材 0.022):低聚合相球心一直是亮的。
    // 根因就在这一项:它的内半径是 0.30R,意味着**球心 30% 被第一个色标铺满**,
    // 于是「整个玻璃体在发光」在低聚合相变成了「球心有一团光」。
    // 素材在 agg<0.40 时中心是暗的,所以这一项在阈值之下不该存在。
    if (b < 0.40) return;
    const { ctx } = this;
    const k = (b - 0.40) / 0.60;
    // 半径也随聚合度长:固定铺满会把亮度加权半径钉死,呼吸的空间摆动就没了
    const reach = R * (0.46 + k * 0.40);
    // 内半径 0.30R 而不是 0.10R:体积光是「整个玻璃体在发光」,不是又一颗球心亮点。
    // 0.10R 那一版把光往球心堆,高聚合相和白热核叠在一起烧成一团
    const g = ctx.createRadialGradient(0, 0, R * 0.30, 0, 0, reach);
    g.addColorStop(0, f.palette.mid);
    g.addColorStop(0.62, f.palette.far);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.globalCompositeOperation = 'lighter';
    // 0.05+k*0.16 → 0.03+k*0.10:listening 的 bloom 0.902 在验收页上把整颗球推成
    // 一团青白。体积光是**一层薄的整体透亮**,不是第二个光源 —— 它铺满球体,所以
    // 它的 alpha 是这里最容易把高聚合相推爆的一项
    ctx.globalAlpha = 0.04 + k * 0.13;
    ctx.fillStyle = g;
    this.contour(R, f, reach / R);
    ctx.fill();
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
  }

  /** 花冠 —— 这一代唯一的主角。每片先 `rotate` 到自己的朝向、再沿自己的轴外移 `off`,
      所以收窄的那一头永远朝内。填充用沿花瓣长轴的径向渐变(实色到 78%,外沿柔化),
      叠加用 `lighter`:光叠加就是变亮,几片挤到一起中心自然过曝 —— 那团白不是画上去的。
      cancelled 与 gated 例外用 `source-over`:一团垮掉的光和一道闸都不该过曝。 */
  private corolla(R: number, f: CoreFrame): void {
    const { ctx } = this;
    const rg = ringAt(f);
    // 成环时**只用主簇一个色**。素材成环相实测只剩**单一色相簇(245°)**,
    // 而上一版按 TINT_ORDER 轮转,画出来是蓝/粉交替 —— 使用者问「形态颜色和素材
    // 一致吗」,不一致的就是这一处:交替配色本身就在说「这些元素不一样」。
    // 成环相不走这张表(每片按 `mixK` 在青→磁之间自己取值,见下面的 `tone`)。
    const tints = [f.palette.far, f.palette.mid, f.palette.alt];
    const flat = f.gated || f.state === 'cancelled';
    // alpha 随聚合度**反向**收:片体越宽、交叠越多,单片就得越淡,
    // 否则高聚合相整颗球烧成白团 —— 参考在 0.983 仍能看见蓝/粉分片
    const b0 = bloomAt(f);
    // 合拢时 n 片挤在 42° 里,交叠面积暴增 —— alpha 再收一档,否则一叠片烧成白刀。
    // 这和随聚合度反向收是同一个道理的两个来源:交叠多了单片就得淡
    // 反向系数 0.11 → **0.16**:listening 的 bloom 到 0.902,三格验收页上它整颗烧成
    // 一团青白、片体全被吞掉。片体越宽、交叠越多,单片就得越淡 —— 这一项就是那个补偿
    // **反向补偿必须是二次的,不是线性的。** 交叠的片数不随 bloom 线性长:高聚合时
    // 七片全部穿过球心,球心处是 7 层相加,而中聚合时只有两三层。线性补偿(0.16·b0)
    // 在 listening(bloom 0.902)仍然把整颗球烧成青白、片体全被吞掉 —— 验收页上
    // 连着两轮都是这一格出问题。二次项只咬高聚合那一段:b0=0.90 时收 0.163,
    // b0=0.52(idle)只收 0.054,中低聚合几乎不动
    // **补偿要咬「这一态有多聚合」,不能咬「这一帧有多聚合」。** 用 b0(含呼吸)那一版
    // 把 listening 的能量摆动从 4.5× 压到 **1.33×** —— 因为呼吸让 b0 涨时 alpha 正好
    // 同步跌,补偿把呼吸本身抵消掉了,球退回一张贴图。改用 bloomLevel()(不含呼吸的
    // 目标值)之后:静态过曝照旧被压住(listening 常数 0.127),而呼吸的明暗摆动
    // 完全不受影响。**看起来像取舍的东西,先问它是不是键错了量。**
    const bl = bloomLevel(f);
    // 成环时**不再收 alpha**:上一版收 34% 是因为 n 片叠在一起要防过曝,
    // 而现在它们互不重叠,收 alpha 只会让整圈变灰。反过来略抬一点 ——
    // 单片不再有邻居帮它加亮,素材的散开相是**一圈明亮的分离亮片**。
    // 成环时 alpha **往上抬 0.9 倍**:素材的散开相是一圈**明亮的**分离亮片
    // (等效半径 0.680,是它全轮的最大值),而单片不再有邻居帮它加亮,
    // 不抬的话整圈读作一圈灰色的碎片
    // 成环时 alpha **抬到 3.4 倍**。这不是审美裁量,是实测:成环相里**整帧的最亮点
    // 就在这些分离的小片上**(块峰值/帧峰值 = 0.93–1.00),而中心是暗的。
    // 上一版抬 1.9 倍画出来是一圈灰蓝碎片 —— 那是「散开了但没亮」,与素材相反。
    let base = (flat ? 0.40 : (0.29 - 0.20 * bl * bl) + f.amplitude * 0.05) * (1 + 2.4 * rg);
    // 多朵向中心收拢的途中,N 朵会逐渐叠在一起 —— 不补偿的话「收缩」看起来是
    // 「先变亮再变暗」而不是「聚到一起」。按朵数与合拢度补一次。
    const nb = blobCenters(f).length;
    if (nb > 1) base /= 1 + (nb - 1) * rg * 0.8;
    const half = outlineHalf();

    for (const c of blobCenters(f)) {
      ctx.save();
      ctx.translate(c.cx * R, c.cy * R);
      ctx.globalCompositeOperation = flat ? 'source-over' : 'lighter';
      // 模糊半径按 R 的比例给:1× 与 2× 屏上柔化程度才一致。
      // 没有这一层,花瓣的轮廓就能被眼睛描出来,那时它是图形不是光
      // 0.014 而不是 0.020:圆润靠轮廓的数学(端点指数 0.5)而不是靠糊,
      // 糊过头片体的边界就读不出来了 —— 参考的透镜片是有边的
      ctx.filter = `blur(${(R * 0.012).toFixed(2)}px)`;
      for (const p of petals(f)) {
        const len = p.len * c.scale * R;
        const wid = p.wid * c.scale * R;
        if (len < 1 || wid < 0.5) continue;
        ctx.save();
        ctx.rotate(p.angle);
        if (p.tilt > 0.001) {
          // 成环相:先走到元素**中心**,再把片体自转到切向,再把轮廓的原点挪回去
          // (`petalOutline` 是从 0 画到 len 的,不是以中心为原点)。
          ctx.translate((p.off + p.len / 2) * c.scale * R, 0);
          ctx.rotate(p.tilt);
          ctx.translate(-len / 2, 0);
        } else {
          ctx.translate(p.off * c.scale * R, 0);
        }
        // 只在最里 26% 是实色,其余一路羽化到透明 —— 花瓣是一团光,不是一块色片。
        // 上一版实色到 78%,渲染出来是七片硬边的矢量花瓣
        // 实色到 46%、其余羽化到透明:再软一档花瓣就数不出来了,再硬一档就读作矢量花瓣。
        // 这两个极端各渲染过一次,0.46 是中间那一档
        //
        // **焦点横跨短轴偏向迎光侧**(第九代):世界光向 LIGHT 恒定,换到这一片的局部
        // 坐标就是 LIGHT − p.angle,所以每片的高光位置各不相同、但都朝着同一个方向 ——
        // 这正是「一叠迎同一束光的弯曲面」与「一堆各自带渐变的色斑」的区别。
        // 参考素材实测亮度梯度的横向分量占比 0.271(散开相 0.48–0.54),
        // 而同心径向渐变的横向分量≈0,上一版就是那样,所以片体读作平的。
        const la = LIGHT - p.angle;
        const lx = Math.cos(la), ly = Math.sin(la);
        // 横跨短轴的偏移量**随聚合度收**:素材实测横向梯度占比在散开相是 0.44–0.54、
        // 高聚合相只有 0.19–0.23 —— 片体挤到一起之后单片的明暗被叠掉了。
        // 上一版给了个常数 0.66,结果全程都是 0.27–0.55,高聚合相偏高。
        // **成环时归零。** 世界坐标的定向光会把六个不同朝向的片体照成六个不同的
        // 明暗分布 —— 使用者问「形状统一了吗」,不统一的一半原因在这里,不在轮廓。
        // 归零之后渐变回到同心,六片因此**长得一样也被照得一样**。
        const eccent = 0.42 * (1 - 0.38 * b0) * (1 - rg);
        const g = ctx.createRadialGradient(
          len * 0.55 + lx * len * 0.08, ly * wid * eccent, len * 0.05,
          len * 0.55, 0, len * 0.96,
        );
        // 成环相按环上位置在青(far)→磁(alt)之间取值;其余形态走三色轮转。
        const tone = p.mixK >= 0 ? blend(f.palette.far, f.palette.alt, p.mixK) : tints[p.tint];
        g.addColorStop(0, tone);
        g.addColorStop(0.52, tone);
        g.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.globalAlpha = base * p.lit;
        ctx.fillStyle = g;
        const pts = petalOutline(len, wid, p.bend, p.taper, p.waist, p.curl);
        // 中点二次曲线:C1 连续且不过冲(Catmull-Rom 会过冲,过冲在轮廓上长小尖刺)。
        // 用 lineTo 连 56 段直线在 296px 上仍能看出棱,而这一代要的正是圆润
        const n2 = pts.length;
        const mid = (a: { x: number; y: number }, b: { x: number; y: number }) =>
          ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
        let m = mid(pts[n2 - 1], pts[0]);
        ctx.beginPath();
        ctx.moveTo(m.x, m.y);
        for (let i = 0; i < n2; i++) {
          m = mid(pts[i], pts[(i + 1) % n2]);
          ctx.quadraticCurveTo(pts[i].x, pts[i].y, m.x, m.y);
        }
        ctx.closePath();
        ctx.fill();

        // 迎光侧的高光 —— 一团**软光**,不是一条线。
        // 上一版在这里描了一道亮边:逐帧脊线含量刚好对上素材的 0.076(我 0.095),
        // 但并排渲染出来是一圈**白色线框**,读作矢量花瓣的描边。素材的脊线来自
        // 弯曲面自己的明暗渐变,不是轮廓线 —— **标量对上了不等于结构对上了**,
        // 这是这一轮最贵的一课。所以改成在片体内再叠一层小半径的偏心渐变:
        // 焦点仍在迎光侧,但边界是羽化的,不会长出一条可描出来的线。
        if (!flat) {
          const hg = ctx.createRadialGradient(
            len * 0.52 + lx * len * 0.10, ly * wid * 0.78, len * 0.02,
            len * 0.52 + lx * len * 0.10, ly * wid * 0.78, len * 0.56,
          );
          // **高光用片体自己的色,不用近白的 `core`。** 使用者的判断是「亮片之间的
          // 重叠部分和素材不匹配」—— 根因就在这儿:往每一片上叠一层近白,两片交叠处
          // 就先被去饱和再被加亮,于是重叠区一律奔向白。素材的重叠区读作**第三个
          // 色相**(青叠粉出淡紫),不是白。同色加色 = 同色变亮,饱和度因此保住。
          // `--lum-core` 从此只服务真正的白热核。
          hg.addColorStop(0, tone);
          hg.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.globalAlpha = clamp01(base * p.lit * 0.50 * (0.30 + 0.70 * b0));
          ctx.fillStyle = hg;
          ctx.fill();   // 复用同一条路径
        }
        ctx.restore();
      }
      ctx.restore();
    }
    ctx.filter = 'none';
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
  }

  /** 白热核:花瓣挤到一起才被逼出来的那团过曝光,所以它的半径与亮度都长在 bloom 上,
      低聚合时直接为 0(`coreGlow()` 已经处理)。分裂成多朵时每朵各有一颗小核 ——
      于是「几路在跑」在余光里也数得清。

      **高斯样的连续衰减,没有实色平台。** 使用者的判断是「中心的亮点太突兀」——
      根因是上一版把实色一直铺到半径的 20% 再一路直线掉到 0:那是一个**有边的盘**,
      而素材的核是一团没有边界、一路化进花瓣里的云。这里改成七档按 exp(−3.2u²) 取的
      alpha,并再叠一层 2.1 倍半径的极淡外晕,让它在花瓣之间化开而不是压在上面。 */
  private hotCore(R: number, f: CoreFrame): void {
    const cg = coreGlow(f);
    if (cg <= 0.001) return;
    const { ctx } = this;
    // 0.04 + cg*0.42 而不是 0.06 + cg*0.86:上一版两遍叠起来在高聚合相把整颗球
    // 烧成一团白,花瓣全被吞掉 —— 「不突兀」不等于「更大更亮」,素材的核在最亮那一格
    // 仍然看得见花瓣穿过它。
    const peak = clamp01(0.025 + cg * 0.30);
    ctx.globalCompositeOperation = 'lighter';
    for (const c of blobCenters(f)) {
      const r = cg * c.scale * R;
      if (r < 0.6) continue;
      for (const [mul, k] of [[1.6, 0.22], [1.0, 1.0]] as [number, number][]) {
        const rr = r * mul;
        const g = ctx.createRadialGradient(c.cx * R, c.cy * R, 0, c.cx * R, c.cy * R, rr);
        for (let s = 0; s <= 6; s++) {
          const u = s / 6;
          g.addColorStop(u, alpha(f.palette.core, Math.exp(-3.2 * u * u) * k));
        }
        g.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.globalAlpha = peak;
        ctx.fillStyle = g;
        this.contour(R, f, rr / R, c.cx * R, c.cy * R);
        ctx.fill();
      }
    }
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
  }

  /** 点击脉冲:一道向外扩散的薄环,一次性、不常驻。 */
  /** 点击脉冲。**不再是一道扩散的环** —— 环是线，而线一律不许出现。
      改成一次沿液面向外涌的**亮度**：一个从内向外推的软边渐变填充，
      到达边界时消失。信号仍然是「一次性、有因果」，只是载体从轮廓换成了光。 */
  private pulse(R: number, f: CoreFrame): void {
    if (f.pulse <= 0.001) return;
    const { ctx } = this;
    const p = clamp01(f.pulse);
    const front = 0.30 + (1 - p) * 0.74;          // 波前从球心附近推到边界外
    const g = ctx.createRadialGradient(0, 0, R * Math.max(0, front - 0.34), 0, 0, R * front);
    g.addColorStop(0, 'rgba(0,0,0,0)');
    g.addColorStop(0.72, alpha(f.palette.core, p * 0.16));
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.fillStyle = g;
    this.contour(R, f, Math.min(1, front));
    ctx.fill();
    ctx.restore();
  }

  /**
   * 闸门环 —— 第九代把这里剩下的都删了,只留它。
   *
   * 删掉的四项:整圈白色内描边、左上镜面弧、右下暖色弧、边缘辉光。理由是实测:
   * 参考素材的径向亮度剖面在 r=0.91 只剩峰值的 3.9%、r=1.00 是 0.4%,**边界上
   * 什么都没有**;而亮度峰值角一轮里扫过 0–354°,定角镜面高光同样不存在。
   * 球的边界现在由液面轮廓(裁剪)+ 流动带(film)承担,那两项是量出来的。
   *
   * 琥珀环留着,而且刻意反着来:它是硬边、定圆、整圈 —— 这个软塌塌的世界里唯一
   * 一个精确的圆。一团流动的光表达不了「拦住」,一道规整的闸可以。它是安全语义,
   * 不参与素材取样。
   */
  /** 闸门**不再画任何描边**。使用者要求「去掉闸门状态最外层的那条线，包括其他形态
      都不要出现类似于包裹球体的实体线（虚线也不行）」—— 上一代把那道琥珀环当成
      「这个软塌塌的世界里唯一一个精确的东西」，是刻意的；这条设计决定因此被推翻。
      闸门的辨识改由**三项同时为 0**（呼吸、自转、形态漂移）+ 整体琥珀色承担，
      两项都不是线：一个僵住的活物 + 一身警示色，余光里仍然最抢眼。 */
  private gate(_R: number, _f: CoreFrame): void {
    /* 有意为空：保留方法是为了 draw() 的调用点不动（ADR 001 渲染器接口稳定）。 */
  }
}
