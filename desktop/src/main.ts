import './style.css';

const app = document.querySelector<HTMLElement>('#app')!;
const status = document.querySelector<HTMLElement>('#status')!;
const labels: Record<string, string> = {idle:'待机',wake:'已唤醒',listening:'聆听中',thinking:'思考中',speaking:'正在回复',error:'需要处理'};

function setState(state: string, amplitude = 0.35) {
  app.dataset.state = state;
  app.style.setProperty('--amplitude', String(Math.max(0.12, Math.min(1, amplitude))));
  status.textContent = labels[state] ?? state;
}

window.addEventListener('evox-voice-state', (event) => {
  const detail = (event as CustomEvent).detail ?? {};
  setState(detail.state ?? 'idle', detail.amplitude ?? 0.35);
});

document.querySelector('#orb')?.addEventListener('click', () => setState(app.dataset.state === 'idle' ? 'listening' : 'idle', 0.7));

Object.assign(window, {
  __READY__: true,
  render_state_to_text: () => JSON.stringify({state: app.dataset.state, label: status.textContent}),
  step: (_ms: number) => render(),
  setVoiceState: setState,
});

let phase = 0;
function render() {
  phase += 0.035;
  app.style.setProperty('--phase', String(Math.sin(phase)));
}
function loop() { render(); requestAnimationFrame(loop); }
if (!new URLSearchParams(location.search).has('test')) loop();
setState('idle');
