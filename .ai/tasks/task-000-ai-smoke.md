# AI Smoke Task 000

状态：TODO

## 目标
验证 Claude Code + Codex 本地接力脚本、交接产物格式和合约校验流程，不修改业务代码。

## 允许修改范围
- `.ai/runs/**`（临时、不提交）
- `.ai/handoffs/**`
- `.ai/reviews/**`
## 禁止修改范围
- 除上述范围外的所有文件，包括业务代码、测试、配置、依赖和 Git 历史。
- 不读取或输出 `.env`、`enrollment/`、声纹文件、模型权重、原始音频或任何凭据。

## 验收标准
1. Claude 输出一份只读架构/流程计划，不修改文件。
2. Codex 仅在允许范围内生成交接记录；如果当前脚本阶段无法安全写入，应明确报告 BLOCKED，而不是修改其他文件。
3. Review 产物包含 `PASS`、`REQUEST_CHANGES` 或 `BLOCKED` 之一，并包含验证章节。
4. 所有 `.ai` 产物通过统一合约校验。
5. 不将本次 smoke 流程宣称为 `REAL-AGENT` 业务验证；它只验证协作编排。

## 验证命令
```powershell
.\scripts\ai\validate-contract.ps1
```

## 证据等级
- 协作脚本 dry-run：AUTO
- 若实际调用 CLI：REAL-AGENT 仅表示 Agent CLI 被真实调用，不代表语音业务链路通过。

## 人工闸门
不得自动提交、推送、合并或覆盖当前工作区已有改动。

