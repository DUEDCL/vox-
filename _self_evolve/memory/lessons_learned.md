# lessons_learned

2026-08-23｜全量 pytest 验证｜默认临时目录清理在测试通过后因 Windows `WinError 5` 退出非零，容易被误判为业务测试失败｜主机默认 pytest 临时根目录存在权限/残留 symlink 清理问题，与用例断言无关｜使用仓库内 `--basetemp .pytest-run-*` 运行并在结束后删除临时目录；记录测试输出与退出码分开｜pytest,Windows,验证
2026-08-23｜工具/Agent 事件出口｜内部错误消息直接进入公开事件会把路径、stderr 或回复正文扇出到所有日志与传输｜可替换工具和 Agent 的 error 字段不是安全协议字段｜在事件边界使用固定原因白名单，内部 ToolResult/AgentChunk 仍保留本地诊断；事件 sink 作为 best-effort 旁路并计数｜隐私,事件,fail-closed

2026-08-24｜派发/熔断/记忆事件出口｜日志或桌面传输 sink 属于旁路，异常不应改变健康状态、派发结果或本地记忆读写｜producer 的事件已校验但 sink 不是业务依赖｜各 producer 捕获 sink Exception，仅递增 sink_failures，不保留异常正文，并用回归测试证明原结果不变｜可靠性,事件,隔离
