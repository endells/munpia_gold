"""Owned-episode PC fallback; only info/timestamp/content endpoints are permitted."""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ASSETS = (
    ('official.mjs', 'https://cdn1.munpia.com/v2/pc-novel/20260930-170627/assets/novel_wasm-J15oHiwv.js', 'f54d4dcaea2fe94a0c6ae063ba5cd825b7fe25a3586825628d6b31460a946918'),
    ('official.wasm', 'https://cdn1.munpia.com/v2/pc-novel/20260930-170627/assets/novel_wasm_bg-CO3If8Lk.wasm', 'd2f48cb842d7cd73a27603459d5d43a070952562d1a24b9b98d06b1f6a2802ca'),
)


_ASSET_CACHE = {}

def parse_frames(data):
    frames, pos = [], 0
    while pos < len(data):
        if pos + 5 > len(data):
            raise ValueError('incomplete frame header')
        kind, size = data[pos], int.from_bytes(data[pos + 1:pos + 5], 'big')
        pos += 5
        if size > 8 * 1024 * 1024 or pos + size > len(data) or kind not in (1, 2, 3):
            raise ValueError('invalid frame')
        frames.append((kind, json.loads(data[pos:pos + size])))
        pos += size
    if not frames or frames[0][0] != 1 or frames[-1][0] != 3:
        raise ValueError('missing start/end frame')
    count = frames[0][1].get('totalChunks')
    chunks = [value for kind, value in frames[1:-1] if kind == 2]
    if (type(count) is not int or count <= 0 or len(chunks) != count or
            len(frames) != count + 2 or frames[-1][1].get('totalChunks') != count):
        raise ValueError('incomplete chunks')
    chunks.sort(key=lambda value: value['index'])
    indices = [value['index'] for value in chunks]
    if indices not in (list(range(count)), list(range(1, count + 1))):
        raise ValueError('missing or duplicate chunk index')
    if not isinstance(frames[0][1].get('publicKey'), str) or not all(isinstance(c.get('content'), str) for c in chunks):
        raise ValueError('invalid fields')
    return frames[0][1]['publicKey'], [value['content'] for value in chunks]


class Runtime:
    def __init__(self, client):
        from .core import MunpiaError
        self.client, self.proc, self.temp = client, None, None
        node = shutil.which('node') or shutil.which('nodejs')
        if not node:
            raise MunpiaError('PC 구매 본문에는 FF 컨테이너 내부의 Node.js 18 이상이 필요합니다. 호스트 PC의 Node.js와 별개입니다.')
        try:
            version = subprocess.run([node, '--version'], capture_output=True, timeout=5, check=True).stdout.decode().strip()
            if not re.fullmatch(r'v\d+\.\d+\.\d+', version) or int(version[1:].split('.')[0]) < 18:
                raise ValueError('old Node.js')
        except (OSError, subprocess.SubprocessError, ValueError):
            raise MunpiaError('FF 컨테이너 내부에 Node.js 18 이상을 설치해야 PC 본문을 처리할 수 있습니다.') from None
        self.temp = tempfile.TemporaryDirectory(prefix='munpia-pc-')
        try:
            for name, url, digest in ASSETS:
                client.check()
                # Fixed CDN assets only, no redirects and no authentication headers.
                data = _ASSET_CACHE.get(digest)
                if data is None:
                    with client.opener.open(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=25) as response:
                        data = response.read(4 * 1024 * 1024 + 1)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise MunpiaError('문피아 PC 모듈 버전이 달라 실행하지 않았습니다. 플러그인 업데이트가 필요합니다.')
                _ASSET_CACHE[digest] = data
                path = Path(self.temp.name) / name
                path.write_bytes(data)
                path.chmod(0o600)
            self.proc = subprocess.Popen([node, '--max-old-space-size=128', str(Path(__file__).with_suffix('.mjs')),
                                          str(Path(self.temp.name) / 'official.mjs'), str(Path(self.temp.name) / 'official.wasm')],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.buffer = bytearray()
            if self.read().get('ready') is not True:
                raise MunpiaError('문피아 PC 모듈을 시작하지 못했습니다.')
        except Exception:
            self.close()
            raise

    def read(self):
        from .core import MunpiaError
        deadline = time.monotonic() + 30
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            while True:
                self.client.check()
                if b'\n' in self.buffer:
                    line, _, rest = self.buffer.partition(b'\n')
                    self.buffer = bytearray(rest)
                    value = json.loads(line)
                    if not isinstance(value, dict) or value.get('error'):
                        raise MunpiaError('PC 본문 처리에 실패했습니다. 완료 파일로 기록하지 않았습니다.')
                    return value
                if time.monotonic() > deadline:
                    raise MunpiaError('PC 본문 처리 시간이 초과되었습니다.')
                if not selector.select(0.2):
                    continue
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise MunpiaError('PC 본문 처리 프로세스가 종료되었습니다.')
                self.buffer.extend(chunk)
                if len(self.buffer) > 8 * 1024 * 1024:
                    raise MunpiaError('PC 본문 처리 결과가 너무 큽니다.')

    def command(self, value):
        self.client.check()
        self.proc.stdin.write(json.dumps(value).encode() + b'\n')
        self.proc.stdin.flush()
        return self.read()

    def close(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=5)
            self.proc.stdin.close()
            self.proc.stdout.close()
            self.proc = None
        if self.temp is not None:
            self.temp.cleanup()
            self.temp = None


def request(client, nid, eid, path, payload=None):
    from .core import MunpiaError, cookie_header, response_error, parse_id
    nid = parse_id(nid)
    if not re.fullmatch(r'[1-9][0-9]*', str(eid)):
        raise MunpiaError('PC 회차 ID가 올바르지 않습니다.')
    prefix = '/api/v1/pc/novel-detail/%s/entries/%s/' % (nid, eid)
    if (path not in (prefix + 'info', prefix + 'content', '/api/v1/pc/novel-detail/entry-timestamp') or
            (payload is not None) != (path == prefix + 'content')):
        raise MunpiaError('허용되지 않은 PC 요청입니다.')
    grant_path = '/api/v1/mobile/novel-detail/%s/entries/%s' % (nid, eid)
    client.check_entry_access(grant_path)
    client.check()
    if client.stop.wait(max(0, client.last_request + client.delay - time.monotonic())):
        client.check()
    client.check_entry_access(grant_path)
    client.last_request = time.monotonic()
    cookies = client.cookie
    if cookies.startswith('['):
        cookies = json.dumps([c for c in json.loads(cookies) if c['domain'] == 'munpia.com' and not c['hostOnly']])
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.munpia.com/novel/viewer/%s/%s' % (nid, eid),
               'Accept': 'application/octet-stream' if payload is not None else 'application/json'}
    cookie = cookie_header(cookies, path)
    if cookie:
        headers['Cookie'] = cookie
    data = None
    if payload is not None:
        headers.update({'Content-Type': 'application/json', 'Origin': 'https://www.munpia.com'})
        data = json.dumps(payload).encode()
    req = urllib.request.Request('https://www.munpia.com' + path, headers=headers, data=data)
    try:
        with client.opener.open(req, timeout=25) as response:
            content_type = response.headers.get('Content-Type', '').lower()
            raw = response.read(8 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read(8192))
        except ValueError:
            error = {}
        finally:
            exc.close()
        raise response_error(error, exc.code) from None
    client.check()
    if len(raw) > 8 * 1024 * 1024:
        raise MunpiaError('PC 응답이 너무 큽니다.')
    if payload is not None and 'application/octet-stream' in content_type:
        return raw
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or parsed.get('code') != 'M000_00000':
        raise response_error(parsed)
    if payload is not None:
        raise MunpiaError('PC 서버가 본문 데이터를 반환하지 않았습니다.')
    if not isinstance(parsed.get('result'), dict):
        raise MunpiaError('PC 정보 응답 형식이 변경되었습니다.')
    return parsed['result']


