"""Локальный сервер жука: отдаёт dist/, хранит резервную копию данных, быстрые заметки и вложения."""
import json, os, re, shutil, subprocess, tempfile, threading, time, datetime, uuid
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
TRANS = os.path.join(DATA, 'transcripts')
MODEL = os.path.join(ROOT, 'models', 'ggml-large-v3-turbo-q5_0.bin')
WHISPER = shutil.which('whisper-cli') or '/opt/homebrew/bin/whisper-cli'
FFMPEG = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
os.makedirs(TRANS, exist_ok=True)
JOBS, JOB_LOCK = {}, threading.Lock()      # id вложения -> {'status','progress','error'}
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

def run_transcription(fid, lang):
    job = JOBS[fid]
    with JOB_LOCK:                                   # по одной за раз: нагрузка на процессор большая
        wav = tempfile.mktemp(suffix='.wav'); base = tempfile.mktemp(prefix='whisper-')
        try:
            job.update(status='running', progress=0)
            r = subprocess.run([FFMPEG, '-loglevel', 'error', '-y', '-i', os.path.join(FILES, fid), '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', wav], capture_output=True, text=True)
            if r.returncode: raise RuntimeError('Не удалось прочитать аудио: ' + r.stderr.strip()[-200:])
            p = subprocess.Popen([WHISPER, '-m', MODEL, '-f', wav, '-l', lang, '-pp', '-oj', '-of', base, '-sns'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
            for line in p.stderr:
                m = re.search(r'progress\s*=\s*(\d+)%', line)
                if m: job['progress'] = int(m.group(1))
            if p.wait(): raise RuntimeError('whisper завершился с ошибкой')
            j = json.load(open(base + '.json', encoding='utf-8'))
            segs = [{'start': round(x['offsets']['from'] / 1000, 1), 'end': round(x['offsets']['to'] / 1000, 1), 'text': x['text'].strip()} for x in j.get('transcription', []) if x['text'].strip()]
            out = {'id': fid, 'language': j.get('result', {}).get('language', ''), 'segments': segs, 'text': ' '.join(x['text'] for x in segs), 'created': datetime.datetime.now().isoformat(timespec='seconds')}
            atomic_write(os.path.join(TRANS, fid + '.json'), json.dumps(out, ensure_ascii=False).encode())
            job.update(status='done', progress=100); schedule_git_sync()
        except Exception as e:
            job.update(status='error', error=str(e)[:300])
        finally:
            for f in (wav, base + '.json'):
                try: os.remove(f)
                except OSError: pass

def transcript_list():
    done = {f[:-5] for f in os.listdir(TRANS) if f.endswith('.json')}
    out = {fid: {'status': 'done', 'progress': 100} for fid in done}
    for fid, j in JOBS.items():
        if j['status'] != 'done' or fid not in done: out[fid] = dict(j)
    return out

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=os.path.join(ROOT, 'dist'), **k)
    def log_message(self, *a): pass
    def ok_host(self): return self.headers.get('Host', '').lower() in allowed_hosts()
    def send_bytes(self, body, ctype='application/json', code=200, headers=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        for k, v in (headers or {}).items(): self.send_header(k, str(v))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p.startswith('/api/'):
            if not self.ok_host(): return self.send_error(403)
            if p == '/api/clips': return self.send_bytes(open(CLIPS, 'rb').read() if os.path.exists(CLIPS) else b'[]')
            if p == '/api/data':
                return self.send_bytes(open(FILE, 'rb').read(), headers={'X-Rev': get_rev(), 'Cache-Control': 'no-store'}) if os.path.exists(FILE) else self.send_error(404)
            if p == '/api/transcripts': return self.send_bytes(json.dumps(transcript_list()).encode(), headers={'Cache-Control': 'no-store'})
            if p.startswith('/api/transcript/') and ID.match(p[16:]):
                fp = os.path.join(TRANS, p[16:] + '.json')
                return self.send_bytes(open(fp, 'rb').read()) if os.path.exists(fp) else self.send_error(404)
            if p == '/api/rev': return self.send_bytes(json.dumps({'rev': get_rev()}).encode(), headers={'Cache-Control': 'no-store'})
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
        if self.path == '/api/transcribe' and self.ok_host() and self.headers.get('Content-Type') == 'application/json':
            try: req = json.loads(self.rfile.read(min(int(self.headers.get('Content-Length', 0)), 2000)))
            except ValueError: return self.send_error(400)
            fid, lang, force = str(req.get('id', '')), str(req.get('lang') or 'auto'), bool(req.get('force'))
            if not ID.match(fid) or not os.path.exists(os.path.join(FILES, fid)): return self.send_bytes(b'{"error":"file-missing"}', code=404)
            if not os.path.exists(MODEL) or not os.path.exists(WHISPER) or not os.path.exists(FFMPEG): return self.send_bytes(b'{"error":"tools-missing"}', code=500)
            if lang not in ('auto', 'en', 'ru', 'uk', 'fr', 'de', 'es', 'it'): lang = 'auto'
            tp = os.path.join(TRANS, fid + '.json')
            if JOBS.get(fid, {}).get('status') in ('queued', 'running'): return self.send_bytes(json.dumps(JOBS[fid]).encode(), code=202)
            if os.path.exists(tp) and not force: return self.send_bytes(b'{"status":"done"}')
            if os.path.exists(tp): os.remove(tp)
            JOBS[fid] = {'status': 'queued', 'progress': 0}
            threading.Thread(target=run_transcription, args=(fid, lang), daemon=True).start()
            return self.send_bytes(json.dumps(JOBS[fid]).encode(), code=202)
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
                      'body': ('«' + text + '»' + (' (с. ' + page + ')' if page else '') + '\n\n' if text else '') + ('Источник: ' + label + (', с. ' + page if page and not text else '') if url else ''), 'source': url, 'folder': str(c.get('folder') or 'inbox')[:80], 'section': (str(c.get('section'))[:20] if c.get('section') else None), 'kind': 'text',
                      'clip': True, 'demo': False, 'attachments': [], 'date': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
        atomic_write(CLIPS, json.dumps(clips, ensure_ascii=False, indent=1).encode()); schedule_git_sync()
        self.send_response(204); self.end_headers()

def pdf_watch_loop():
    """Раз в ~20 секунд просит наблюдателя PDF дописать новые выделения в заметки."""
    py, tool = os.path.join(ROOT, '.venv', 'bin', 'python'), os.path.join(ROOT, 'tools', 'pdf_watch.py')
    env = dict(os.environ, JOOKA_URL=f'http://127.0.0.1:{PORT}', JOOKA_DATA=DATA)
    if os.environ.get('JOOKA_PDF_ROOT'): env['JOOKA_PDF_ROOT'] = os.environ['JOOKA_PDF_ROOT']
    while True:
        time.sleep(20)
        if not (os.path.exists(py) and os.path.exists(tool)): continue
        try:
            r = subprocess.run([py, tool, 'scan'], env=env, capture_output=True, text=True, timeout=900)
            if r.stdout.strip() or r.returncode:
                with open(os.path.join(DATA, 'pdf_watch.log'), 'a', encoding='utf-8') as f:
                    f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M ') + (r.stdout.strip() or '') + (('\nERR ' + r.stderr.strip()[-300:]) if r.returncode else '') + '\n')
        except Exception as e:
            print('pdf watch:', e, flush=True)

if __name__ == '__main__':
    threading.Thread(target=pdf_watch_loop, daemon=True).start()
    schedule_git_sync(5)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
