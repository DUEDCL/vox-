param(
    [string]$TaskPath,
    [string]$ReviewPath,
    [string]$HandoffPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path

function Fail([string]$Message) {
    throw "AI contract validation failed: $Message"
}

function Get-MarkdownFiles([string]$RelativeDir) {
    $dir = Join-Path $Root $RelativeDir
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return @() }
    return @(Get-ChildItem -LiteralPath $dir -Filter '*.md' -File | Where-Object { $_.Name -notmatch '^README|^template' })
}

function Resolve-Artifact([string]$Path) {
    $candidate = if ([IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $Root $Path }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { Fail "找不到产物：$Path" }
    return Get-Item -LiteralPath $candidate
}

function Assert-Contains([string]$Text, [string]$Pattern, [string]$Path) {
    if ($Text -notmatch $Pattern) { Fail "$Path 缺少必需字段：$Pattern" }
}

$files = @()
$files += Get-MarkdownFiles '.ai/tasks'
$files += Get-MarkdownFiles '.ai/reviews'
$files += Get-MarkdownFiles '.ai/handoffs'
if ($TaskPath) { $files += Resolve-Artifact $TaskPath }
if ($ReviewPath) { $files += Resolve-Artifact $ReviewPath }
if ($HandoffPath) { $files += Resolve-Artifact $HandoffPath }
$files = @($files | Sort-Object FullName -Unique)

$secretPattern = '(-----BEGIN [A-Z ]+PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9_\-]{20,}|github_pat_[A-Za-z0-9_\-]{20,}|sk-(?:ant|proj)-[A-Za-z0-9_\-]{20,})'
foreach ($file in $files) {
    $text = Get-Content -LiteralPath $file.FullName -Raw
    if ($text -match $secretPattern) { Fail "$($file.FullName) 看起来包含凭据形状，已拒绝提交" }
}

foreach ($file in $files) {
    $text = Get-Content -LiteralPath $file.FullName -Raw
    $relative = [IO.Path]::GetRelativePath($Root, $file.FullName).Replace('\', '/')
    if ($relative -like '.ai/tasks/*') {
        Assert-Contains $text '^# ' $relative
        Assert-Contains $text '## 目标' $relative
        Assert-Contains $text '## 验收标准' $relative
        Assert-Contains $text '## 验证命令' $relative
        Assert-Contains $text '状态：\s*(TODO|IN_PROGRESS|REVIEW|CHANGES_REQUESTED|VERIFIED|MERGED|BLOCKED)' $relative
    } elseif ($relative -like '.ai/reviews/*') {
        Assert-Contains $text '^# ' $relative
        Assert-Contains $text '## 结论' $relative
        Assert-Contains $text '(PASS|REQUEST_CHANGES|BLOCKED)' $relative
        Assert-Contains $text '## 已执行验证' $relative
    } elseif ($relative -like '.ai/handoffs/*') {
        Assert-Contains $text '^# ' $relative
        Assert-Contains $text '## 修改摘要' $relative
        Assert-Contains $text '## 验证结果' $relative
        Assert-Contains $text '## 已知风险' $relative
    }
}

Write-Output "AI contract OK: $($files.Count) artifact(s) checked."
