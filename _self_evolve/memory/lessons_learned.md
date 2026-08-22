# lessons_learned

2026-08-23｜全量 pytest 验证｜默认临时目录清理在测试通过后因 Windows `WinError 5` 退出非零，容易被误判为业务测试失败｜主机默认 pytest 临时根目录存在权限/残留 symlink 清理问题，与用例断言无关｜使用仓库内 `--basetemp .pytest-run-*` 运行并在结束后删除临时目录；记录测试输出与退出码分开｜pytest,Windows,验证
