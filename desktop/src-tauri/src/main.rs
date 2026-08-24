#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Vox —— 唤醒球的宿主窗口。
//!
//! 这里只做浏览器做不到、必须落在 OS 层的五件事：
//!   1. 无边框 + 透明 + 置顶，并关掉 Windows 给无边框窗口画的方形投影；
//!   2. 命中区之外的鼠标穿透 —— **前端量、Rust 判**；
//!   3. 窗口尺寸跟随前端上报的内容外接盒（展开面板要长出来）；
//!   4. 拖动与运行时显隐；
//!   5. **与 Python 的事件通道** —— stdin 收事件、stdout 回确认。
//!
//! 前端不 import `@tauri-apps/api`，只用 `__TAURI_INTERNALS__.invoke` 调下面四个命令。
//! 因此本 crate **不需要 capabilities 文件**：Tauri 2 只对 `plugin:` 前缀的命令、
//! 或应用自带 ACL manifest 时才查权限（tauri-2.10.3 `webview/mod.rs:1802`）。
//! 不放 capabilities 反而是更紧的姿态 —— 所有 core 插件命令对前端一律不可达，
//! IPC 面就是下面这四个 `vox_*`。
//!
//! 事件通道走**父进程的管道，不是回环端口**。工具确认卡的答复决定 `shell.run`
//! 跑不跑；开一个本地端口等于让机器上任何进程都能替用户按「允许」。管道只有
//! spawn 我们的那个父进程够得着，而且不需要绑端口、发 token、写回环校验。
//!
//! Rust 侧**不认识事件类型**：整个信封原样投给前端的 `vox-bridge`。
//! 契约里加一种事件不该需要改 Rust —— 那种耦合的表现是「新事件在 UI 上静默消失」。


use std::io::{BufRead, Write};
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{
    LogicalPosition, LogicalSize, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};

const WAKE_LABEL: &str = "wake";

/// 折叠态兜底尺寸。前端第一帧上报后就不再用它。
const INIT_W: f64 = 210.0;
const INIT_H: f64 = 205.0;

/// 首次落位时在球下方预留的空间。
/// 不预留的话，每次展开面板都会撞到工作区下边界、被夹回来，球就往上跳一大截。
const PANEL_RESERVE: f64 = 260.0;

/// 命中轮询周期。
///
/// Tauri 2 的 `set_ignore_cursor_events` 是**整窗开关**，没有 Electron 的 `forward`
/// 选项（tauri#6164 仍开着，社区来源），所以「球能点、周围能穿透」只能靠 Rust 侧
/// 轮询光标自己判。16ms 会让事件循环每秒多跑 60 趟往返；30ms 在手感上分辨不出来。
const POLL_INTERVAL: Duration = Duration::from_millis(30);

/// 前端上报的命中区域。单位是**逻辑 CSS 像素**，原点在窗口内容区左上角。
///
/// 圆心半径来自 CSS 布局的实际测量而不是这里的硬编码：任何一次样式改动都会让
/// Rust 侧写死的坐标漂掉，而漂掉的表现是「球点不动」或「空白处点不下去」，两者都难查。
#[derive(Debug, Clone, Deserialize)]
struct HitRegion {
    width: f64,
    height: f64,
    circle: Option<Circle>,
    #[serde(default)]
    rects: Vec<Rect>,
}

#[derive(Debug, Clone, Copy, Deserialize)]
struct Circle {
    cx: f64,
    cy: f64,
    r: f64,
}

#[derive(Debug, Clone, Copy, Deserialize)]
struct Rect {
    x: f64,
    y: f64,
    w: f64,
    h: f64,
}

impl HitRegion {
    fn contains(&self, x: f64, y: f64) -> bool {
        if x < 0.0 || y < 0.0 || x > self.width || y > self.height {
            return false;
        }
        if let Some(c) = self.circle {
            let (dx, dy) = (x - c.cx, y - c.cy);
            if dx * dx + dy * dy <= c.r * c.r {
                return true;
            }
        }
        self.rects
            .iter()
            .any(|r| x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h)
    }
}

