/* 参数控件面板。使用者：「给我在 localhost:5273/ 添加可直接修改参数及时看到效果的控块，
   我自己调，但是每一个参数你要解释清楚是什么，我想要的就是类似于一个『米』字在最无规则旋转」。

   面板直接写 `TUNE`（core.ts 导出的那个对象），而渲染器每帧读它 —— 所以拖动滑块的下一帧
   就生效，不重载、不丢当前的动画相位。**没有第二份默认值**：滑块的初值从 `TUNE` 读，
   「恢复默认」恢复到页面加载时那一份快照，所以 core.ts 里改了默认值这里跟着变。

   「导出」按钮吐出一段可以直接贴回 core.ts 的字面量 —— 调好的一组数不用手抄。 */
import { TUNE } from './src/core';

type Slider = {
  key: keyof typeof TUNE;
  label: string;
  min: number; max: number; step: number;
  why: string;
};

type Group = { name: string; items: Slider[] };

/** 「米」字要的是：片心重合（offBase→0）+ 片体细长（ratio 小）+ 自转快（selfW 大）。
    每一条的 `why` 是给使用者看的解释，不是给我看的注释。 */
const GROUPS: Group[] = [
  {
    name: '几何 · 同一个椭圆片，六片各自形变',
    items: [
      { key: 'ellA', label: 'ellA 长半轴', min: 0.3, max: 1.0, step: 0.01,
        why: '椭圆的长半轴（球可见半径的比例）。0.74 ⇒ 长轴几乎横跨整个球。低聚合态（待机/取消）自动取它的 72%。' },
      { key: 'ellB', label: 'ellB 短/长比', min: 0.15, max: 1.0, step: 0.01,
        why: '短半轴 ÷ 长半轴。**1.0 = 正圆**，那是你最后确认的形状。弯曲与扭曲之后圆片的投影自然成为各种叶形，所以「形态多变」不靠这一项。调小会回到椭圆（0.30 就是「米」字的笔画）。' },
      { key: 'bend', label: 'bend 弯曲角(度)', min: 0, max: 180, step: 1,
        why: '**单位是度，数值直接来自你的工程**：E3D Deform 面板实测「弯曲角度 = −50.76°」。片体沿长轴弯过的总角就是它 × 每片的系数（±0.34–1.0）。我此前用的是无量纲系数，换算出来最大 86° —— 大了七成。' },
      { key: 'wig', label: 'wig 浮动(度)', min: 0, max: 60, step: 1,
        why: '**`wiggle(0.2, 15)` 里的 15 就是它，单位是度不是百分比。** 工程里三个 Deform 属性都挂着 `wiggle(0.2,15)`。绝对度数很重要：±15° 相对弯曲的 50.8° 是 ±30%，相对扭曲的 21° 是 ±71% —— 一个相对比例表达不了这个差别。' },
      { key: 'twist', label: 'twist 扭曲角(度)', min: 0, max: 120, step: 1,
        why: '**单位是度，来自工程**：「扭曲 X = −10.41°、扭曲 Y = −21°」。我此前那版换算出来是 92° —— **大了四倍多**，这就是形状一直怪的一个具体原因。每片的系数带符号，所以扭向左右相反。' },
      { key: 'tiltXY', label: 'tiltXY 出屏倾角', min: 0, max: 1.4, step: 0.02,
        why: '绕 X / Y 轴的慢摆幅度（rad）。**小才符合「旋转方向是面对摄像机」** —— 大了片体会频繁转到侧面、投影收成一条线。它给的是「片体在往里/往外翻」的 3D 感。' },
      { key: 'offBase', label: 'offBase 心距', min: 0, max: 1.0, step: 0.01,
        why: '六片的片心离整组中心多远。**默认 0 = 六个中心完全重合**（你要的）。往大调六片会散成一朵花；成环相（思考态）不受它影响，那一相自己推到一圈上。' },
      { key: 'feather', label: 'feather 羽化', min: 0.08, max: 1, step: 0.02,
        why: '**这一项是 AE 流程里那张贴图。** 你的合成 “2” 是一个反转 + 羽化 1000 的圆遮罩，接在 E3D 材质的 Alpha 通道上 —— 片体因此是「中心实、边缘一路羽化到透明」的一团光，而我此前一直画的是轮廓内均匀填充的硬边形状（所以读作色块）。0.34 = 从 34% 半径起就明显衰减；调到 1 会回到硬边。加色下「alpha→0」与「颜色→黑」等价，所以它是一条径向渐变而不是遮罩。' },
      { key: 'opacity', label: 'opacity 单片亮度', min: 0.1, max: 1.2, step: 0.02,
        why: '单片的亮度强度。合成是**加色**（你的流程：材质混合模式 = 屏幕/添加），所以中心亮度 ≈ 这个值 × 叠在那儿的片数 × 曝光。中心过曝先调 feather（缩小实心区），再调这一项。' },
      { key: 'vibrance', label: 'vibrance 自然饱和度', min: 0, max: 1.5, step: 0.02,
        why: '**工程实测 +60 ⇒ 0.60**（我从你的 .aep 里解出来的，同一条链上还有 +30 / +100 / −20 / −52 几层）。做法与 AE 的 Vibrance 同构：往「灰 → 原色」外推，低饱和的像素提升得更多。0 = 关掉，你会看到颜色一下子灰下去 —— 那说明这一层在观感里占的比重不小。' },
      { key: 'turb', label: 'turb 湍流置换', min: 0, max: 0.4, step: 0.01,
        why: '**你工程里挂了 `Turbulent Displace`（数量 7 / 大小 150），教程里没提这一条。** 它在 2D 上把画好的形状有机地扭动，与 E3D 的 Twist/Bend 是两回事 —— 是「流体感」的一个独立来源。Canvas 2D 做不了逐像素置换，所以用轮廓的低频谐波扰动近似（第九代液面轮廓用的同一手法）。' },
      { key: 'turbN', label: 'turbN 湍流频率', min: 1, max: 8, step: 1,
        why: '沿轮廓一圈几个波。「大小 150」对应低频、大尺度的扰动，所以 3 左右；调大会变成细密的锯齿。' },
      { key: 'glow', label: 'glow 后期辉光', min: 0, max: 1, step: 0.02,
        why: '**这是你流程最后那个调整图层上的 Deep Glow。** 它把画好的一帧整幅糊开一层再加回去 —— 所以「发光」作用在合成结果上，不是材质属性（我此前一直在让材质自己亮，方向是反的）。0 = 关掉，看清片体本身的颜色与形状。' },
      { key: 'glowR', label: 'glowR 辉光半径', min: 0.01, max: 0.3, step: 0.005,
        why: '辉光的模糊半径（× 球半径）。大 ⇒ 光晕铺得远、整颗球更「湿」；小 ⇒ 只在亮部边上镶一圈。' },
      { key: 'exposure', label: 'exposure 曝光', min: 0.6, max: 4, step: 0.05,
        why: '**素材物理环境实测 2.00**（你给的截图：曝光 2.00、伽玛 1.00、色彩白）。颜色算完之后整体乘它再钳制，过曝的部分自然往白里走。半透明覆盖没有加色累积，所以「发光」现在由它和中心光核承担 —— 想更亮就调它，不要去调 opacity。' },
    ],
  },
  {
    name: '黑雾 · 「被遮挡的部分几乎不可见」就是它',
    items: [
      { key: 'fogNear', label: 'fogNear 雾起点', min: -1, max: 1, step: 0.02,
        why: '深度大于这个值的地方完全没有雾（z 越大越靠近你）。调小 ⇒ 更多片体落进雾里。' },
      { key: 'fogRange', label: 'fogRange 雾范围', min: 0.2, max: 3, step: 0.05,
        why: '从起点再往里多深变成**全黑**。合成是加色，而加色里「黑」= 不贡献，所以远处的片会自然消失 —— 这就是素材里「被遮挡的部分几乎不可见」的真正机制（你给的雾参数截图：黑、100%、线性）。调小 ⇒ 前后对比更狠。' },
    ],
  },
  {
    name: '运动 · 面对摄像机的自转 + 两层圆周',
    items: [
      { key: 'selfW', label: 'selfW 自转', min: 0, max: 4, step: 0.05,
        why: '**绕视线轴的自转**（rad/s）—— 你说的「旋转方向是面对摄像机」，主运动就是它。片心重合时它几乎是唯一看得见的运动。' },
      { key: 'orbW', label: 'orbW 公转', min: 0, max: 4, step: 0.05,
        why: '片心绕整组中心公转的角速度（rad/s）。片心重合（offBase→0）时看不出来。' },
      { key: 'hubW', label: 'hubW 整组进动', min: 0, max: 3, step: 0.05,
        why: '整组中心绕球心进动的角速度（rad/s）。**整团光的位移只有这一层给。** 它与 orbW 不许接近（否则两层锁相，等于只有一层）。' },
      { key: 'hubTilt', label: 'hubTilt 进动半径', min: 0, max: 0.6, step: 0.01,
        why: '整团光绕球心画的那个圈有多大。调太大会让球的另一半空掉。' },
      { key: 'dirSpread', label: 'dirSpread 无规则度', min: 0, max: 1, step: 0.02,
        why: '各片的转向与速率差异强度。**0 = 六片同速同向（整组像一个刚体）；1 = 方向（±）与倍率（0.74–1.36）全开，有的顺时针有的逆时针、快慢不一 = 最无规则。**' },
    ],
  },
  {
    name: '球壳 · 流边',
    items: [
      { key: 'shellW', label: 'shellW 带宽', min: 0.004, max: 0.30, step: 0.004,
        why: '这一层的宽度（× 球半径）。它不是「给球描个边」—— 它是**球内壁被最近那片染上的颜色**。0.024 只染到球壳那一线；调到 0.15 会往内壁染进 0.15R 深。颜色沿圈由最近两片插值 ⇒ 六片一转，染色带跟着流。' },
      { key: 'shellA', label: 'shellA 亮度', min: 0, max: 3, step: 0.05,
        why: '球壳的亮度系数。整圈发白通常不是这一项太大，而是 shellReach 太大或 shellPow 太小（每个方位都有片够得着 ⇒ 整圈等亮 ⇒ 加色叠成白）。' },
      { key: 'shellReach', label: 'shellReach 判定距离', min: 0.3, max: 2, step: 0.02,
        why: '「多远之外就不算贴着这一段」的三维距离阈值。**越小 ⇒ 亮带越集中在真正贴近的那一段。**' },
      { key: 'shellPow', label: 'shellPow 对比', min: 1, max: 9, step: 0.5,
        why: '距离权重的幂次。越大 ⇒ 亮暗对比越强、亮带越窄越干脆。' },
    ],
  },
  {
    name: '中心光核 + 片体颜色曲线',
    items: [
      { key: 'coreR', label: 'coreR 半径', min: 0.3, max: 4, step: 0.05,
        why: '光核半径的倍数（乘在每态自己的 coreGlow 基准上）。待机态基准是 0.163R，所以 1.75 ⇒ 约 0.29R。' },
      { key: 'coreA', label: 'coreA 亮度', min: 0, max: 0.9, step: 0.01,
        why: '光核峰值 alpha 的斜率。它加色叠在片体之上，而片体本身已经在中心叠出一团亮了 —— 这一项只是补那团亮的核。' },
      { key: 'coreSoft', label: 'coreSoft 柔和度', min: 0.6, max: 6, step: 0.1,
        why: '高斯衰减的指数。**越小越柔和**（同样的亮度铺得更开）；越大越收成一个点，大到一定程度就读作「贴上去的亮点」。' },
      { key: 'tintLit', label: 'tintLit 亮档白度', min: 0, max: 0.6, step: 0.02,
        why: '片体最亮那一档往白里带多少。0 = 纯簇色（很饱和）；0.22 = 被照亮的半透体；0.4 以上会开始洗掉色相。' },
      { key: 'tintDim', label: 'tintDim 暗部深度', min: 0, max: 1, step: 0.02,
        why: '片体最暗那一档压到多黑。1 = 对比最强、立体感最明显；调低会让整片趋于均匀的一块色。' },
    ],
  },
];

