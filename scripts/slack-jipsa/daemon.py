#!/usr/bin/env python3
"""
Slack ↔ Claude Code daemon (Agent Bootstrap)

흐름:
1. Socket Mode로 슬랙 채널 메시지 실시간 수신
2. 사용자 메시지면 → ⏳ reaction → claude --print --resume <session_id> 호출
3. 응답 → 채널 메인에 post → ⏳ 제거 + ✅ reaction
4. (옵션) 한 턴을 노션 'Claude Code 턴 로그' DB에 적재

세션 유지:
- 채널별 session_id를 ~/.claude/scripts/slack-jipsa/sessions/{channel}.txt 에 저장
- 첫 메시지: --session-id <uuid> 로 새 세션 시작
- 이후: --resume <session_id> 로 같은 세션 이어감

검증된 코드를 일반 배포용으로 다음 항목만 환경변수화:
- NOTION_SESSION_DB / NOTION_DAILY_DB (env, 비어있으면 노션 적재 skip)
- USER_SLACK_ID (구 MIRI_USER_ID alias 지원)
- USER_NAME (시스템 프롬프트, 기본 '사용자')
- SLACK_BOT_NAME (노션 프로젝트 컬럼명, 기본 '슬랙 비서')
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import uuid
import subprocess
import threading
from pathlib import Path

# fcntl은 Unix 전용. Windows에선 None.
try:
    import fcntl  # type: ignore
except ImportError:
    fcntl = None  # type: ignore

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

SECRETS = Path.home() / '.claude/secrets/slack-jipsa.env'
SESSIONS_DIR = Path.home() / '.claude/scripts/slack-jipsa/sessions'
LOGS_DIR = Path.home() / '.claude/scripts/slack-jipsa/logs'
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Notion DB IDs — 사용자 .env에서 받음. 비어있으면 노션 적재 skip.
NOTION_SESSION_DB = ''   # set after load_env()
NOTION_DAILY_DB = ''     # set after load_env() (optional)
sys.path.insert(0, str(Path.home() / '.claude/scripts'))

# 공유 대화 버퍼 (클코 + 코덱스 둘 다 read/write)
SHARED_DIR = Path.home() / '.claude/scripts/slack-jipsa-shared'
SHARED_DIR.mkdir(parents=True, exist_ok=True)
SHARED_BUFFER_LIMIT = 30


def shared_buffer_path(channel: str, thread_ts: str = '') -> Path:
    key = f'slack_{channel}_{thread_ts or "root"}'
    return SHARED_DIR / f'{key}.jsonl'


def load_shared(channel: str, thread_ts: str = '') -> list[dict]:
    p = shared_buffer_path(channel, thread_ts)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines()[-SHARED_BUFFER_LIMIT:]:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def append_shared(channel: str, thread_ts: str, who: str, text: str, msg_ts: str = '') -> None:
    """공유 버퍼에 추가. msg_ts는 Slack event/post ts이며 중복 방지 키다."""
    p = shared_buffer_path(channel, thread_ts)
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {
        'ts': time.time(),
        'msg_ts': msg_ts,
        'who': who,
        'text': text[:2000],
    }
    with p.open('a+', encoding='utf-8') as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            if msg_ts:
                f.seek(0)
                for line in f.read().splitlines()[-50:]:
                    try:
                        if json.loads(line).get('msg_ts') == msg_ts:
                            return
                    except Exception:
                        pass
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_env() -> dict[str, str]:
    env = {}
    for line in SECRETS.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
BOT_TOKEN = ENV['SLACK_BOT_TOKEN']
APP_TOKEN = ENV['SLACK_APP_TOKEN']
CHANNEL = ENV['SLACK_CHANNEL']
CHANNEL_DIALOG = ENV.get('SLACK_CHANNEL_DIALOG', '')  # 두 봇 대화 채널 (옵션)
# USER_SLACK_ID = 봇이 응답할 대상 사용자. (구 변수명 MIRI_USER_ID alias 지원)
MIRI = ENV.get('USER_SLACK_ID') or ENV.get('MIRI_USER_ID', '')
BOT = ENV['BOT_USER_ID']
USER_NAME = ENV.get('USER_NAME', '사용자')
BOT_NAME = ENV.get('SLACK_BOT_NAME', '슬랙 비서')
DIALOG_TURN_LIMIT = 6  # 대화 채널에서 봇 자기 응답 최대 N턴 (무한루프 방지)

# Notion DB IDs from .env (set globals declared above)
NOTION_SESSION_DB = ENV.get('NOTION_SESSION_DB', '')
NOTION_DAILY_DB = ENV.get('NOTION_DAILY_DB', '')

# 단톡 토론 모드 트리거: 사용자 발화에 매치되면 봇끼리 자유 응답 허용
DISCUSSION_TRIGGER = re.compile(
    r'(토론|비교|반박|의견\s*(나눠|줘|얘기|교환)|각자\s*의견|둘이|서로\s*의견)',
    re.IGNORECASE,
)
# 토론 종료 신호
DISCUSSION_STOP = re.compile(
    r'(\b그만\b|\b종료\b|\bstop\b|\b끝\b|\b정리\b|\b중단\b|토론\s*그만|토론\s*종료)',
    re.IGNORECASE,
)

web = WebClient(token=BOT_TOKEN)
sock = SocketModeClient(app_token=APP_TOKEN, web_client=web)


def log(msg: str) -> None:
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    today = time.strftime('%Y-%m-%d')
    (LOGS_DIR / f'{today}.log').open('a').write(line + '\n')


def session_path(channel: str) -> Path:
    return SESSIONS_DIR / f'{channel}.txt'


def get_or_create_session(channel: str) -> tuple[str, bool]:
    """채널의 session_id 반환. 없으면 새로 생성. (id, is_new)"""
    p = session_path(channel)
    if p.exists():
        sid = p.read_text().strip()
        if sid: return sid, False
    sid = str(uuid.uuid4())
    p.write_text(sid)
    return sid, True


def reset_session(channel: str) -> str:
    """세션 리셋. 새 session_id 생성."""
    sid = str(uuid.uuid4())
    session_path(channel).write_text(sid)
    return sid


SYSTEM_PROMPT = f"""당신은 {USER_NAME}님의 슬랙 비서 '{BOT_NAME}'입니다.

