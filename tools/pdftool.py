"""Small helper for reading the ACS PDFs: TOC dump, keyword search, page range."""
import sys, io, re
import pypdf

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

path = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else 'toc'
r = pypdf.PdfReader(path)
n = len(r.pages)


def text(i):
    return r.pages[i].extract_text() or ''


if mode == 'info':
    print(f'{path}: {n} pages')
    try:
        def walk(items, depth=0):
            for it in items:
                if isinstance(it, list):
                    walk(it, depth + 1)
                else:
                    pg = r.get_destination_page_number(it) + 1
                    print('  ' * depth + f'{it.title}  ... p{pg}')
        walk(r.outline)
    except Exception as e:
        print('no outline:', e)

elif mode == 'toc':
    lo = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    hi = int(sys.argv[4]) if len(sys.argv) > 4 else 20
    for i in range(lo - 1, min(hi, n)):
        print(f'--- PAGE {i+1} ---')
        print(text(i))

elif mode == 'find':
    pat = re.compile(sys.argv[3], re.I)
    ctx = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    for i in range(n):
        t = text(i)
        if pat.search(t):
            if ctx:
                print(f'=== PAGE {i+1} ===')
                print(t[:ctx])
            else:
                lines = [ln for ln in t.splitlines() if pat.search(ln)]
                print(f'p{i+1}: ' + ' | '.join(lines[:4]))

elif mode == 'range':
    lo = int(sys.argv[3]); hi = int(sys.argv[4]) if len(sys.argv) > 4 else lo + 4
    for i in range(lo - 1, min(hi, n)):
        print(f'--- PAGE {i+1} ---')
        print(text(i))
