#!/usr/bin/env python3
"""
Slack ↔ Gemini CLI daemon (Agent Bootstrap for Gemini)

흐름:
1. Socket Mode로 슬랙 채널 메시지 실시간 수신
2. 사용자 메시지면 → ⏳ reaction → gemini --yolo --prompt <text> 호출
3. 응답 → 채널 메인에 post → ⏳ 제거 + ✅ reaction
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

# fcntl은 Unix 전용. Windows에선 무시.
try:
    import fcntl
except ImportError:
    fcntl = None

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

# 설정 파일 경로 (윈도우/맥 공용)
SECRETS = Path.home() / '.claude/secrets/gemini-jipsa.env'
SESSIONS_DIR = Path.home() / '.claude/scripts/slack-jipsa/sessions-gemini'
LOGS_DIR = Path.home() / '.claude/scripts/slack-jipsa/logs-gemini'
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

def load_env() -> dict[str, str]:
    env = {}
    if not SECRETS.exists():
        print(f"Error: {SECRETS} 파일이 없습니다.")
        sys.exit(1)
    for line in SECRETS.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env

ENV = load_env()
BOT_TOKEN = ENV['SLACK_BOT_TOKEN']
APP_TOKEN = ENV['SLACK_APP_TOKEN']
CHANNEL = ENV['SLACK_CHANNEL']
MIRI = ENV.get('USER_SLACK_ID') or ENV.get('MIRI_USER_ID', '')
BOT = ENV['BOT_USER_ID']
USER_NAME = ENV.get('USER_NAME', '사용자')
BOT_NAME = ENV.get('SLACK_BOT_NAME', '제미나이 비서')

web = WebClient(token=BOT_TOKEN)
sock = SocketModeClient(app_token=APP_TOKEN, web_client=web)

def log(msg: str) -> None:
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    today = time.strftime('%Y-%m-%d')
    with (LOGS_DIR / f'{today}.log').open('a', encoding='utf-8') as f:
        f.write(line + '\n')

def session_path(channel: str) -> Path:
    return SESSIONS_DIR / f'{channel}.txt'

def get_or_create_session(channel: str) -> tuple[str, bool]:
    p = session_path(channel)
    if p.exists():
        sid = p.read_text().strip()
        if sid: return sid, False
    sid = str(uuid.uuid4())
    p.write_text(sid)
    return sid, True

def reset_session(channel: str) -> str:
    sid = str(uuid.uuid4())
    session_path(channel).write_text(sid)
    return sid

SYSTEM_PROMPT = f"당신은 {USER_NAME}님의 슬랙 비서 '{BOT_NAME}'(Gemini CLI)입니다. 친절하고 간결하게 답변하세요."

def _run_gemini(prompt: str, session_id: str, is_new: bool, timeout: int) -> subprocess.CompletedProcess:
    # 제미나이 명령어 구성
    full_prompt = f"{SYSTEM_PROMPT}\n\n사용자 메시지: {prompt}"
    cmd = [
        'gemini',
        '--yolo',
        '--output-format', 'text',
        '--prompt', full_prompt
    ]
    if is_new:
        cmd.extend(['--session-id', session_id])
    else:
        cmd.extend(['--resume', session_id])
    
    # 윈도우에서는 shell=True가 필요할 수 있음
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=timeout, shell=True)

def call_gemini(prompt: str, channel: str, timeout: int = 300) -> str:
    sid, is_new = get_or_create_session(channel)
    try:
        r = _run_gemini(prompt, sid, is_new, timeout)
        # 세션 만료 시 새 세션으로 재시도
        if r.returncode != 0 and not is_new:
            log(f'  resume fail, fallback to new session')
            new_sid = reset_session(channel)
            r = _run_gemini(prompt, new_sid, True, timeout)
    except subprocess.TimeoutExpired:
        return f'⏱️ 타임아웃 ({timeout}초). 제미나이가 너무 바쁘네요.'
    
    if r.returncode != 0:
        log(f'  gemini fail rc={r.returncode}: {(r.stderr or "")[-300:]}')
        return f"❌ 에러가 발생했습니다: {r.stderr[-100:] if r.stderr else '알 수 없는 오류'}"
    
    return (r.stdout or '').strip()

def handle_message(event: dict) -> None:
    text = event.get('text', '').strip()
    channel = event.get('channel', '')
    ts = event.get('ts', '')
    user = event.get('user', '')

    if not text: return
    
    # 상세 로그 (진단용)
    log(f"[진단] 메시지 수신 - 채널: {channel}, 사용자: {user}, 내용: {text[:20]}...")

    if channel != CHANNEL:
        log(f"  [무시] 채널 일치하지 않음 (설정된 채널: {CHANNEL})")
        return
    if user == BOT: return # 자기 자신 무시
    if user != MIRI:
        log(f"  [무시] 허용된 사용자 아님 (설정된 사용자: {MIRI})")
        return

    if text.lower() in ('리셋', 'reset', '새세션'):
        new_sid = reset_session(channel)
        web.chat_postMessage(channel=channel, text=f'🔄 제미나이 세션 리셋 (`{new_sid[:8]}`)')
        return

    log(f'msg: {text[:50]}...')

    # ⏳ 반응 추가
    try:
        web.reactions_add(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
    except: pass

    # 제미나이 호출
    reply = call_gemini(text, channel)
    
    # 결과 전송
    try:
        web.chat_postMessage(channel=channel, text=reply)
    except Exception as e:
        log(f'  post fail: {e}')

    # ⏳ 제거, ✅ 추가
    try:
        web.reactions_remove(channel=channel, timestamp=ts, name='hourglass_flowing_sand')
        web.reactions_add(channel=channel, timestamp=ts, name='white_check_mark')
    except: pass

def on_event(client: SocketModeClient, req: SocketModeRequest) -> None:
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    if req.type != 'events_api': return
    event = req.payload.get('event', {})
    if event.get('type') == 'message' and not event.get('subtype'):
        threading.Thread(target=handle_message, args=(event,), daemon=True).start()

def main() -> None:
    log(f'=== {BOT_NAME} (Gemini) 시작 ===')
    sock.socket_mode_request_listeners.append(on_event)
    sock.connect()
    while True:
        time.sleep(60)

if __name__ == '__main__':
    main()