**필수**: cwd `~/.claude/scripts/slack-jipsa/`의 CLAUDE.md를 절대 규칙으로 따르세요.
페르소나, 슬랙 mrkdwn, 도구 호출 제한, 일정/가계부/캘린더 필터 규칙 모두 거기 있습니다.

규칙 어기면 {USER_NAME}님이 직접 지적합니다. 같은 실수 반복 금지."""


def _run_claude(prompt: str, session_id: str, is_new: bool, timeout: int) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env['CLAUDE_SKIP_HOOKS'] = '1'
    cmd = [
        'claude', '--print',
        '--permission-mode', 'bypassPermissions',
        '--dangerously-skip-permissions',
        '--add-dir', str(Path.home()),
        '--output-format', 'text',
        '--model', 'opus',
        '--append-system-prompt', SYSTEM_PROMPT,
    ]
    cmd.extend(['--session-id', session_id] if is_new else ['--resume', session_id])
    # cwd를 slack-jipsa 디렉토리로 → CLAUDE.md 자동 로드 → 절대 규칙 적용
    cwd = str(Path.home() / '.claude/scripts/slack-jipsa')
    return subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=timeout)


def call_claude(prompt: str, channel: str, timeout: int = 900) -> str:
    """클로드 코드 호출. resume 실패 시 자동으로 새 session 재시도."""
    sid, is_new = get_or_create_session(channel)
    try:
        r = _run_claude(prompt, sid, is_new, timeout)
        # resume 실패 (jsonl 없음) → 새 session으로 재시도
        if r.returncode != 0 and not is_new and 'No conversation found' in (r.stderr or ''):
            log(f'  resume fail, fallback to new session')
            new_sid = reset_session(channel)
            r = _run_claude(prompt, new_sid, True, timeout)
    except subprocess.TimeoutExpired:
        return f'⏱️ 타임아웃 ({timeout}초). 작업이 너무 길어요.'
    if r.returncode != 0:
        # 슬랙에 fail 메시지 보내지 않음 (잡음). stderr는 로그로만.
        log(f'  claude fail rc={r.returncode}: {(r.stderr or "")[-300:]}')
        return '__SILENT_FAIL__'
    return (r.stdout or '').strip()


_dialog_self_turn_count = 0  # 대화 채널에서 내 연속 응답 카운트 (무한루프 방지)
_discussion_mode: dict[str, bool] = {}  # channel → 토론 모드 ON/OFF

# discussion 상태 공유 파일 (proactive cron 스크립트가 read 함)
DISCUSSION_STATE_FILE = SHARED_DIR / 'discussion_state.json'


def _write_discussion_state() -> None:
    """현재 _discussion_mode 상태를 JSON 파일로 저장."""
    try:
        DISCUSSION_STATE_FILE.write_text(json.dumps({
            'mode': dict(_discussion_mode),
            'ts': time.time(),
        }))
    except Exception:
        pass


def notion_log_turn(channel: str, event_ts: str, user_text: str, reply_text: str,
                    session_id: str, model: str = 'opus') -> None:
    """슬랙 ↔ 클코 한 턴을 노션 'Claude Code 턴 로그' DB에 적재.

    daemon이 claude --print headless 모드라 Stop hook 발동 안 함 → 직접 적재.

    NOTION_SESSION_DB가 비어있으면 skip (옵션 기능).
    """
    if not NOTION_SESSION_DB:
        return
    try:
        import json as _json
        import urllib.request as _urlreq
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from lib.notion import upsert_by_external_id

        kst = _tz(_td(hours=9))
        now = _dt.now(kst)
        ts_iso = now.isoformat()
        date_str = now.date().isoformat()

        def _trim(s: str, n: int = 1900) -> str:
            return (s or '')[:n]

        # NOTION_API_TOKEN 우선 (NOTION_TOKEN/notion-token.txt는 legacy fallback)
        token = (
            os.environ.get('NOTION_API_TOKEN')
            or os.environ.get('NOTION_TOKEN')
            or ''
        )
        if not token:
            legacy = os.path.expanduser('~/.claude/secrets/notion-token.txt')
            if os.path.exists(legacy):
                token = open(legacy).read().strip()
        if not token:
            log('  notion log skip: no NOTION_API_TOKEN')
            return
        headers = {'Authorization': f'Bearer {token}', 'Notion-Version': '2022-06-28',
                   'Content-Type': 'application/json'}

        def _http(method, url, data=None):
            if data is not None:
                data = _json.dumps(data).encode()
            req = _urlreq.Request(url, data=data, headers=headers, method=method)
            with _urlreq.urlopen(req, timeout=15) as r:
                return _json.loads(r.read() or b'{}')

        # NOTION_DAILY_DB 비어있으면 _http에서 즉시 실패 → except로 빠지고 daily_id=None 유지
        daily_id = None
        try:
            r = _http('POST', f'https://api.notion.com/v1/databases/{NOTION_DAILY_DB}/query',
                {'filter': {'property': '날짜', 'date': {'equals': date_str}}, 'page_size': 1})
            if r.get('results'):
                daily_id = r['results'][0]['id']
            else:
                p = _http('POST', 'https://api.notion.com/v1/pages', {
                    'parent': {'database_id': NOTION_DAILY_DB},
                    'properties': {
                        '이름': {'title': [{'text': {'content': f'{date_str} 일일 통합'}}]},
                        '날짜': {'date': {'start': date_str}},
                        '상태': {'status': {'name': '진행 중'}},
                        'external_id': {'rich_text': [{'text': {'content': f'daily:{date_str}'}}]},
                    },
                })
                daily_id = p.get('id')
        except Exception:
            pass

        properties = {
            '프로젝트': {'title': [{'text': {'content': f'{BOT_NAME} (슬랙)'}}]},
            '시각': {'date': {'start': ts_iso}},
            '세션 ID': {'rich_text': [{'text': {'content': session_id}}]},
            '작업 디렉토리': {'rich_text': [{'text': {'content': str(Path.home() / '.claude/scripts/slack-jipsa')}}]},
            '시킨 일': {'rich_text': [{'text': {'content': _trim(user_text)}}]},
            '한 일': {'rich_text': [{'text': {'content': _trim(reply_text)}}]},
            '결과': {'rich_text': [{'text': {'content': _trim(reply_text)}}]},
            '모델': {'select': {'name': model}},
            '도구 호출 수': {'number': 0},
            '전체 요약': {'rich_text': [{'text': {'content': _trim(user_text + ' → ' + reply_text)}}]},
        }
        if daily_id:
            properties['📊 일일 통합'] = {'relation': [{'id': daily_id}]}

        ext_id = f'jipsa:{channel}:{event_ts}'
        upsert_by_external_id(NOTION_SESSION_DB, ext_id, properties)
    except Exception as e:
        log(f'  notion log fail: {e}')


def handle_message(event: dict) -> None:
    """사용자 메시지 처리 + (대화 채널이면) 다른 봇 메시지에도 반응."""
    global _dialog_self_turn_count
    text = event.get('text', '').strip()
    channel = event.get('channel', '')
    ts = event.get('ts', '')
    user = event.get('user', '')
    bot_id = event.get('bot_id', '')

    if not text: return
    if channel != CHANNEL and channel != CHANNEL_DIALOG: return

    global _discussion_mode
    is_dialog = (channel == CHANNEL_DIALOG)
    is_miri = (user == MIRI)
    is_self = (user == BOT or bot_id == BOT)
    is_other_bot = (not is_miri and not is_self and (user.startswith('U') and user != MIRI))

    if is_self: return  # 자기 자신 무시
    if not is_dialog and not is_miri: return  # 메인 채널은 사용자만

    # 단톡 토론 모드 관리 (사용자 발화 기준 ON/OFF)
    if is_dialog and is_miri:
        if DISCUSSION_STOP.search(text):
            _discussion_mode[channel] = False
            _write_discussion_state()
            log(f'  discussion mode OFF (stop keyword)')
        elif DISCUSSION_TRIGGER.search(text):
            _discussion_mode[channel] = True
            _dialog_self_turn_count = 0
            _write_discussion_state()
            log(f'  discussion mode ON (trigger keyword)')
        else:
            # 사용자의 일반 발화 = 새 주제 = 토론 종료
            was_on = _discussion_mode.get(channel, False)
            _discussion_mode[channel] = False
            _dialog_self_turn_count = 0
            if was_on:
                _write_discussion_state()
                log(f'  discussion mode OFF (new topic from user)')

    # 다른 봇 발화: discussion 모드가 켜져있을 때만 응답 허용
    if is_other_bot:
        thread_ts_only = event.get('thread_ts', '')
        try:
            append_shared(channel, thread_ts_only, '코덱스', text, msg_ts=ts)
        except Exception:
            pass
        if not _discussion_mode.get(channel):
            log(f'  other-bot message, discussion OFF — skip response')
            return
        if _dialog_self_turn_count >= DIALOG_TURN_LIMIT:
            log(f'  dialog turn limit ({DIALOG_TURN_LIMIT}) — auto-stop discussion')
            _discussion_mode[channel] = False
            _write_discussion_state()
            return
        log(f'  discussion ON — respond to other-bot (turn {_dialog_self_turn_count}/{DIALOG_TURN_LIMIT})')

    # 명령어: '리셋' / '새세션' / 'reset' (단독 키워드)
    if text.strip().lower() in ('리셋', '새세션', '새 세션', 'reset', '!reset', '!리셋'):
        new_sid = reset_session(channel)
        web.chat_postMessage(channel=channel, text=f'🔄 새 세션 시작 (`{new_sid[:8]}`)')
        return

    log(f'msg: {text[:80]}')

    # ⏳ reaction
    try:
        web.reactions_add(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
    except Exception as e:
        log(f'  reaction add fail: {e}')

    thread_ts = event.get('thread_ts', '')
    # 공유 버퍼 적재 (사용자 또는 다른 봇 발화)
    who_label = USER_NAME if is_miri else ('other-bot' if is_other_bot else '?')  # 원본의 '코덱스' 라벨 → 일반화
    append_shared(channel, thread_ts, who_label, text, msg_ts=ts)

    # 공유 버퍼 맥락을 prompt에 prefix로 추가 (단톡↔갠톡 cross-channel)
    shared = load_shared(channel, thread_ts)
    prompt_with_ctx = text
    if shared and len(shared) > 1:
        ctx_lines = [f'## 최근 대화 맥락 ({USER_NAME}·Claude·다른 봇 모두 포함)']
        for h in shared[-15:-1]:  # 마지막은 방금 들어온 거라 제외
            ctx_lines.append(f'[{h.get("who","?")}] {h.get("text","")[:400]}')
        ctx_lines.append('')
        ctx_lines.append(f'## 현재 메시지')
        ctx_lines.append(text)
        prompt_with_ctx = '\n'.join(ctx_lines)

    # 클로드 호출 (resume 실패 시 자동 fallback)
    reply = call_claude(prompt_with_ctx, channel)
    log(f'  reply: {reply[:80]}')

    # 자기가 응답할 차례가 아니라 판단 → 시스템이 SKIP 출력 → post 안 함
    if reply.strip().upper().startswith('SKIP'):
        log(f'  SKIP — 다른 봇이 응답할 차례')
        try:
            web.reactions_remove(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
            web.reactions_add(channel=channel, timestamp=ts, name='eyes')
        except Exception:
            pass
        return

    # 빈 응답 또는 silent fail → 슬랙 푸시 안 함, reaction만
    if not reply.strip() or reply.strip() == '__SILENT_FAIL__':
        is_fail = reply.strip() == '__SILENT_FAIL__'
        log(f'  empty/fail reply — slack 미전송 (fail={is_fail})')
        try:
            web.reactions_remove(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
            web.reactions_add(channel=channel, timestamp=ts,
                              name='warning' if is_fail else 'speech_balloon')
        except Exception:
            pass
        return

    # 응답 마크다운 잔재 제거 (시스템 프롬프트로 못 막은 경우 안전망)
    sys.path.insert(0, str(Path.home() / '.claude/scripts'))
    try:
        from lib.slack_mrkdwn import to_mrkdwn
        reply_clean = to_mrkdwn(reply)
    except Exception:
        reply_clean = reply

    try:
        res = web.chat_postMessage(channel=channel, text=reply_clean, mrkdwn=True)
        if channel == CHANNEL_DIALOG:
            _dialog_self_turn_count += 1
        # 공유 버퍼에 클코 응답 적재
        append_shared(channel, thread_ts, '클코', reply_clean, msg_ts=str(res.get('ts', '') or ''))
    except Exception as e:
        log(f'  post fail: {e}')

    # ⏳ 제거, ✅ 추가
    try:
        web.reactions_remove(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
        web.reactions_add(channel=channel, timestamp=ts, name='white_check_mark')
    except Exception as e:
        log(f'  reaction swap fail: {e}')

    # 노션 턴 로그 적재 (Stop hook 우회) — 비동기
    try:
        sid_for_log, _ = get_or_create_session(channel)
        threading.Thread(
            target=notion_log_turn,
            args=(channel, ts, text, reply_clean, sid_for_log, 'opus'),
            daemon=True,
        ).start()
    except Exception as e:
        log(f'  notion log thread fail: {e}')


def handle_file_share(event: dict) -> None:
    """슬랙 파일 공유 처리 — 다운로드 후 Claude에게 분석 요청."""
    import urllib.request as _urlreq

    channel = event.get('channel', '')
    ts = event.get('ts', '')
    user = event.get('user', '')
    files = event.get('files', [])

    if not files: return
    if user != MIRI: return
    if channel != CHANNEL: return

    file_info = files[0]
    file_name = file_info.get('name', 'file')
    file_url = (file_info.get('url_private_download') or file_info.get('url_private', ''))
    if not file_url:
        return

    log(f'file: {file_name}')
    try:
        web.reactions_add(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
    except Exception:
        pass

    try:
        save_dir = Path.home() / '.claude/scripts/slack-jipsa/uploads'
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / file_name
        req_dl = _urlreq.Request(file_url, headers={'Authorization': f'Bearer {BOT_TOKEN}'})
        with _urlreq.urlopen(req_dl, timeout=30) as r:
            save_path.write_bytes(r.read())
        log(f'  saved: {save_path}')
    except Exception as e:
        log(f'  file download fail: {e}')
        web.chat_postMessage(channel=channel, text=f'파일 다운로드 실패: {e}')
        return

    comment = (event.get('text') or '').strip()
    prompt = f"슬랙에서 파일이 왔어. 분석해서 핵심 내용을 요약해줘.\n파일 경로: {save_path}"
    if comment:
        prompt += f"\n사용자 코멘트: {comment}"

    reply = call_claude(prompt, channel)
    log(f'  reply: {reply[:80]}')

    if reply and reply not in ('__SILENT_FAIL__', ''):
        try:
            from lib.slack_mrkdwn import to_mrkdwn
            reply = to_mrkdwn(reply)
        except Exception:
            pass
        try:
            web.chat_postMessage(channel=channel, text=reply, mrkdwn=True)
        except Exception as e:
            log(f'  post fail: {e}')

    try:
        web.reactions_remove(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
        web.reactions_add(channel=channel, timestamp=ts, name='white_check_mark')
    except Exception as e:
        log(f'  reaction swap fail: {e}')


def on_event(client: SocketModeClient, req: SocketModeRequest) -> None:
    # Slack에 즉시 ACK (3초 이내 필수)
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    if req.type != 'events_api': return
    event = req.payload.get('event', {})
    etype = event.get('type')
    subtype = event.get('subtype')
    has_files = bool(event.get('files'))
    if etype != 'message': return
    if subtype == 'file_share' or (not subtype and has_files):
        threading.Thread(target=handle_file_share, args=(event,), daemon=True).start()
    elif not subtype:
        threading.Thread(target=handle_message, args=(event,), daemon=True).start()


def main() -> None:
    log(f'=== {BOT_NAME} daemon 시작 (channel={CHANNEL[:6]}.., bot={BOT}) ===')
    sock.socket_mode_request_listeners.append(on_event)
    sock.connect()
    log('Socket Mode 연결됨. 메시지 대기 중...')
    # 무한 대기
    while True:
        time.sleep(60)


if __name__ == '__main__':
    main()
