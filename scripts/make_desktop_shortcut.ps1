# 建/修桌面快捷方式。单独成文件而不是 -Command 一行，是为了让中文说明不经过控制台
# 代码页 —— 走 -Command 时它会按 cp936 编码，写进 .lnk 的就是乱码。
$ErrorActionPreference = 'Stop'

$repo   = 'D:\program\vioce-wake'
$target = Join-Path $repo 'scripts\vox.cmd'
$icon   = Join-Path $repo 'desktop\src-tauri\icons\icon.ico'
$link   = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Vox.lnk'

if (-not (Test-Path $target)) { throw "找不到启动脚本：$target" }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath       = $target
$sc.WorkingDirectory = $repo
$sc.Description      = 'Vox — 控制台 + 麦克风 + 唤醒球'
if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
$sc.Save()

$check = $shell.CreateShortcut($link)
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Write-Output "已写入: $link"
Write-Output "目标  : $($check.TargetPath)"
Write-Output "说明  : $($check.Description)"
