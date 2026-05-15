$ErrorActionPreference = "Continue"
$WatchDir = [Environment]::GetFolderPath("Desktop") + "\slack-bot"
$ProcessedDir = Join-Path $WatchDir ".processed"
$LogDir = "$env:USERPROFILE\.claude\scripts\folder-watch\logs"
$env:PATH = "$env:APPDATA\Python\Python314\Scripts;$env:APPDATA\npm;$env:PATH"
$env:PYTHONUTF8 = "1"

# .env 로드
Get-Content "$env:USERPROFILE\.claude\secrets\slack-jipsa.env" -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+?)\s*=\s*(.+?)\s*$') {
        Set-Item -Path "Env:$($matches[1])" -Value $matches[2]
    }
}

function Write-Log($msg) {
    $log = Join-Path $LogDir "$(Get-Date -Format 'yyyy-MM-dd').log"
    "[$(Get-Date -Format 'HH:mm:ss')] $msg" | Out-File -Append -Encoding utf8 $log
}

function Post-Slack($text) {
    $body = @{ channel = $env:SLACK_CHANNEL; text = $text } | ConvertTo-Json -Compress
    try {
        Invoke-RestMethod -Uri "https://slack.com/api/chat.postMessage" `
            -Method Post `
            -Headers @{ Authorization = "Bearer $env:SLACK_BOT_TOKEN" } `
            -ContentType "application/json; charset=utf-8" `
            -Body $body | Out-Null
    } catch {
        Write-Log "Slack post error: $_"
    }
}

function Process-File($filePath) {
    $basename = Split-Path $filePath -Leaf
    Write-Log "Processing: $basename"
    Post-Slack ":hourglass_flowing_sand: `$basename` 처리 시작"

    $prompt = "이 파일을 분석해서 핵심을 3~5문장으로 요약한 후, 같은 폴더에 동일한 이름의 .summary.md 파일로 저장해줘. 파일 경로: $filePath"
    $result = $prompt | & claude --print --dangerously-skip-permissions --add-dir $WatchDir 2>&1 | Out-String

    if ($result -and $result.Trim()) {
        $short = if ($result.Length -gt 3500) { $result.Substring(0, 3500) + "..." } else { $result.Trim() }
        Post-Slack ":white_check_mark: `$basename` 처리 완료`n``````n$short`n``````"
    } else {
        Post-Slack ":white_check_mark: `$basename` 처리 완료 (결과는 폴더 확인)"
    }

    $newName = "$(Get-Date -Format 'yyyyMMdd-HHmmss')-$basename"
    Move-Item $filePath (Join-Path $ProcessedDir $newName) -Force
    Write-Log "Done: $basename -> .processed\$newName"
}

Write-Log "FolderWatch 시작 (슬랙 알림 ON). 감시 중: $WatchDir"

# 시작 시 이미 있는 파일 처리
Get-ChildItem -Path $WatchDir -File | Where-Object { -not $_.Name.StartsWith('.') } | ForEach-Object {
    Process-File $_.FullName
}

# 폴링 방식 (5초마다 새 파일 확인)
while ($true) {
    Start-Sleep -Seconds 5
    Get-ChildItem -Path $WatchDir -File | Where-Object { -not $_.Name.StartsWith('.') } | ForEach-Object {
        Process-File $_.FullName
    }
}
