#!/usr/bin/env python3
"""Claude Code Stop hook — Windows용. Slack webhook + Notion DB 이중 적재."""
import json, os, sys, re
from pathlib import Path
from urllib import request as urlreq
from collections import Counter
from datetime import datetime, timezone, timedelta

HOOK_LOG = Path(os.environ.get("HOOK_LOG", str(Path.home() / ".claude/hooks/stop-hook.log")))

def _log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with HOOK_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""

def is_real_user(entry):
    if entry.get("type") != "user":
        return False
    txt = text_content((entry.get("message") or {}).get("content", ""))
    if not re.sub(r"\s+", "", txt):
        return False
    if re.match(r"^\s*<task-notification>", txt):
        return False
    return True

def load_env_file():
    env_path = Path.home() / ".claude/secrets/slack-jipsa.env"
    if not env_path.exists():
        return {}
    env = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env

def http_post(url, data, headers):
    req = urlreq.Request(url, data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                         headers=headers, method="POST")
    with urlreq.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")

def http_patch(url, data, headers):
    req = urlreq.Request(url, data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                         headers=headers, method="PATCH")
    with urlreq.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")

def main():
    if os.environ.get("SLACK_HOOK_RUNNING"):
        sys.exit(0)
    os.environ["SLACK_HOOK_RUNNING"] = "1"
    if os.environ.get("CLAUDE_SKIP_SUMMARY") == "1" or os.environ.get("CLAUDE_SKIP_HOOKS") == "1":
        sys.exit(0)

    file_env = load_env_file()
    slack_url = os.environ.get("SLACK_SESSION_WEBHOOK") or file_env.get("SLACK_SESSION_WEBHOOK", "")
    notion_token = os.environ.get("NOTION_API_TOKEN") or file_env.get("NOTION_API_TOKEN", "")
    notion_db = os.environ.get("NOTION_SESSION_DB") or file_env.get("NOTION_SESSION_DB", "")
    notion_daily_db = os.environ.get("NOTION_DAILY_DB") or file_env.get("NOTION_DAILY_DB", "")

    if not slack_url and not notion_token:
        sys.exit(0)

    try:
        raw = sys.stdin.buffer.read()
        stdin_json = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        sys.exit(0)

    session_id = stdin_json.get("session_id", "")
    cwd = stdin_json.get("cwd") or os.getcwd()
    if not session_id:
        sys.exit(0)

    project_name = Path(cwd).name

    transcript = None
    for f in (Path.home() / ".claude/projects").rglob(f"{session_id}.jsonl"):
        transcript = f
        break
    if not transcript or not transcript.exists():
        sys.exit(0)

    entries = []
    for line in transcript.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            e = json.loads(line)
            if e.get("type") in ("user", "assistant"):
                entries.append(e)
        except Exception:
            pass

    last_user_idx = None
    for i, e in enumerate(entries):
        if is_real_user(e):
            last_user_idx = i
    if last_user_idx is None:
        sys.exit(0)

    turn = entries[last_user_idx:]
    user_prompt = text_content((turn[0].get("message") or {}).get("content", ""))[:200]

    tools = []
    asst_texts = []
    model_used = "unknown"
    for e in turn:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        if msg.get("model"):
            model_used = msg["model"]
        content = msg.get("content", [])
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "tool_use":
                        tools.append(b.get("name", "?"))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        asst_texts.append(b["text"])
        elif isinstance(content, str) and content.strip():
            asst_texts.append(content)

    result_txt = (asst_texts[-1] if asst_texts else "")[:200]

    if not user_prompt and not tools:
        sys.exit(0)

    tool_counts = Counter(tools)
    actions_md = ", ".join(
        f"{n} x{c}" if c > 1 else n for n, c in tool_counts.items()
    ) if tool_counts else "(도구 없음)"

    # 모델명 축약
    model_short = re.sub(r"claude-opus-\d.*", "opus", model_used)
    model_short = re.sub(r"claude-sonnet-\d.*", "sonnet", model_short)
    model_short = re.sub(r"claude-haiku-\d.*", "haiku", model_short)
    if model_short == model_used:
        model_short = "unknown"

    _log(f"hook start session={session_id} project={project_name} tools={len(tools)}")

    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    ts_hm = now.strftime("%H:%M")
    ts_iso = now.isoformat()
    date_str = now.date().isoformat()
    short_session = session_id[:8]

    # ── Slack 전송 ──────────────────────────────────────────────────
    if slack_url:
        body = f"🎯 *시킨 일*\n{user_prompt}\n\n📝 *한 일*\n{actions_md}\n\n🧠 *결과*\n{result_txt}"
        payload = {
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"🤖 {project_name}"}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"⏰ {ts_hm} KST  ·  세션 `{short_session}`"}]},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": body}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"📁 `{cwd}`"}]},
                {"type": "divider"}
            ],
            "text": f"Claude 턴 · {project_name}"
        }
        try:
            req = urlreq.Request(slack_url,
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
            with urlreq.urlopen(req, timeout=10) as r:
                _log(f"slack sent: {r.status}")
        except Exception as e:
            _log(f"slack error: {e}")

    # ── Notion 적재 ─────────────────────────────────────────────────
    if notion_token and notion_db:
        notion_headers = {
            "Authorization": f"Bearer {notion_token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }

        def trim(s, n=1900): return (s or "")[:n]
        def rt(s): return [{"text": {"content": trim(s)}}]

        # 일일 통합 row 가져오거나 생성
        daily_id = None
        if notion_daily_db:
            try:
                q = http_post(f"https://api.notion.com/v1/databases/{notion_daily_db}/query",
                              {"filter": {"property": "날짜", "date": {"equals": date_str}}, "page_size": 1},
                              notion_headers)
                if q.get("results"):
                    daily_id = q["results"][0]["id"]
                else:
                    p = http_post("https://api.notion.com/v1/pages", {
                        "parent": {"database_id": notion_daily_db},
                        "properties": {
                            "이름": {"title": [{"text": {"content": f"{date_str} 일일 통합"}}]},
                            "날짜": {"date": {"start": date_str}},
                            "external_id": {"rich_text": rt(f"daily:{date_str}")}
                        }
                    }, notion_headers)
                    daily_id = p.get("id")
            except Exception as e:
                _log(f"notion daily error: {e}")

        # 턴 로그 upsert (external_id 기준)
        turn_count = sum(1 for e in entries[:last_user_idx+1] if is_real_user(e))
        external_id = f"claude:{session_id}:{turn_count}"
        try:
            # 기존 row 검색
            q = http_post(f"https://api.notion.com/v1/databases/{notion_db}/query", {
                "filter": {"property": "external_id", "rich_text": {"equals": external_id}},
                "page_size": 1
            }, notion_headers)
            props = {
                "프로젝트": {"title": [{"text": {"content": trim(project_name)}}]},
                "시각": {"date": {"start": ts_iso}},
                "세션 ID": {"rich_text": rt(session_id)},
                "작업 디렉토리": {"rich_text": rt(cwd)},
                "시킨 일": {"rich_text": rt(user_prompt)},
                "한 일": {"rich_text": rt(actions_md)},
                "결과": {"rich_text": rt(result_txt)},
                "확인 필요": {"rich_text": rt("없음")},
                "모델": {"select": {"name": model_short}},
                "도구 호출 수": {"number": len(tools)},
                "전체 요약": {"rich_text": rt(f"{user_prompt}\n\n{actions_md}\n\n{result_txt}")},
                "external_id": {"rich_text": rt(external_id)},
            }
            if daily_id:
                props["📊 일일 통합"] = {"relation": [{"id": daily_id}]}

            if q.get("results"):
                page_id = q["results"][0]["id"]
                http_patch(f"https://api.notion.com/v1/pages/{page_id}", {"properties": props}, notion_headers)
                _log(f"notion updated: {page_id}")
            else:
                resp = http_post("https://api.notion.com/v1/pages", {
                    "parent": {"database_id": notion_db},
                    "properties": props
                }, notion_headers)
                _log(f"notion created: {resp.get('id')}")
        except Exception as e:
            _log(f"notion error: {e}")

    sys.exit(0)

if __name__ == "__main__":
    main()
