# slack-jipsa daemon runner
$envFile = "$env:USERPROFILE\.claude\secrets\slack-jipsa.env"
Get-Content $envFile | ForEach-Object {
    if ($_ -match "^([^#][^=]*)=(.*)$") {
        [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
    }
}
# UTF-8 강제 (daemon.py subprocess 인코딩 문제 해결)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# claude.exe (shim) + npm PATH 명시 (Task Scheduler 환경에서 claude 못 찾는 문제 해결)
$env:PATH = "$env:APPDATA\Python\Python314\Scripts;$env:APPDATA\npm;$env:PATH"
py -3 "$env:USERPROFILE\.claude\scripts\slack-jipsa\daemon.py"
