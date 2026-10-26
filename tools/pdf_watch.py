#!/usr/bin/env python3
"""Следит за PDF и сам дописывает новые выделения в заметки жука (без участия человека).

Что наблюдаем:
  1. Папка ~/jooka-pdf. Подпапка задаёт, куда попадёт заметка:
       Входящие | Мой ресерч | Unit 6 - Ридинги | Unit 6 - Лекции | Unit 7 - Ридинги | Unit 7 - Лекции
     PDF прямо в корне ~/jooka-pdf идёт во Входящие.
  2. Отдельные файлы из data/pdf_watch.json: {"files": [{"path": ..., "title": ..., "offset": 0, "folder": "inbox", "section": ""}]}

Как работает: каждые ~20 секунд сервер жука запускает `scan`. Для PDF, изменённых с прошлого раза, берутся
выделения (highlight/underline), которых ещё не было, текст читается из PDF или распознаётся OCR (eng и rus),
цитата уходит в заметку этого PDF (одна заметка на файл, номер страницы по номеру на самой странице).

Команды:  scan            обработать всё, что изменилось
          adopt <pdf>     считать текущие выделения файла уже учтёнными (ничего не отправлять)
"""
import fitz, json, os, re, subprocess, sys, tempfile, time, urllib.request
from urllib.parse import quote
from PIL import Image

HOME = os.path.expanduser('~')
ROOT = os.environ.get('JOOKA_PDF_ROOT', os.path.join(HOME, 'jooka-pdf'))
DATA = os.environ.get('JOOKA_DATA', os.path.join(HOME, 'jooka-web', 'data'))
URL = os.environ.get('JOOKA_URL', 'http://localhost:4318')
TESS = os.environ.get('TESSERACT', '/opt/homebrew/bin/tesseract')
TESSDATA = os.path.join(HOME, 'jooka-web', 'models', 'tessdata')
MIN_CONF = 65
CONFIG, STATE = os.path.join(DATA, 'pdf_watch.json'), os.path.join(DATA, 'pdf_state.json')
ROUTES = {'входящие': ('inbox', ''), 'мой ресерч': ('research', ''),
          'unit 6 - ридинги': ('unit-6', 'readings'), 'unit 6 - лекции': ('unit-6', 'lectures'),
          'unit 7 - ридинги': ('unit-7', 'readings'), 'unit 7 - лекции': ('unit-7', 'lectures')}


def load(path, default):
    try: return json.load(open(path, encoding='utf-8'))
    except (OSError, ValueError): return default


def ocr(img, lang):
    """Возвращает (текст, средняя уверенность) для одной строки."""
    r = subprocess.run([TESS, img, '-', '--tessdata-dir', TESSDATA, '-l', lang, '--psm', '7', 'tsv'], capture_output=True, text=True)
    words, confs = [], []
    for row in r.stdout.splitlines()[1:]:
        c = row.split('\t')
        if len(c) >= 12 and c[0] == '5' and c[11].strip():
            words.append(c[11]); confs.append(float(c[10]))
    return ' '.join(words), (sum(confs) / len(confs) if confs else 0)


def read_clip(pg, rect, scale=3):
    text = pg.get_text('text', clip=rect).strip().replace('\n', ' ')
    if text: return text, 100
    f = tempfile.mktemp(suffix='.png')
    try:
        pg.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, colorspace=fitz.csGRAY).save(f)
        Image.open(f).point(lambda p: 255 if p > 150 else 0).save(f)        # убираем жёлтую заливку выделения
        best = max((ocr(f, lang) for lang in ('eng', 'rus')), key=lambda x: x[1])
        return best[0], best[1]
    finally:
        try: os.remove(f)
        except OSError: pass


def clean(t):
    t = re.sub(r'\s+', ' ', t).strip()
    t = re.sub(r'([a-zа-яё])- ([a-zа-яё])', r'\1\2', t)                 # перенос «com- plete»
    t = re.sub(r'(?<=[.,;:!?”"»)])[\d*†]{1,2}$', '', t).strip()          # хвост-сноска после кавычки
    t = re.sub(r'^\|\s*|\s*\|$', '', t)                                 # одиночная палка на краю строки
    t = re.sub(r'(?<=\s)\|(?=\s)', 'I', t)
    if t[:1] in '“"' and '”' not in t and t.count('"') < 2: t = t[1:].lstrip()   # одинокая открывающая кавычка от края выделения
    return t