#[derive(Default)]
struct Layout(Mutex<Option<HitRegion>>);

/* ============ IPC 命令 ============ */

#[tauri::command]
fn vox_report_layout(region: HitRegion, window: WebviewWindow, layout: tauri::State<'_, Layout>) {
    // 先落尺寸再存命中区：反过来会有一帧拿新命中区去测旧窗口
    resize_to(&window, region.width, region.height);
    *layout.0.lock().unwrap_or_else(|e| e.into_inner()) = Some(region);
}

#[tauri::command]
fn vox_start_drag(window: WebviewWindow) {
    let _ = window.start_dragging();
}

#[tauri::command]
fn vox_set_visible(window: WebviewWindow, visible: bool) {
    let _ = if visible { window.show() } else { window.hide() };
}

/// 确认卡的答复回到 Python。`id` 是提问那条 `tool.confirm_required` 的信封 id。
///
/// 用信封 id 而不是新造一个关联字段：平台契约的 `additionalProperties: false`
/// 本来就不允许多一个键，而信封 id 已经唯一。
#[tauri::command]
fn vox_confirm_reply(id: String, approved: bool) {
    write_line(&format!(
        "{{\"kind\":\"confirm\",\"id\":{},\"approved\":{}}}",
        js_string_literal(&id),
        if approved { "true" } else { "false" }
    ));
}

/* ============ 事件通道 ============ */

/// 一行 JSON 到 stdout。写失败即父进程已走，没有可做的补救。
fn write_line(line: &str) {
    let out = std::io::stdout();
    let mut handle = out.lock();
    if writeln!(handle, "{}", line).is_ok() {
        let _ = handle.flush();
    }
}

/// 把任意文本包成一个 **JS 字符串字面量**。
///
/// 事件正文来自 Python 并被 `eval` 送进前端，所以这里是唯一能出注入的地方。
/// 做法是不拼 JS 结构，只产字面量：引号、反斜杠、控制字符全转义，前端再
/// `JSON.parse` 出结构。U+2028 / U+2029 一并转义 —— 它们在 JSON 里合法、
/// 在 ES2019 之前的 JS 字符串里是换行，转掉比赌运行时版本便宜。
fn js_string_literal(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len() + 2);
    out.push('"');
    for ch in raw.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{2028}' => out.push_str("\\u2028"),
            '\u{2029}' => out.push_str("\\u2029"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// 一行入站消息 -> 一段要 eval 的 JS，或 `None` 表示这行不该动前端。
///
/// 拆成纯函数是为了能测：注入形状的事件正文必须在这里就变成一个字面量，
/// 而不是等真机上出问题。Rust 不解析 `event` 里面有什么 —— 类型分派在前端。
fn script_for_line(line: &str) -> Option<String> {
    let text = line.trim();
    if text.is_empty() || !text.starts_with('{') {
        return None;
    }
    let literal = js_string_literal(text);
    Some(format!(
        "window.dispatchEvent(new CustomEvent('vox-bridge',{{detail:JSON.parse({})}}))",
        literal
    ))
}

/// 读 stdin 直到管道关闭。没有父进程时第一次读就 EOF，线程静静退出。
fn spawn_event_reader(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let stdin = std::io::stdin();
        for line in stdin.lock().lines() {
            let Ok(line) = line else { break };
            let Some(script) = script_for_line(&line) else {
                continue;
            };
            let Some(window) = app.get_webview_window(WAKE_LABEL) else {
                break; // 窗口没了 = 应用在退出
            };
            let _ = window.eval(script.as_str());
        }
    });
}

/* ============ 几何 ============ */

/// 尺寸跟随内容，位置只在越界时才动。
///
/// 球在布局最上方，所以**保持左上角不动 = 球不动**，面板向下长。只有下边越出
/// 工作区时才上移；`PANEL_RESERVE` 就是为了让这条路径在正常展开时根本走不到。
fn resize_to(window: &WebviewWindow, w: f64, h: f64) {
    let (w, h) = (w.clamp(120.0, 1600.0), h.clamp(120.0, 1600.0));
    if window.set_size(LogicalSize::new(w, h)).is_err() {
        return;
    }
    clamp_into_work_area(window, w, h);
}

