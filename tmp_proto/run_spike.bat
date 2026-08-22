@echo off
cd /d D:\program\vioce-wake\desktop
set VOX_WAKE_VISIBLE=1
npm run tauri dev > D:\program\vioce-wake\tmp_proto\tauri_dev.log 2>&1
