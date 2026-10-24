"""Локальный сервер жука: отдаёт dist/, хранит резервную копию данных, быстрые заметки и вложения."""
import json, os, re, shutil, subprocess, threading, time, datetime, uuid
from urllib.parse import unquote
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
FILE = os.path.join(DATA, 'jooka-data.json')
CLIPS = os.path.join(DATA, 'clips.json')
HIST = os.path.join(DATA, 'history')
FILES = os.path.join(DATA, 'files')
PORT = 4318
SNAP_EVERY = 600          # снимок перед записью не чаще раза в 10 минут
KEEP_RECENT, KEEP_DAYS = 30, 30
for d in (HIST, FILES): os.makedirs(d, exist_ok=True)
ID = re.compile(r'^[A-Za-z0-9-]{8,64}$')
REV = os.path.join(DATA, 'rev.txt')
HOSTS = os.path.join(DATA, 'allowed_hosts.txt')   # по одному имени в строке, например: mac.tailnet-name.ts.net

def get_rev():
    try: return int(open(REV).read().strip() or 0)
    except (OSError, ValueError): return 0

def allowed_hosts():
    base = {f'localhost:{PORT}', f'127.0.0.1:{PORT}'}
    try: base |= {l.strip().lower() for l in open(HOSTS, encoding='utf-8') if l.strip() and not l.startswith('#')}
    except OSError: pass
    return base


def atomic_write(path, raw):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f: f.write(raw)
    os.replace(tmp, path)

def snapshots():
    return sorted(f for f in os.listdir(HIST) if f.startswith('snapshot-') and f.endswith('.json'))