fn clamp_into_work_area(window: &WebviewWindow, w: f64, h: f64) {
    let (Ok(scale), Ok(pos), Ok(Some(monitor))) = (
        window.scale_factor(),
        window.outer_position(),
        window.current_monitor(),
    ) else {
        return;
    };

    // 全部换算成逻辑像素再比。物理与逻辑混用是 DPI 缩放下最常见的错法。
    let area = monitor.work_area();
    let ax = area.position.x as f64 / scale;
    let ay = area.position.y as f64 / scale;
    let aw = area.size.width as f64 / scale;
    let ah = area.size.height as f64 / scale;
    let (x0, y0) = (pos.x as f64 / scale, pos.y as f64 / scale);

    // 先按右/下推回来，再按左/上兜底：窗口比工作区还大时以左上角对齐
    let x = x0.min(ax + aw - w).max(ax);
    let y = y0.min(ay + ah - h).max(ay);

    if (x - x0).abs() > 0.5 || (y - y0).abs() > 0.5 {
        let _ = window.set_position(LogicalPosition::new(x, y));
    }
}

/// 首次落位：工作区底部居中，球下方留出 `PANEL_RESERVE`。
fn place_initially(window: &WebviewWindow) {
    let (Ok(scale), Ok(Some(monitor))) = (window.scale_factor(), window.current_monitor()) else {
        return;
    };
    let area = monitor.work_area();
    let ax = area.position.x as f64 / scale;
    let ay = area.position.y as f64 / scale;
    let aw = area.size.width as f64 / scale;
    let ah = area.size.height as f64 / scale;

    let x = ax + (aw - INIT_W) / 2.0;
    let y = (ay + ah - INIT_H - PANEL_RESERVE).max(ay);
    let _ = window.set_position(LogicalPosition::new(x, y));
}

/* ============ 命中轮询 ============ */

/// 读不到光标 / 读不到窗口位置 / 前端还没上报 —— 三条失败路径**一律倒向「窗口吃鼠标」**。
///
/// 反方向（全窗穿透）看似更礼貌，但那会让工具确认卡变成一张点不动的图，
/// 而一个点不下去的确认卡等价于没有确认。窗口最多两百来像素见方，挡住桌面是可恢复的；
/// 确认按钮点不动不是。
fn cursor_over_hit_region(app: &tauri::AppHandle, window: &WebviewWindow) -> bool {
    let (Ok(cursor), Ok(origin), Ok(scale)) = (
        window.cursor_position(),
        window.inner_position(),
        window.scale_factor(),
    ) else {
        return true;
    };

    let x = (cursor.x - origin.x as f64) / scale;
    let y = (cursor.y - origin.y as f64) / scale;

    let state = app.state::<Layout>();
    let guard = state.0.lock().unwrap_or_else(|e| e.into_inner());
    match guard.as_ref() {
        None => true,
        Some(region) => region.contains(x, y),
    }
}

fn spawn_hit_test(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        // None = 还没下过开关，第一次必落一次实际调用
        let mut ignoring: Option<bool> = None;
        loop {
            std::thread::sleep(POLL_INTERVAL);
            let Some(window) = app.get_webview_window(WAKE_LABEL) else {
                break; // 窗口没了 = 应用在退出
            };
            if !window.is_visible().unwrap_or(false) {
                // 隐藏期间不碰开关；重新显示时强制重下一次，避免沿用过期状态
                ignoring = None;
                continue;
            }
            let want_ignore = !cursor_over_hit_region(&app, &window);
            if ignoring != Some(want_ignore) && window.set_ignore_cursor_events(want_ignore).is_ok()
            {
                ignoring = Some(want_ignore);
            }
        }
    });
}

/* ============ 系统托盘 ============ */

