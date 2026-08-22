param(
    [Parameter(Mandatory = $true)]
    [string]$TaskFile,
    [int]$MaxReviewLoops = 2,
    [switch]$AllowWrite,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$task = (Resolve-Path (Join-Path $Root $TaskFile)).Path
$taskName = [IO.Path]::GetFileNameWithoutExtension($task)
$planOutput = Join-Path $Root ".ai/runs/$taskName-plan.md"
$handoffOutput = Join-Path $Root ".ai/handoffs/$taskName-handoff.md"
$reviewOutput = Join-Path $Root ".ai/reviews/$taskName-review.md"

if (-not $DryRun -and -not $AllowWrite) {
    throw '完整接力流程会让 Codex 修改工作区。请显式传入 -AllowWrite；仅规划/审查请分别调用 run-agent.ps1 -ReadOnly。'
}

$basePrompt = "你正在参与 Vox 的 AI 接力开发。`n请先读取：`n- AGENTS.md`n- CLAUDE.md`n- .ai/CONTRACT.md`n- $TaskFile`n`n严格遵守任务允许/禁止修改范围、项目本地优先和验证等级约束。不要读取或输出 .env、凭据、enrollment、声纹、模型权重或原始音频。"

function Invoke-Agent([string]$agent, [string]$prompt, [string]$output, [switch]$readOnly, [switch]$review) {
    $promptFile = Join-Path $env:TEMP "$taskName-$agent-prompt.md"
    ($basePrompt + "`n`n" + $prompt) | Set-Content -Encoding utf8 $promptFile
    $args = @('-Agent', $agent, '-PromptFile', $promptFile, '-OutputFile', $output)
    if ($readOnly) { $args += '-ReadOnly' } else { $args += '-AllowWrite' }
    if ($review) { $args += '-Review' }
    if ($DryRun) { Write-Output "DRY RUN: scripts/ai/run-agent.ps1 $($args -join ' ')"; return }
    & (Join-Path $PSScriptRoot 'run-agent.ps1') @args
    if ($LASTEXITCODE -ne 0) { throw "$agent 阶段失败" }
}

function Get-ReviewConclusion([string]$Text) {
    # 只解析“## 结论”章节，避免正文中提到 REQUEST_CHANGES/BLOCKED
    # 导致一个实际 PASS 被误判为失败；兼容模板中的空行和 HTML 注释。
    $sectionMatch = [regex]::Match(
        $Text,
        '(?ims)^##\s*结论\b(?<section>.*?)(?=^##\s+|\z)'
    )
    if ($sectionMatch.Success) {
        $conclusion = [regex]::Match(
            $sectionMatch.Groups['section'].Value,
            '(?im)\b(PASS|REQUEST_CHANGES|BLOCKED)\b'
        )
        if ($conclusion.Success) { return $conclusion.Groups[1].Value.ToUpperInvariant() }
    }

    $fallback = [regex]::Match(
        $Text,
        '(?im)^\s*(PASS|REQUEST_CHANGES|BLOCKED)\s*$'
    )
    if ($fallback.Success) { return $fallback.Groups[1].Value.ToUpperInvariant() }
    return ''
}

if ($DryRun) {
    Write-Output 'DRY RUN：仅打印接力计划，不调用任何 Agent，不写入工作区。'
    Write-Output "Task: $([IO.Path]::GetRelativePath($Root, $task))"
    Write-Output "Max review loops: $MaxReviewLoops"
    Write-Output 'Planned stages: Claude plan -> Codex implement -> Claude review -> Codex fix (up to limit) -> contract validation'
    & (Join-Path $PSScriptRoot 'validate-contract.ps1') -TaskPath ([IO.Path]::GetRelativePath($Root, $task))
    return
}

Write-Output '阶段 1：Claude Code 规划任务（只读）'
Invoke-Agent 'claude' '作为 Architect，只读分析当前仓库和任务文件。不要修改任何文件。输出 Markdown 计划，必须包含：## 实现方案、## 验收标准、## 风险与人工闸门、## 建议修改文件。' $planOutput -readOnly

Write-Output '阶段 2：Codex 实现任务'
Invoke-Agent 'codex' "作为 Implementer，按任务文件实现代码和测试。先读取 $([IO.Path]::GetRelativePath($Root, $planOutput))，将 Claude 的计划作为参考。只修改允许范围。最终输出 Markdown 交接，必须包含：## 状态、## 修改摘要、## 修改文件、## 验证结果、## 已知风险、## 需要人类确认。" $handoffOutput

for ($round = 1; $round -le $MaxReviewLoops; $round++) {
    Write-Output "阶段 3：Claude Code 独立审查（第 $round 轮，只读）"
    Invoke-Agent 'claude' '作为独立 Reviewer，审查宿主脚本附带的当前 diff 和任务验收标准。不要编辑代码。最终输出 Markdown，必须包含：## 结论（只能是 PASS、REQUEST_CHANGES 或 BLOCKED）、## Findings、## 验收标准核对、## 已执行验证、## 仍需人工确认。每个问题给出 P0/P1/P2/P3、文件/位置、证据和最小修复建议。' $reviewOutput -readOnly -review
    $reviewText = if (Test-Path $reviewOutput) { Get-Content $reviewOutput -Raw } else { '' }
    $conclusion = Get-ReviewConclusion $reviewText
    Write-Output "审查结论：$conclusion"
    if ($conclusion -eq 'PASS') { break }
    if ($round -eq $MaxReviewLoops) { throw "达到最大审查轮次 $MaxReviewLoops，任务仍未 PASS；请人工处理。" }
    Write-Output "阶段 4：Codex 根据审查意见修复（第 $round 轮）"
    Invoke-Agent 'codex' "读取 $([IO.Path]::GetRelativePath($Root, $reviewOutput))。只修复其中明确的问题，不进行无关重构。重新输出完整 Markdown 交接，保留既有章节并逐条处理审查意见，重新运行任务文件中的验证命令。" $handoffOutput
}

Write-Output '最终阶段：运行协作产物校验'
& (Join-Path $PSScriptRoot 'validate-contract.ps1')
Write-Output 'AI 接力流程完成；请人工检查 diff、验证输出和高风险闸门后再合并。'

