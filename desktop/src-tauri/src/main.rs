#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{WebviewUrl, WebviewWindowBuilder};

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let wake = WebviewWindowBuilder::new(app, "wake", WebviewUrl::App("index.html".into()))
                .title("EvoX Voice Wake")
                .inner_size(210.0, 205.0)
                .resizable(false)
                .decorations(false)
                .transparent(true)
                .always_on_top(true)
                .skip_taskbar(true)
                .focused(false)
                .visible(std::env::var_os("EVOX_WAKE_VISIBLE").is_some())
                .build()?;
            wake.set_ignore_cursor_events(false)?;
            if let Some(monitor) = wake.current_monitor()? {
                let size = monitor.size();
                let scale = monitor.scale_factor();
                let x = (size.width as f64 / scale - 210.0) / 2.0;
                let y = size.height as f64 / scale - 250.0;
                wake.set_position(tauri::Position::Logical(tauri::LogicalPosition::new(x, y)))?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to run EvoX voice wake window");
}
