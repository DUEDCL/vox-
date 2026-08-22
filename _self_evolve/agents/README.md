# Agent 角色说明

- Architect：读取上下文、拆分任务、识别风险。
- Coder：在允许范围内实现和补测试。
- Reviewer：独立阅读 diff、运行验证、出具 PASS/REQUEST_CHANGES/BLOCKED。
- Tester：设计最小回归和边界验证，标注证据等级。
- Memory Keeper：将可复用经验写入 `_self_evolve/memory/`，不得写入敏感数据。