def entry(client, nid, eid):
    from .core import MunpiaError, LoginRequired
    info = request(client, nid, eid, '/api/v1/pc/novel-detail/%s/entries/%s/info' % (nid, eid))
    if info.get('login') is not True:
        raise LoginRequired('PC 문피아 로그인이 확인되지 않습니다. PC에서도 사용할 수 있는 .munpia.com 쿠키가 필요합니다.')
    meta = info.get('entry') or {}
    if str(meta.get('id')) != str(eid) or str((info.get('novel') or {}).get('id')) != str(nid):
        raise MunpiaError('PC 작품/회차 정보가 일치하지 않습니다.')
    # Do not silently omit protected-renderer image placement in this initial adapter.
    if meta.get('attachments') or info.get('attachments'):
        raise MunpiaError('PC 본문의 삽화 배치는 아직 지원하지 않습니다. 누락 방지를 위해 이 회차를 저장하지 않았습니다.')
    runtime = Runtime(client)
    try:
        stamp = request(client, nid, eid, '/api/v1/pc/novel-detail/entry-timestamp').get('timestamp')
        if type(stamp) is not int:
            raise MunpiaError('PC 시간 응답이 올바르지 않습니다.')
        signed = runtime.command({'action': 'sign', 'timestamp': stamp})
        payload = {key: signed[key] for key in ('publicKey', 'salt', 'signature')}
        payload['timestamp'] = stamp
        data = request(client, nid, eid, '/api/v1/pc/novel-detail/%s/entries/%s/content' % (nid, eid), payload)
        try:
            server_key, chunks = parse_frames(data)
        except (ValueError, KeyError, TypeError):
            raise MunpiaError('PC 본문 조각이 누락되었거나 형식이 변경되었습니다. 저장하지 않았습니다.') from None
        result = runtime.command({'action': 'render', 'serverKey': server_key, 'salt': signed['salt'],
                                  'publicKey': signed['publicKey'], 'timestamp': stamp, 'entryId': str(eid), 'chunks': chunks})
        content = result.get('content')
        if not isinstance(content, str) or not content.strip():
            raise MunpiaError('PC 본문이 비어 있어 저장하지 않았습니다.')
        return dict(meta, content=content, attachments=[], _source='pc'), content
    finally:
        runtime.close()
