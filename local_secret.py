"""Opt-in loopback-only, one-shot credential input. Not an OS keychain or vault."""
import re
import secrets
import threading
import time

PAGE = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>로컬 Claude 연결</title><body>
<h1>이번 실행에만 Claude 키 연결</h1>
<p>키는 서버 프로세스 메모리에만 보유합니다. 서버를 종료하면 다시 입력해야 합니다. 이 화면은 비밀 금고가 아니며 같은 컴퓨터의 관리자나 악성 프로그램으로부터 보호하지 못합니다.</p>
<p>Anthropic에 가상 사건 자료를 전송하는 API 호출은 별도의 앱 버튼으로 시작됩니다. 여기에 입력하는 것만으로 모델을 호출하지 않습니다.</p>
<form id="form"><label>Claude API 키 <input id="key" type="password" autocomplete="off" autocapitalize="none" spellcheck="false" required maxlength="512"></label><button>이번 실행에 연결</button></form>
<button id="discard">키 폐기 / 입력 닫기</button><p id="status" role="status"></p>
<script nonce="__NONCE__">
const token='__TOKEN__'; const key=document.getElementById('key'); const status=document.getElementById('status');
async function send(action){let body={action};if(action==='set')body.key=key.value;key.value='';document.getElementById('form').hidden=true;try{const r=await fetch('/api/local-secret',{method:'POST',cache:'no-store',credentials:'omit',headers:{'Content-Type':'application/json','X-Local-Secret-CSRF':token},body:JSON.stringify(body)});body.key='';const d=await r.json();status.textContent=r.ok?(action==='set'?'이번 실행에 연결됐습니다. 키 값은 다시 표시되지 않습니다.':'키 참조를 폐기했습니다. 다시 연결하려면 서버를 재시작하세요.'):d.error.message;}catch(e){body.key='';status.textContent='결과를 확인할 수 없습니다. 다시 입력하지 말고 서버를 종료해 초기화하세요.';}}
document.getElementById('form').onsubmit=e=>{e.preventDefault();send('set')};document.getElementById('discard').onclick=()=>send('clear');window.addEventListener('pagehide',()=>{key.value=''});
</script></body></html>'''

class LocalSecret:
    def __init__(self, adapter, error_class):
        if adapter.provider != 'anthropic' or adapter.key:
            raise ValueError('Local entry requires Anthropic provider without a preconfigured key')
        self.error_class = error_class
        self.adapter = adapter
        self.token = secrets.token_urlsafe(32)
        self.deadline = time.monotonic() + 900
        self.sealed = False
        self.lock = threading.Lock()

    def page(self):
        nonce = secrets.token_urlsafe(24)
        return PAGE.replace('__NONCE__', nonce).replace('__TOKEN__', self.token).encode(), nonce

    def submit(self, token, payload):
        APIError = self.error_class
        with self.lock, self.adapter._usage_lock:
            if not secrets.compare_digest(token, self.token):
                raise APIError('CSRF_REJECTED', '입력 페이지를 다시 확인하세요.', 403)
            if payload.get('action') not in ('set', 'clear'):
                raise APIError('INVALID_ACTION', '잘못된 작업입니다.')
            if self.adapter._active:
                raise APIError('AI_BUSY', '진행 중인 요청이 끝난 뒤 폐기하세요.', 409)
            if payload['action'] == 'clear':
                self.adapter.key = ''
                self.adapter.verified = False
                self.sealed = True
                return {'ok': True, 'present': False}
            if self.sealed or time.monotonic() >= self.deadline:
                raise APIError('ENTRY_CLOSED', '입력이 닫혔습니다. 서버를 재시작하세요.', 409)
            key = payload.get('key')
            if not isinstance(key, str) or not 20 <= len(key) <= 512 or not key.startswith('sk-ant-') or not re.fullmatch(r'[A-Za-z0-9_-]+', key):
                raise APIError('INVALID_KEY_FORMAT', 'Claude 키 형식을 확인하세요.')
            self.adapter.key = key
            self.adapter.verified = False
            self.sealed = True
            return {'ok': True, 'present': True}
