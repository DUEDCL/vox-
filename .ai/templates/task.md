# AI Task：任务名称

## 元数据

- Issue：
- 状态：TODO
- 实现者：Codex
- 架构师 / 审查者：Claude Code
- 分支：
- 创建日期：

## 目标

<!-- 用一两句话描述用户可见目标。 -->

## 背景与现状

<!-- 指向现有模块、契约、ADR 或问题。不要复制敏感数据。 -->

## 允许修改

- 

## 禁止修改

- 数据库结构（除非明确批准）
- 核心依赖版本（除非明确批准）
- `contracts/voice-events.schema.json`
- `enrollment/`、`.env`、模型权重和凭据

## 实现方案

<!-- Claude Code 填写；如果还没有方案，先保持 TODO。 -->

## 验收标准

- [ ] 
- [ ] 
- [ ] 边界和异常行为已覆盖
- [ ] 未改变未授权的公开契约

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

## 风险与回滚

- 风险：
- 回滚方式：

## 交接要求

实现完成后写入 `.ai/handoffs/task-xxx-handoff.md`，必须包含修改文件、验证命令、真实结果和未解决风险。
