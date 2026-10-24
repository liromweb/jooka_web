"""Достаёт выделения (highlight) из PDF, в том числе со сканов, и кладёт их в заметку жука.

Нужно один раз:  python3 -m venv .venv && .venv/bin/pip install pymupdf pillow   (и tesseract: brew install tesseract)
Читаем:  .venv/bin/python tools/pdf_highlights.py extract "/путь/файл.pdf"      -> JSON [{page,text}], текст из OCR, нужна вычитка
Кладём:  .venv/bin/python tools/pdf_highlights.py post "/путь/файл.pdf" "Название заметки" вычитанный.json [смещение_страниц] [папка] [раздел]
         папка, например unit-6; раздел: readings (Ридинги) или lectures (Мои лекции); по умолчанию заметка идёт во Входящие
Повторное добавление безопасно: жук не дублирует цитаты, которые уже есть в заметке.
"""
import fitz, json, os, subprocess, sys, tempfile, urllib.request
from urllib.parse import quote
from PIL import Image

def extract(pdf):
    out = []
    for pg in fitz.open(pdf):
        for a in pg.annots() or []:
            if a.type[1] not in ('Highlight', 'Underline'): continue
            v, lines = a.vertices, []
            for i in range(0, len(v), 4):
                r = fitz.Quad(v[i:i + 4]).rect; r = fitz.Rect(r.x0 - 4, r.y0 - 3, r.x1 + 4, r.y1 + 3)
                text = pg.get_text('text', clip=r).strip().replace('\n', ' ')
                if not text:   # скан без текстового слоя: OCR
                    f = tempfile.mktemp(suffix='.png'); pg.get_pixmap(matrix=fitz.Matrix(3, 3), clip=r, colorspace=fitz.csGRAY).save(f)
                    Image.open(f).point(lambda p: 255 if p > 150 else 0).save(f)
                    text = subprocess.run(['tesseract', f, '-', '--psm', '7', '-l', 'eng'], capture_output=True, text=True).stdout.strip(); os.remove(f)
                lines.append((r.y0, text))
            out.append({'page': pg.number + 1, 'text': ' '.join(t for _, t in sorted(lines))})
    return out

def post(pdf, title, items, offset=0, folder='', section=''):
    url = 'file://' + quote(os.path.abspath(pdf))
    for it in items:
        body = json.dumps({'text': it['text'], 'url': url, 'title': title, 'page': str(int(it['page']) + offset), 'folder': folder, 'section': section}).encode()
        urllib.request.urlopen(urllib.request.Request('http://localhost:4318/api/clip', data=body, headers={'Content-Type': 'application/json'}))
    print('добавлено', len(items))

if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'extract': print(json.dumps(extract(sys.argv[2]), ensure_ascii=False, indent=1))
    elif cmd == 'post': post(sys.argv[2], sys.argv[3], json.load(open(sys.argv[4], encoding='utf-8')), int(sys.argv[5]) if len(sys.argv) > 5 else 0, sys.argv[6] if len(sys.argv) > 6 else '', sys.argv[7] if len(sys.argv) > 7 else '')