def snapshot_before_write():
    """Копия ТЕКУЩЕГО файла до перезаписи, чтобы откатиться к состоянию до ошибки."""
    if not os.path.exists(FILE): return
    snaps = snapshots()
    if snaps and time.time() - os.path.getmtime(os.path.join(HIST, snaps[-1])) < SNAP_EVERY: return
    shutil.copy(FILE, os.path.join(HIST, 'snapshot-' + datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S') + '.json'))
    snaps = snapshots()
    keep, seen = set(snaps[-KEEP_RECENT:]), set()
    for f in reversed(snaps):                      # плюс по одному снимку за каждый из последних дней
        day = f[9:19]
        if day not in seen and len(seen) < KEEP_DAYS: seen.add(day); keep.add(f)
    for f in snaps:
        if f not in keep: os.remove(os.path.join(HIST, f))

def count_materials(raw):
    try:
        d = json.loads(raw)
        return len(d.get('materials', [])) if isinstance(d, dict) else -1
    except ValueError: return -1

def count_file(path):
    try: return count_materials(open(path, 'rb').read())
    except OSError: return -1

# История версий: data/ это отдельный git-репозиторий. Если задан remote origin, копия уходит и на GitHub (приватный репозиторий).
WRITE_LOCK = threading.Lock()
GIT_LOCK, git_timer = threading.Lock(), None
def git(*args, timeout=90):
    return subprocess.run(['git', *args], cwd=DATA, capture_output=True, text=True, timeout=timeout)

def git_sync():
    with GIT_LOCK:
        try:
            if not os.path.isdir(os.path.join(DATA, '.git')):
                git('init', '-q', '-b', 'main'); git('config', 'user.name', 'jooka backup'); git('config', 'user.email', 'jooka@localhost')
                open(os.path.join(DATA, '.gitignore'), 'w').write('files/\nhistory/\n*.tmp\nserver.log\n')
            git('add', '-A')
            if git('diff', '--cached', '--quiet').returncode != 0:
                git('commit', '-q', '-m', 'auto ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))
            if git('remote').stdout.strip() and git('rev-list', '--count', '@{u}..HEAD').stdout.strip() != '0':
                git('push', '-q', '-u', 'origin', 'main') if git('rev-parse', '--abbrev-ref', '@{u}').returncode else git('push', '-q')
        except Exception as e:
            print('git sync:', e, flush=True)

def schedule_git_sync(delay=60):
    global git_timer
    if git_timer: git_timer.cancel()
    git_timer = threading.Timer(delay, git_sync); git_timer.daemon = True; git_timer.start()

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=os.path.join(ROOT, 'dist'), **k)
    def log_message(self, *a): pass
    def ok_host(self): return self.headers.get('Host', '').lower() in allowed_hosts()
    def send_bytes(self, body, ctype='application/json', code=200, headers=None):
        self.send_response(code); self.send_header('Content-Type', ctype)
        for k, v in (headers or {}).items(): self.send_header(k, str(v)); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p.startswith('/api/'):
            if not self.ok_host(): return self.send_error(403)
            if p == '/api/clips': return self.send_bytes(open(CLIPS, 'rb').read() if os.path.exists(CLIPS) else b'[]')
            if p == '/api/data':
                return self.send_bytes(open(FILE, 'rb').read(), headers={'X-Rev': get_rev(), 'Cache-Control': 'no-store'}) if os.path.exists(FILE) else self.send_error(404)
            if p == '/api/files': return self.send_bytes(json.dumps(os.listdir(FILES)).encode())
            if p.startswith('/api/file/') and ID.match(p[10:]):
                fp = os.path.join(FILES, p[10:])
                return self.send_bytes(open(fp, 'rb').read(), 'application/octet-stream') if os.path.exists(fp) else self.send_error(404)
            return self.send_error(404)
        return super().do_GET()

    def do_PUT(self):
        p = self.path
        if not (p.startswith('/api/file/') and ID.match(p[10:])) or not self.ok_host() or self.headers.get('Content-Type') != 'application/octet-stream': return self.send_error(403)
        n = int(self.headers.get('Content-Length', 0))
        if n > 300_000_000: return self.send_error(413)
        atomic_write(os.path.join(FILES, p[10:]), self.rfile.read(n))
        self.send_response(204); self.end_headers()

    def do_POST(self):
        if self.path not in ('/api/data', '/api/clip') or not self.ok_host() or self.headers.get('Content-Type') != 'application/json': return self.send_error(403)
        n = int(self.headers.get('Content-Length', 0))
        if n > 50_000_000 or (self.path == '/api/clip' and n > 100_000): return self.send_error(413)
        raw = self.rfile.read(n)
        if self.path == '/api/clip': return self.save_clip(raw)
        new = count_materials(raw)
        if new < 0: return self.send_error(400)
        old = count_file(FILE)
        if old >= 10 and new < old * 0.5 and self.headers.get('X-Jooka-Confirm') != '1':
            atomic_write(os.path.join(HIST, 'rejected-' + datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S') + '.json'), raw)
            return self.send_bytes(json.dumps({'old': old, 'new': new}).encode(), code=409)
        with WRITE_LOCK:
            base = self.headers.get('X-Base-Rev')
            if base is not None and base != str(get_rev()):          # другое устройство успело записать раньше
                return self.send_bytes(open(FILE, 'rb').read(), code=412, headers={'X-Rev': get_rev()})
            snapshot_before_write()
            atomic_write(FILE, raw); schedule_git_sync()
            rev = get_rev() + 1; atomic_write(REV, str(rev).encode())
        self.send_response(204); self.send_header('X-Rev', str(rev)); self.end_headers()

    def save_clip(self, raw):
        try:
            c = json.loads(raw); title = str(c.get('title') or '').strip()[:200]; url = str(c.get('url') or '')[:2000]; text = str(c.get('text') or '').strip()[:6000]
        except (ValueError, AttributeError): return self.send_error(400)
        if not url.startswith(('http://', 'https://', 'file://')): url = ''
        page = str(c.get('page') or '').strip()[:20]
        label = unquote(url.rsplit('/', 1)[-1]) if url.startswith('file://') else url
        clips = json.load(open(CLIPS, encoding='utf-8')) if os.path.exists(CLIPS) else []
        clips.append({'id': 'clip-' + uuid.uuid4().hex[:12], 'title': title or url or 'Быстрая заметка', 'text': text, 'page': page,
                      'body': ('«' + text + '»' + (' (с. ' + page + ')' if page else '') + '\n\n' if text else '') + ('Источник: ' + label + (', с. ' + page if page and not text else '') if url else ''), 'source': url, 'folder': 'inbox', 'kind': 'text',
                      'clip': True, 'demo': False, 'attachments': [], 'date': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
        atomic_write(CLIPS, json.dumps(clips, ensure_ascii=False, indent=1).encode()); schedule_git_sync()
        self.send_response(204); self.end_headers()

if __name__ == '__main__':
    schedule_git_sync(5)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
