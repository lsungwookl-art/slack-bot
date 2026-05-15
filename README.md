# slack-bot

Slack ↔ Claude Code 로컬 에이전트 세팅 모음.  
Windows 11 + Python 3.14 + Claude Code CLI 기준.

---

## 모듈 구성

| 모듈 | 설명 |
|------|------|
| 1 | Slack → Claude Code 양방향 대화 (Socket Mode 봇) |
| 2 | 폴더 트리거 자동 처리 (바탕화면/slack-bot 감시) |
| 3 | 폴더 처리 결과 Slack 알림 |
| 4 | Claude Code 세션 종료 시 Slack + Notion 자동 아카이브 |

---

## 파일 구조

```
slack-bot/
├── hooks/
│   └── slack-session-summary.py   # Claude Code Stop hook (Slack + Notion 전송)
├── scripts/
│   ├── slack-jipsa/
│   │   ├── daemon.py              # Slack Socket Mode 봇 데몬
│   │   └── run.ps1                # 데몬 실행 스크립트 (Task Scheduler용)
│   └── folder-watch/
│       └── folder-watch.ps1       # 폴더 감시 + Claude 처리 + Slack 알림
├── secrets/
│   └── slack-jipsa.env.example    # 환경변수 양식 (실제 값은 .gitignore 처리)
└── settings.json                  # Claude Code hook 설정 예시
```

---

## 설치 방법

### 1. 환경변수 파일 설정

```
~/.claude/secrets/slack-jipsa.env
```

`secrets/slack-jipsa.env.example`을 복사해서 실제 값으로 채운다.

필요한 항목:
- `SLACK_BOT_TOKEN` — Slack 봇 토큰 (`xoxb-...`)
- `SLACK_APP_TOKEN` — Slack 앱 토큰 (`xapp-...`)
- `SLACK_CHANNEL` — 봇이 응답할 채널 ID
- `SLACK_SESSION_WEBHOOK` — Incoming Webhook URL
- `NOTION_API_TOKEN` — Notion Integration 토큰
- `NOTION_SESSION_DB` — 턴 로그 DB ID
- `NOTION_DAILY_DB` — 일일 통합 DB ID

### 2. 파일 배치

```
~/.claude/hooks/slack-session-summary.py
~/.claude/scripts/slack-jipsa/daemon.py
~/.claude/scripts/slack-jipsa/run.ps1
~/.claude/scripts/folder-watch/folder-watch.ps1
```

### 3. Claude Code Stop hook 등록

`~/.claude/settings.json`에 추가:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "py -3 C:\\Users\\<USERNAME>\\.claude\\hooks\\slack-session-summary.py"
          }
        ]
      }
    ]
  }
}
```

### 4. Windows 작업 스케줄러 등록

로그인 시 자동 실행되도록 두 작업을 등록한다.

**SlackJipsa** (Slack 봇 데몬):
```
트리거: 로그온 시
작업: powershell.exe -File "~\.claude\scripts\slack-jipsa\run.ps1"
재시작: 오류 시 5회
```

**FolderWatch** (폴더 감시):
```
트리거: 로그온 시
작업: powershell.exe -File "~\.claude\scripts\folder-watch\folder-watch.ps1"
재시작: 오류 시 999회
```

---

## 동작 방식

**Slack 봇**
1. Slack 채널에 메시지 입력
2. daemon.py가 Socket Mode로 수신
3. `claude --print`로 Claude API 호출
4. 결과를 Slack에 전송

**폴더 트리거**
1. `바탕화면/slack-bot/` 에 파일 투하
2. folder-watch.ps1이 5초마다 감지
3. Claude가 파일 분석 후 `.summary.md` 생성
4. Slack에 완료 알림

**Notion 아카이브**
1. Claude Code 세션 종료 시 Stop hook 발화
2. 마지막 턴의 요청·도구·결과를 Notion DB에 저장
3. 일일 통합 DB와 자동 연결

---

## 주의사항

- `secrets/*.env` 파일은 절대 커밋하지 않는다.
- PC가 켜져 있고 로그인된 상태에서만 봇이 동작한다.
- Python 실행은 `py -3` 사용 (Windows Python Launcher).