/// 球是无边框 + skip_taskbar + 置顶，桌面上没有别的入口能关它。托盘是用户唯一的
/// 「显示/隐藏/退出」路径：没有它，退出这个进程只能去任务管理器。
///
/// 托盘菜单是 Rust 侧直接建的，和四个 `vox_*` 命令无关，因此**不扩大 IPC 面**：
/// 前端仍然够不到托盘。
fn build_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let toggle = MenuItem::with_id(app, "toggle", "显示 / 隐藏", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&toggle, &quit])?;
    TrayIconBuilder::with_id("wake-tray")
        .icon(app.default_window_icon().cloned().expect("app icon must be bundled"))
        .tooltip("Vox")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "toggle" => {
                if let Some(window) = app.get_webview_window(WAKE_LABEL) {
                    if window.is_visible().unwrap_or(false) {
                        let _ = window.hide();
                    } else {
                        let _ = window.show();
                    }
                }
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .manage(Layout::default())
        .invoke_handler(tauri::generate_handler![
            vox_report_layout,
            vox_start_drag,
            vox_set_visible,
            vox_confirm_reply
        ])
        .setup(|app| {
            let mut builder =
                WebviewWindowBuilder::new(app, WAKE_LABEL, WebviewUrl::App("index.html".into()))
                    .title("Vox")
                    .inner_size(INIT_W, INIT_H)
                    .resizable(false)
                    .decorations(false)
                    .transparent(true)
                    // 无边框 + 透明还留着投影的话，桌面上会出现一块跟着球走的方形灰影
                    .shadow(false)
                    .always_on_top(true)
                    .skip_taskbar(true)
                    .focused(false)
                    // 右键浏览器菜单在前端 main.ts 里 preventDefault，
                    // tauri 2.10 的 builder 没有对应开关，别在这里找。
                    .visible(std::env::var_os("VOX_WAKE_VISIBLE").is_some());
            // 远程会话（RDP）里 WebView2 的 GPU 合成会让窗口透明失效，
            // 表现为球周围一块实底方框。只在真远程时才退回软件合成，
            // 本地会话保持硬件加速。覆盖默认参数时必须带上这三项禁用，
            // 否则 PDF/OOUI/SmartScreen 会重新出现。
            if std::env::var("SESSIONNAME")
                .map(|session| session.starts_with("RDP-"))
                .unwrap_or(false)
            {
                builder = builder.additional_browser_args(
                    "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --disable-gpu",
                );
            }
            let wake = builder.build()?;
            wake.set_ignore_cursor_events(false)?;
            place_initially(&wake);
            spawn_hit_test(app.handle().clone());
            spawn_event_reader(app.handle().clone());
            build_tray(app.handle())?;
            // 父进程凭这一行知道管道通了，而不是靠猜一个启动延时
            write_line("{\"kind\":\"ready\"}");
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to run Vox voice wake window");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn region() -> HitRegion {
        HitRegion {
            width: 200.0,
            height: 400.0,
            circle: Some(Circle {
                cx: 100.0,
                cy: 90.0,
                r: 82.0,
            }),
            rects: vec![Rect {
                x: 16.0,
                y: 200.0,
                w: 168.0,
                h: 120.0,
            }],
        }
    }

    #[test]
    fn circle_center_hits() {
        assert!(region().contains(100.0, 90.0));
    }

    #[test]
    fn just_inside_and_outside_the_radius() {
        // 半径 82，正右方 81.9 命中、82.1 不命中
        assert!(region().contains(181.9, 90.0));
        assert!(!region().contains(182.1, 90.0));
    }

    #[test]
    fn corner_gap_between_circle_and_rect_passes_through() {
        // 球的外接方内、圆外、也不在面板矩形里 —— 这块必须穿透，
        // 否则球周围会有一圈看不见却挡鼠标的死区
        assert!(!region().contains(25.0, 25.0));
    }

    #[test]
    fn panel_rect_hits_including_edges() {
        assert!(region().contains(16.0, 200.0));
        assert!(region().contains(184.0, 320.0));
        assert!(!region().contains(15.9, 200.0));
        assert!(!region().contains(100.0, 321.0));
    }

    #[test]
    fn outside_the_window_never_hits() {
        assert!(!region().contains(-1.0, 90.0));
        assert!(!region().contains(100.0, -1.0));
        assert!(!region().contains(201.0, 90.0));
        assert!(!region().contains(100.0, 401.0));
    }

    #[test]
    fn region_without_circle_still_hits_rects() {
        let mut r = region();
        r.circle = None;
        assert!(!r.contains(100.0, 90.0));
        assert!(r.contains(100.0, 260.0));
    }

    #[test]
    fn deserializes_the_payload_the_frontend_actually_sends() {
        let json = r#"{"width":372,"height":417,"circle":{"cx":105,"cy":88,"r":82},
                       "rects":[{"x":16,"y":200,"w":340,"h":120}]}"#;
        let r: HitRegion = serde_json::from_str(json).expect("frontend payload must parse");
        assert!(r.contains(105.0, 88.0));
        assert!(r.contains(200.0, 250.0));
        assert!(!r.contains(360.0, 60.0));
    }

    #[test]
    fn missing_rects_defaults_to_empty() {
        let r: HitRegion =
            serde_json::from_str(r#"{"width":180,"height":198,"circle":null}"#).unwrap();
        assert!(r.rects.is_empty());
        assert!(!r.contains(90.0, 90.0));
    }

    /* ---- 事件通道 ---- */

    #[test]
    fn a_quote_cannot_close_the_literal() {
        // 事件正文来自 Python 并被 eval，所以「一个引号能不能提前收尾」
        // 就是这条通道有没有注入的全部问题
        let evil = r#"{"type":"x","payload":{"command":"\" ; alert(1) ; \""}}"#;
        let script = script_for_line(evil).expect("a JSON line must produce a script");
        // 收尾引号只能有一个，就是最后那个
        let body = &script[script.find('(').unwrap()..];
        assert_eq!(body.matches("\");").count() + body.matches("\")").count(), 1);
        assert!(!script.contains("; alert(1) ;\""));
    }

    #[test]
    fn backslashes_and_newlines_are_escaped_not_passed_through() {
        let literal = js_string_literal("a\\b\nc\rd\te");
        assert_eq!(literal, "\"a\\\\b\\nc\\rd\\te\"");
        // 原始换行留在 JS 字面量里就是语法错误，整段 eval 静默失败
        assert!(!literal.contains('\n'));
        assert!(!literal.contains('\r'));
    }

    #[test]
    fn line_separators_that_json_allows_but_js_breaks_on() {
        let literal = js_string_literal("a\u{2028}b\u{2029}c");
        assert_eq!(literal, "\"a\\u2028b\\u2029c\"");
    }

    #[test]
    fn control_characters_become_escapes() {
        assert_eq!(js_string_literal("a\u{0}b\u{1f}"), "\"a\\u0000b\\u001f\"");
    }

    #[test]
    fn chinese_text_survives_unescaped() {
        // 转义器不该顺手把中文变成 \uXXXX：确认卡要显示的是命令原文
        assert_eq!(js_string_literal("读一下 README"), "\"读一下 README\"");
    }

    #[test]
    fn non_json_lines_move_nothing() {
        assert!(script_for_line("").is_none());
        assert!(script_for_line("   ").is_none());
        assert!(script_for_line("hello").is_none());
        // 裸数组也不是信封；不判类型，但形状要求是对象
        assert!(script_for_line("[1,2]").is_none());
    }

    #[test]
    fn an_envelope_reaches_one_frontend_event() {
        let script = script_for_line(r#"{"kind":"event","event":{"type":"task.done"}}"#).unwrap();
        assert!(script.contains("vox-bridge"));
        assert!(script.contains("JSON.parse"));
        // Rust 不认识事件类型：类型名只作为数据出现，不参与分派
        assert!(!script.contains("if"));
    }
}
