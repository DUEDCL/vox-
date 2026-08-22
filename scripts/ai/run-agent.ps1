param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('claude', 'codex')]
    [string]$Agent,
    [Parameter(Mandatory = $true)]
    [string]$PromptFile,
    [Parameter(Mandatory = $true)]
    [string]$OutputFile,
    [switch]$ReadOnly,
    [switch]$Review,
    [switch]$AllowWrite
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path

function Assert-InRoot([string]$Candidate, [string]$Label) {
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $candidateFull = [IO.Path]::GetFullPath($Candidate)
    if (-not $candidateFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label 必须位于仓库根目录内：$Candidate"
    }
    return $candidateFull
}

function Resolve-RepoPath([string]$Path) {
    $candidate = if ([IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $Root $Path }
    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    return Assert-InRoot $resolved '输入文件'
}

$promptPath = Resolve-RepoPath $PromptFile
$outputPath = if ([IO.Path]::IsPathRooted($OutputFile)) { $OutputFile } else { Join-Path $Root $OutputFile }
$outputPath = Assert-InRoot $outputPath '输出文件'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputPath) | Out-Null
$prompt = Get-Content -LiteralPath $promptPath -Raw

if ($Review) {
    $diff = (git -C $Root diff --no-ext-diff --no-color | Out-String)
    if ($diff.Length -gt 120000) {
        $diff = $diff.Substring(0, 120000) + "`n[diff truncated by review harness]"
    }
    $prompt += [Environment]::NewLine
    $prompt += '以下是宿主脚本采集的当前工作区 diff；只读审查它，不要编辑文件：'
    $prompt += [Environment]::NewLine + '```diff' + [Environment]::NewLine
    $prompt += $diff
    $prompt += [Environment]::NewLine + '```'

    # git diff 默认不包含未跟踪文件。把安全过滤后的清单交给 Reviewer，

    # 由它使用只读 Read 工具检查新增文件，避免把整个脏工作区内容复制进 prompt。
    $untracked = @(git -C $Root ls-files --others --exclude-standard | Where-Object {
        $_ -notmatch '(^|/)(\.env|enrollment|models)(/|$)' -and
        $_ -notmatch '\.(voiceprint|wav|mp3|flac)$'
    })
    if ($untracked.Count -gt 0) {
        $prompt += [Environment]::NewLine
        $prompt += '以下是当前未跟踪文件清单；只检查与任务允许范围相关的文件，不要读取敏感目录：'
        $prompt += [Environment]::NewLine + ($untracked -join [Environment]::NewLine)
    }
}

if (-not $ReadOnly -and -not $AllowWrite) {
    throw '写入模式必须显式传入 -AllowWrite；审查/规划请使用 -ReadOnly。'
}

$gitStatus = @(git -C $Root status --short)
if ($gitStatus.Count -gt 0) {
    Write-Warning '当前工作区存在未提交修改；脚本不会 stash、reset、clean 或覆盖它们。建议在独立 worktree 中运行。'
}

if ($Agent -eq 'claude') {
    $tools = if ($ReadOnly -or $Review) { @('--tools', 'Read,Glob,Grep') } else { @() }
    $stdoutPath = Join-Path $env:TEMP ('claude-' + [guid]::NewGuid().ToString() + '.out.txt')
    $stderrPath = Join-Path $env:TEMP ('claude-' + [guid]::NewGuid().ToString() + '.err.txt')
    try {
        & claude -p $prompt --output-format text --no-session-persistence @tools 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE
        if (Test-Path $stdoutPath) {
            Copy-Item -LiteralPath $stdoutPath -Destination $outputPath -Force
        } else {
            Set-Content -LiteralPath $outputPath -Value '' -Encoding utf8
        }
        if (Test-Path $stderrPath) {
            $stderr = Get-Content -LiteralPath $stderrPath -Raw
            if ($stderr) { Write-Warning ('claude stderr: ' + $stderr.Trim()) }
        }
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
} else {
    $sandbox = if ($ReadOnly -or $Review) { 'read-only' } else { 'workspace-write' }
    $eventsPath = Join-Path $env:TEMP ('codex-' + [guid]::NewGuid().ToString() + '.jsonl')
    & codex exec --json -C $Root -s $sandbox -o $outputPath $prompt 1> $eventsPath 2>&1
    $exitCode = $LASTEXITCODE
    if (Test-Path $eventsPath) { Remove-Item -LiteralPath $eventsPath -Force }
}

if ($exitCode -ne 0) { throw ('{0} 执行失败，退出码：{1}' -f $Agent, $exitCode) }
Write-Output ('{0} completed: {1}' -f $Agent, $outputPath)


