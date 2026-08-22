import './style.css';

/* Vox — main.ts
   六态球体 + 流式回复 + 工具确认卡(FR-6.13)。
   测试钩子保持兼容现有 SIM 测试。 */

const app = document.querySelector<HTMLElement>('#app')!;
const orb = document.querySelector<HTMLButtonElement>('#orb')!;
const eyeL = document.getElementById('eyeL')!;
const eyeR = document.getElementById('eyeR')!;
const status = document.querySelector<HTMLElement>('#status')!;
const panel = document.querySelector<HTMLElement>('#panel')!;
const reply = document.querySelector<HTMLElement>('#reply')!;
const confirmCard = document.querySelector<HTMLElement>('#confirm')!;
const confirmCmd = document.querySelector<HTMLElement>('#confirm-cmd')!;
const confirmReason = document.querySelector<HTMLElement>('#confirm-reason')!;
const confirmDeny = document.querySelector<HTMLButtonElement>('#confirm-deny')!;
const confirmAllow = document.querySelector<HTMLButtonElement>('#confirm-allow')!;
const particles = document.getElementById('particles')!;

const labels: Record<string, string> = {
  idle: '待机',
  listening: '聆听中',
  thinking: '思考中',
  speaking: '正在回复',
  cancelled: '已取消',
  error: '需要处理',
};

let state: string = 'idle';
let blinkEnabled = true;
let blinkTimer: number | null = null;
let expandTimer: number | null = null;
let confirmPending: {cmd: string; reason?: string; settle: (v: boolean) => void} | null = null;