const DEFAULTS: Record<string, number> = JSON.parse(JSON.stringify(TUNE));

/** 「米」字预设：片心几乎重合（0.02 rad）+ 笔画细长（长短比 3.85）+ 自转拉到 2.3 rad/s
    + 差异拉满。`dish` 提到 0.46 是必须的 —— 片心一重合，穿插就只剩凹陷这一个来源。 */
const MI = {
  offBase: 0.02, ellA: 0.80, ellB: 0.26, bend: 3.4, twist: 3.2, tiltXY: 0.30,
  selfW: 2.10, orbW: 0.90, hubW: 0.55, hubTilt: 0.10, dirSpread: 1,
  opacity: 0.26, exposure: 2.5, glow: 0.48, glowR: 0.09, feather: 0.30, wig: 0.22, fogNear: 0.30, fogRange: 1.05,
};



export function mountTune(host: HTMLElement, onChange: () => void): void {
  // `?bare=1` 打开时面板默认折叠 —— 它有 20 条滑块 + 说明，展开时占满一屏，
  // headless 截图会拍不到球。折叠起来才能一次拍到「面板 + 球」。
  const bare = new URLSearchParams(location.search).get('bare') === '1';
  const parts: string[] = [`<details${bare ? '' : ' open'}><summary><b>参数控件</b> —— 拖动即刻生效（每帧读，不重载）。悬停看不到说明的话，说明就在滑块下面那行。</summary>`];
  for (const g of GROUPS) {
    parts.push(`<div class="grp">${g.name}</div><div class="rows">`);
    for (const it of g.items) {
      const v = TUNE[it.key] as number;
      parts.push(
        `<div class="row"><label for="t_${it.key}">${it.label}</label>`
        + `<input type="range" id="t_${it.key}" data-k="${it.key}" min="${it.min}" max="${it.max}" step="${it.step}" value="${v}">`
        + `<output id="o_${it.key}">${v}</output>`
        + `<div class="why">${it.why}</div></div>`,
      );
    }
    parts.push('</div>');
  }
  parts.push(
    '<div class="acts">'
    + '<button id="t_reset">恢复默认</button>'
    + '<button id="t_mi">套用「米」字预设</button>'
    + '<button id="t_export">导出当前参数</button>'
    + '</div><textarea id="t_out" readonly placeholder="点「导出当前参数」，这里会给出可以直接贴回 core.ts 的一段字面量"></textarea>',
  );
  parts.push('</details>');
  host.innerHTML = parts.join('');

  const sync = (): void => {
    for (const g of GROUPS) {
      for (const it of g.items) {
        const el = document.getElementById(`t_${it.key}`) as HTMLInputElement | null;
        const out = document.getElementById(`o_${it.key}`);
        if (el) el.value = String(TUNE[it.key]);
        if (out) out.textContent = String(TUNE[it.key]);
      }
    }
  };

  host.addEventListener('input', (e) => {
    const el = e.target as HTMLInputElement;
    const k = el.dataset.k as keyof typeof TUNE | undefined;
    if (!k) return;
    (TUNE[k] as number) = Number(el.value);
    const out = document.getElementById(`o_${k}`);
    if (out) out.textContent = el.value;
    onChange();
  });

  document.getElementById('t_reset')?.addEventListener('click', () => {
    for (const k of Object.keys(DEFAULTS)) (TUNE as Record<string, number>)[k] = DEFAULTS[k];
    sync(); onChange();
  });

  // 「米」字：六片图形中心严格重合 + 细长笔画 + 自转拉快 + 差异拉满
  const applyMi = (): void => {
    Object.assign(TUNE, MI);
    sync(); onChange();
  };
  document.getElementById('t_mi')?.addEventListener('click', applyMi);

  // 「竖立面对」：六个碗的法向全部拉到视线轴 ⇒ 正面朝人

  // `?preset=mi` / `?preset=face` 直接带着一组参数打开 —— 方便把一组值发给别人看
  const preset = new URLSearchParams(location.search).get('preset');
  if (preset === 'mi') applyMi();
  if (preset === 'face') applyFace();

  document.getElementById('t_export')?.addEventListener('click', () => {
    const ta = document.getElementById('t_out') as HTMLTextAreaElement | null;
    if (!ta) return;
    const body = Object.entries(TUNE)
      .map(([k, v]) => `  ${k}: ${v},`)
      .join('\n');
    ta.value = `export const TUNE = {\n${body}\n};`;
    ta.select();
  });
}