def printed_page(pg, cache):
    """Номер страницы, напечатанный внизу листа (скан книги). Читаем по множеству разных обрезок и берём
    ответ, за который проголосовало большинство; если уверенности нет, возвращаем None."""
    n = pg.number
    if n in cache: return cache[n]
    r = pg.rect; votes = {}
    for y0 in (.86, .88, .89):
        for y1 in (.98, 1.0):
            for x0 in (.25, .35):
                for x1 in (.65, .75):
                    band = fitz.Rect(r.x0 + r.width * x0, r.y0 + r.height * y0, r.x0 + r.width * x1, r.y0 + r.height * y1)
                    f = tempfile.mktemp(suffix='.png')
                    try:
                        pg.get_pixmap(matrix=fitz.Matrix(3, 3), clip=band, colorspace=fitz.csGRAY).save(f)
                        out = subprocess.run([TESS, f, '-', '--tessdata-dir', TESSDATA, '-l', 'eng', '--psm', '7', '-c', 'tessedit_char_whitelist=0123456789'], capture_output=True, text=True).stdout.strip()
                    finally:
                        try: os.remove(f)
                        except OSError: pass
                    if out.isdigit() and 1 <= int(out) <= 3000: votes[int(out)] = votes.get(int(out), 0) + 1
    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    val = ranked[0][0] if ranked and ranked[0][1] >= 6 and (len(ranked) == 1 or ranked[0][1] >= 3 * ranked[1][1]) else None
    cache[n] = val
    return val


def highlights(doc):
    out = []
    for pg in doc:
        for a in pg.annots() or []:
            if a.type[1] not in ('Highlight', 'Underline'): continue
            r = a.rect
            out.append((pg.number, f'{pg.number}:{int(r.x0) // 3},{int(r.y0) // 3},{int(r.x1) // 3},{int(r.y1) // 3}', list(a.vertices), (r.y0, r.x0)))
    out.sort(key=lambda x: (x[0], x[3]))
    return out


def annotation_text(pg, v):
    lines = []
    for i in range(0, len(v), 4):
        q = fitz.Quad(v[i:i + 4]).rect; r = fitz.Rect(q.x0 - 4, q.y0 - 3, q.x1 + 4, q.y1 + 3)
        t, c = read_clip(pg, r)
        lines.append((round(q.y0), q.x0, t, c, max(1, len(t))))
    lines.sort()
    conf = sum(l[3] * l[4] for l in lines) / sum(l[4] for l in lines)
    return clean(' '.join(l[2] for l in lines)), conf


def post(payload):
    req = urllib.request.Request(URL + '/api/clip', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    return urllib.request.urlopen(req, timeout=20).status == 204


def targets():
    cfg = load(CONFIG, {})
    seen = {}
    for f in cfg.get('files', []):
        p = os.path.expanduser(f['path'])
        seen[p] = {'title': f.get('title'), 'offset': int(f.get('offset', 0)), 'folder': f.get('folder', 'inbox'), 'section': f.get('section', '')}
    if os.path.isdir(ROOT):
        for dp, dn, fn in os.walk(ROOT):
            dn[:] = [d for d in dn if not d.startswith('.')]
            for name in fn:
                if not name.lower().endswith('.pdf') or name.startswith('.'): continue
                p = os.path.join(dp, name)
                rel = os.path.relpath(dp, ROOT).split(os.sep)[0].lower()
                folder, section = ROUTES.get(rel, ('inbox', ''))
                seen.setdefault(p, {'title': None, 'offset': 0, 'folder': folder, 'section': section})
    return seen


def scan(only_adopt=None):
    state = load(STATE, {})
    for path, meta in targets().items():
        if only_adopt and os.path.abspath(only_adopt) != os.path.abspath(path): continue
        try: st = os.stat(path)
        except OSError: continue
        sig = [st.st_mtime, st.st_size]
        entry = state.get(path, {'sig': None, 'done': []})
        if entry['sig'] == sig and not only_adopt: continue
        if time.time() - st.st_mtime < 3: continue                     # файл ещё записывается
        try: doc = fitz.open(path)
        except Exception: continue
        hl = highlights(doc); done = set(entry['done']); ok = True; cache = {}
        title = meta['title'] or os.path.splitext(os.path.basename(path))[0]
        url = 'file://' + quote(os.path.abspath(path))
        for pno, key, verts, _ in hl:
            if key in done: continue
            if only_adopt: done.add(key); continue
            pg = doc[pno]
            text, conf = annotation_text(pg, verts)
            if text and conf < MIN_CONF:                                # скан читается плохо (фото под углом, рукопись, картинка)
                print(f'~ {os.path.basename(path)} с. {pno + 1}: выделение пропущено, текст не читается надёжно (уверенность {round(conf)})', flush=True)
                text = ''
            if text:
                page = printed_page(pg, cache)
                if page: entry['offset'] = page - (pno + 1)                 # запоминаем сдвиг «печатный номер минус номер в PDF»
                else: page = pno + 1 + entry.get('offset', meta['offset'])
                try: ok = post({'text': text, 'url': url, 'title': title, 'page': str(page), 'folder': meta['folder'], 'section': meta['section']})
                except Exception: ok = False
                if not ok: break
                print(f'+ {os.path.basename(path)} с. {page}: {text[:70]}', flush=True)
            done.add(key)
        if ok:
            state[path] = {'sig': sig, 'done': sorted(done), 'offset': entry.get('offset', meta['offset'])}
        else:
            state[path] = {'sig': None, 'done': sorted(done), 'offset': entry.get('offset', meta['offset'])}           # повторим в следующий раз
        os.makedirs(DATA, exist_ok=True)
        tmp = STATE + '.tmp'; json.dump(state, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False); os.replace(tmp, STATE)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'scan'
    if cmd == 'adopt': scan(only_adopt=sys.argv[2])
    else: scan()