/* ============ 状态切换 ============ */
function setState(next: string, amplitude = 0.35) {
  if (!['idle','listening','thinking','speaking','cancelled','error'].includes(next)) {
    console.warn(`[wake] invalid state: ${next}`);
    return;
  }
  const prev = state;
  state = next;
  app.dataset.state = next;
  app.style.setProperty('--amplitude', String(Math.max(0.12, Math.min(1, amplitude))));
  status.textContent = labels[next] ?? next;
  orb.setAttribute('aria-label', `Vox 状态: ${labels[next] ?? next}`);

  // 一次性动作：进入时放一次
  if (next === 'cancelled' && prev !== 'cancelled') {
    orb.classList.remove('collapsing');
    void orb.offsetWidth;
    orb.classList.add('collapsing');
    setTimeout(() => orb.classList.remove('collapsing'), 400);
  }
  if (next === 'error' && prev !== 'error') {
    orb.classList.remove('shaking');
    void orb.offsetWidth;
    orb.classList.add('shaking');
    setTimeout(() => orb.classList.remove('shaking'), 500);
  }

  // error/cancelled 不眨眼
  if ((next === 'error' || next === 'cancelled') && blinkEnabled) {
    blinkEnabled = false;
    if (blinkTimer) clearTimeout(blinkTimer);
  } else if (prev === 'error' || prev === 'cancelled') {
    blinkEnabled = true;
    schedBlink();
  }
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

function startReply() {
  replyText = '';
  reply.textContent = '';
  if (caretNode) caretNode.remove();
  caretNode = document.createElement('span');
  caretNode.className = 'caret';
  reply.appendChild(caretNode);
  expand();
}

function appendReplyChunk(chunk: string) {
  replyText += chunk;
  if (caretNode && caretNode.parentNode) caretNode.remove();
  reply.textContent = replyText;
  if (caretNode) reply.appendChild(caretNode);
  reply.scrollTop = reply.scrollHeight;
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
      confirmAllow.onclick = null;
      confirmDeny.onclick = null;
      resolve(v);
    };

    confirmCmd.textContent = cmd;
    confirmCmd.scrollTop = 0;
    confirmReason.textContent = reason || '';
    confirmCard.hidden = false;
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
  const sb = layoutBox(status);
  const rects: HitRegion['rects'] = [];
  let bottom = sb.y + sb.h;
  let right = Math.max(ob.x + ob.w, sb.x + sb.w);

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

/* ============ 点击：squish + 粒子 ============ */
orb.addEventListener('click', (ev) => {
  // 拖完 OS 会补一个 click，要吞掉；但键盘激活的 click 的 detail 是 0，不能连它一起吞
  if (dragged && ev.detail > 0) { dragged = false; return; }
  dragged = false;

  orb.classList.remove('squishing');
  void orb.offsetWidth;
  orb.style.transform = '';
  orb.classList.add('squishing');
  setTimeout(() => orb.classList.remove('squishing'), 360);

  const fluid = orb.querySelector('.inner-fluid') as HTMLElement;
  if (fluid) {
    fluid.style.filter = 'brightness(1.15)';
    setTimeout(() => { fluid.style.filter = ''; }, 350);
  }

  for (let i = 0; i < 8; i++) {
    const p = document.createElement('i');
    p.className = 'particle';
    const size = 2 + Math.random() * 3;
    const ang = (Math.PI * 2 / 8) * i;
    const dist = 15 + Math.random() * 25;
    p.style.width = p.style.height = size + 'px';
    p.style.setProperty('--px', Math.cos(ang) * dist + 'px');
    p.style.setProperty('--py', Math.sin(ang) * dist + 'px');
    particles.appendChild(p);
    setTimeout(() => p.remove(), 450);
  }

  // 事件桥接：点击可切态（测试用途）
  window.dispatchEvent(new CustomEvent('vox-orb-click', {detail: {state}}));
});

/* ============ 眨眼 ============
   四个分支的意义不是「随机」，而是让静止的球看着像有内在节律：
   偶发的连续两下、犹豫的半闭再全闭、困倦的慢眨、以及大多数时候的普通一下。 */
function blinkOnce(closeMs: number, ratio = 0.14): Promise<void> {
  return new Promise((done) => {
    const h = getComputedStyle(eyeL).height;
    const shut = `${Math.max(2, parseFloat(h) * ratio)}px`;
    for (const e of [eyeL, eyeR]) {
      e.style.height = shut;
      e.style.borderRadius = '2px';
    }
    window.setTimeout(() => {
      for (const e of [eyeL, eyeR]) {
        e.style.height = '';
        e.style.borderRadius = '';
      }
      done();
    }, closeMs);
  });
}

function schedBlink() {
  if (blinkTimer) clearTimeout(blinkTimer);
  // 20% 概率进入「密集眨眼」窗口，其余是常态间隔
  const base = Math.random() < 0.2
    ? 800 + Math.random() * 1500
    : 2500 + Math.random() * 5000;

  blinkTimer = window.setTimeout(async () => {
    if (!blinkEnabled) return;
    const roll = Math.random();
    if (roll < 0.15) {
      // 连续两下
      await blinkOnce(80);
      await new Promise((r) => setTimeout(r, 110));
      await blinkOnce(80);
    } else if (roll < 0.3) {
      // 半闭 → 全闭
      await blinkOnce(90, 0.45);
      await new Promise((r) => setTimeout(r, 60));
      await blinkOnce(85);
    } else if (roll < 0.4) {
      // 慢眨
      await blinkOnce(250 + Math.random() * 150);
    } else {
      await blinkOnce(70 + Math.random() * 30);
    }
    if (blinkEnabled) schedBlink();
  }, base);
}

/* ============ 漂浮与呼吸 ============
   三条不同频率的正弦相加，周期互不整除，所以看不出循环。 */
let t = 0;
const seed = Math.random() * 100;
let curScale = 1;
let phase = 0;

function render() {
  const floatY = Math.sin(t * 1.3) * 3.5 + Math.sin(t * 0.67) * 2 + Math.sin(t * 2.3) * 0.6;
  const floatR = Math.sin(t * 0.9) * 0.3 + Math.sin(t * 0.37) * 0.2;
  const fidgetX = Math.sin(t * 0.23 + seed) * 0.6;

  // speaking 时呼吸幅度随音量放大，其余状态是恒定的浅呼吸
  const amp = state === 'speaking'
    ? parseFloat(getComputedStyle(app).getPropertyValue('--amplitude')) || 0.35
    : 0.35;
  const targetScale = 1 + Math.sin(t * 2) * 0.01 * (1 + amp) + Math.sin(t * 0.8) * 0.005;
  curScale += (targetScale - curScale) * 0.08;

  if (!orb.classList.contains('squishing') && !orb.classList.contains('shaking')) {
    orb.style.transform =
      `translate(${fidgetX.toFixed(2)}px, ${floatY.toFixed(2)}px) ` +
      `rotate(${floatR.toFixed(3)}deg) scale(${curScale.toFixed(4)})`;
  }

  phase += 0.035;
  app.style.setProperty('--phase', String(Math.sin(phase)));
  t += 0.016;

  // 面板高度随流式文本增长，每 6 帧量一次；reportLayout 内部按内容去重，不变就不过 IPC
  if ((frame = (frame + 1) % 6) === 0) reportLayout();
}
let frame = 0;

function loop() {
  render();
  requestAnimationFrame(loop);
}

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
    case 'llm.delta':
      if (typeof p.text === 'string') appendReplyChunk(p.text);
      break;
    case 'turn.done':
    case 'turn.cancelled':
      endReply();
      break;
    case 'task.failed':
      // 失败要看得见。payload 只有 error 与 task_id，没有正文可漏。
      if (typeof p.error === 'string') appendReplyChunk(`\n[失败] ${p.error}`);
      endReply();
      break;
    case 'tool.refused':
      if (typeof p.reason === 'string') appendReplyChunk(`\n[已拒绝] ${p.reason}`);
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
  setBlinkEnabled: (v: boolean) => {
    blinkEnabled = v;
    if (!v && blinkTimer) clearTimeout(blinkTimer);
    if (v) schedBlink();
  },
  setVisible,
  // 命中区域由前端量、Rust 消费；导出供 SIM 断言
  measureHitRegion,
  reportLayout: () => reportLayout(true),
  render_layout_to_text: () => JSON.stringify(lastRegion ?? measureHitRegion()),
  render_panel_to_text: () => JSON.stringify({
    expanded: app.dataset.expanded === 'true',
    reply: replyText,
    confirm: confirmCard.hidden ? null : {cmd: confirmCmd.textContent, reason: confirmReason.textContent},
  }),
});

if (!new URLSearchParams(location.search).has('test')) {
  loop();
  schedBlink();
}

setState('idle');
reportLayout(true);
window.addEventListener('resize', () => reportLayout(true));
