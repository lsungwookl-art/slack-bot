# Gemini Slack Bot Runner for Windows
$env:PYTHONIOENCODING = "utf-8"
$SECRETS = "$env:USERPROFILE\.claude\secrets\slack-jipsa.env"

if (Test-Path $SECRETS) {
    Get-Content $SECRETS | Where-Object { $_ -match '=' -and -not $_.StartsWith('#') } | ForEach-Object {
        $parts = $_.Split('=', 2)
        $key = $parts[0].Trim()
        $val = $parts[1].Trim()
        [System.Environment]::SetEnvironmentVariable($key, $val, "Process")
    }
} else {
    Write-Error "설정 파일($SECRETS)을 찾을 수 없습니다."
    exit 1
}

$DAEMON = "$env:USERPROFILE\.claude\scripts\slack-jipsa\gemini-daemon.py"
python $DAEMON
