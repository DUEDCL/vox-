# EvoX/EvoMap Community Search

> Search date: 2026-07-22 (first pass); updated 2026-07-22 (installed-plugin audit, second pass)

## Result

No verifiable EvoX-native voice asset was found through the currently available EvoX channels and application-layer web search. `evox_channel_discover` returned `No channel directory configured` because `EVOX_HUB_URL` is not configured in this session. Web search found a generic EvoMap plugin-extension bounty, but no installable voice plugin ID, release, source tree, or compatibility manifest.

This means not discovered through the available entry points, not proof that the wider EvoMap ecosystem contains no such asset.

## Installed-plugin audit (2026-07-22)

Audited all assets reachable from this agent session for audio capture, background lifecycle, current-session send, TTS, system window, and desktop UI capability:

| Asset type | Name | Voice/audio | Session send | System window / overlay | Windows | License | Verdict |
|---|---|---|---|---|---|---|---|
| skill | evox-officecli-excel-studio | no | no | no | yes (host) | not declared (user skill) | irrelevant |
| skill | evox-officecli-ppt-studio | no | no | no | yes (host) | not declared (user skill) | irrelevant |
| skill | evox-theme-studio | no | no | desktop theme JSON only | yes (host) | not declared (user skill) | theming only, no overlay |
| skill | github-repo-digest | no | no | no | yes | wraps gitingest | research helper |
| skill | hatch-pet | no | no | desktop pet sprite (app-level) | yes (host) | not declared (user skill) | sprite only, no audio |
| skill | visual-demo | no | no | no | yes | not declared (user skill) | useful for canvas/WebGL demo scaffolding |
| MCP | computer-use (streamable-http, 127.0.0.1) | no audio tools exposed | no | desktop control via screenshots | yes | local plugin, not published | fallback UI automation only |
| MCP | evox-sessions (streamable-http, 127.0.0.1) | no | **yes** — archive_session / collect_result etc. | no | yes | local plugin, not published | session-bridge building block |
| MCP | pdf (stdio, npx mcp-pdf) | no | no | no | yes | upstream package | irrelevant |
| MCP | playwright (stdio, npx @playwright/mcp) | no | no | no | yes | Apache-2.0 (upstream) | browser preview/testing helper |
| cdylib extensions | none installed | — | — | — | — | — | nothing to reuse |
| subagents | voice-plugin-developer etc. | no tools of its own | no | no | — | user-authored prompt | orchestration only |

Key findings:

- **Audio capture / wake word / STT / TTS: no installed plugin provides any of these.** They must come from open-source components (see `open-source-landscape.md`).
- **Current-session send: no voice plugin, but `evox-sessions` MCP proves a local authenticated session channel exists** (`archive_session`, `collect_result`, session roster). The `core/session_bridge.py` ConversationTransport abstraction should target this channel rather than inventing a new IPC.
- **System overlay window: no installed plugin exposes a system-level window/overlay API.** The independent Tauri wake window in `desktop/` remains the only verifiable path for the liquid-glass orb.
- Channel directory (`EVOX_HUB_URL`) is unconfigured, so the EvoMap online registry could not be enumerated from this session; presence/roster tools only see the local session.

## Evidence

Queries used: `voice`, `voice assistant`, `wake word`, `hotword`, `speech`, `microphone`, `STT`, `TTS`, `system overlay`, `desktop assistant`, `语音助手`, `语音唤醒`, `唤醒词`, `麦克风`.

Relevant result: [Tool Functionality Extension via Plugins](https://evomap.ai/bounty/cm4f59215fcb6ab27afb6aa9c). It is a generic bounty, not a voice implementation, and has no installable artifact in the result.

## Decision

Do not install or imitate an alleged native plugin without a verifiable asset ID, source, version, permissions, and EvoX compatibility declaration. Continue with open-source component evaluation and keep the integration boundary replaceable. Use the local `evox-sessions` MCP channel as the session-bridge transport candidate; build audio capture and the overlay window from open-source components.
