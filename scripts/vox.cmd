@echo off
setlocal

rem ============================================================================
rem  Vox 启动脚本 —— 控制台 + 麦克风 + 唤醒球
rem
rem  桌面上那个快捷方式指向这个文件，所以要改启动方式改这里，不用重做快捷方式。
rem
rem  几个决定，每个都有理由：
rem
rem  * chcp 65001：Python 写出来的是 UTF-8 字节，而 Windows 控制台默认按本机代码页
rem    （这台机器是 cp936）解释它们 —— 于是日志里的中文是乱码。这不是程序的缺陷，
rem    是终端的显示设置，所以修在这一层而不是去改代码。
rem  * pushd "%~dp0.."：切到仓库根。用脚本自己的位置算，不用当前目录 —— 双击时
rem    的当前目录是桌面，而配置、模型、.env 都按仓库根解析。
rem  * 失败时 pause：双击启动的窗口在进程退出时会立刻关闭，报错一个字都看不到。
rem  * 用 .venv 里那个 python.exe，不用系统 Python：系统环境没装 sherpa-onnx 和
rem    soundfile，而那两个是唤醒和识别的全部依赖。
rem ============================================================================

chcp 65001 >nul
pushd "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [启动失败] 找不到 .venv\Scripts\python.exe
    echo.
    echo 隔离环境还没建。在仓库根跑一次：
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements-voice.txt
    echo.
    popd
    pause
    exit /b 1
)

echo Vox 正在启动：控制台 + 麦克风 + 唤醒球
echo 仓库：%CD%
echo.
echo 浏览器会自动打开控制台。要停止：在这个窗口按 Ctrl+C，或者用托盘菜单的「退出」。
echo.

.venv\Scripts\python.exe scripts\run_console.py --voice
set EXITCODE=%ERRORLEVEL%

popd

rem 0 = 正常退出（Ctrl+C 也算），其余都要把窗口留住让人读错误。
if not "%EXITCODE%"=="0" (
    echo.
    echo [Vox 退出，代码 %EXITCODE%]
    echo.
    echo 常见原因：
    echo   * 端口 8899 被占用 —— 上一个 Vox 还在跑，先关掉它
    echo   * 麦克风被占用或被隐私设置拒绝 —— 控制台照常能开，页面上会说
    echo   * .env 里缺密钥 —— 页面「密钥」那一栏可以补
    echo.
    pause
)

endlocal
