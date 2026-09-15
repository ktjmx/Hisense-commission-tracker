import json, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

BASE = 'https://www.price4.co.uk/'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; HisenseCommissionTracker/1.0; +https://github.com/)'}


def norm(s):
    return re.sub(r'[^a-z0-9]', '', str(s).lower())


def get_models():
    html = Path('index.html').read_text(encoding='utf-8')
    m = re.search(r'const\s+TVs\s*=\s*\[(.*?)\];', html, re.S)
    if not m:
        raise RuntimeError('Could not find TVs catalogue in index.html')
    return list(dict.fromkeys(re.findall(r'\[\s*[\"\']([^\"\']+)[\"\']\s*,', m.group(1))))


def collect_product_links(models):
    wanted = {norm(m): m for m in models}
    found = {}
    # Price4 currently paginates the Hisense television catalogue. Keep going until
    # a page adds nothing; the upper limit protects the daily job from runaway loops.
    empty_pages = 0
    for page in range(1, 11):
        url = BASE + 'departments/televisions/brand/hisense' + ('' if page == 1 else f'/{page}')
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        before = len(found)
        for a in soup.find_all('a', href=True):
            href = a['href']
            if not re.search(r'/hisense-[^/]+-\d+\.aspx$', href, re.I):
                continue
            label = a.get_text(' ', strip=True)
            slug = href.rsplit('/', 1)[-1].rsplit('.', 1)[0]
            slug = re.sub(r'-\d+$', '', slug)
            candidate = norm(slug)
            for nm, original in wanted.items():
                if nm == candidate or nm in candidate or candidate in nm:
                    found.setdefault(original, urljoin(BASE, href))
                    break
        if len(found) == before:
            empty_pages += 1
        else:
            empty_pages = 0
        if empty_pages >= 2:
            break
        time.sleep(0.4)
    return found


def parse_price(text):
    # Prefer a monetary value in the cell, ignoring stock counts and RRP text.
    vals = re.findall(r'£\s*([0-9][0-9,]*(?:\.\d{1,2})?)', text)
    if not vals:
        return None
    try:
        return float(vals[0].replace(',', ''))
    except ValueError:
        return None


def parse_product(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')
    retailers = {}
    last_updated = None
    body = soup.get_text(' ', strip=True)
    m = re.search(r'Last updated:\s*([^|]+?)(?:\s+An asterisk|\s+ℹ️|\s+Comparing prices)', body)
    if m:
        last_updated = m.group(1).strip()
    for tr in soup.find_all('tr'):
        cells = tr.find_all(['td','th'])
        if len(cells) < 2:
            continue
        store = cells[0].get_text(' ', strip=True).replace('Image:', '').strip()
        price_text = cells[1].get_text(' ', strip=True)
        price = parse_price(price_text)
        if not store or store.lower() in {'store', 'total', 'visit store'} or price is None:
            continue
        # Avoid accidental rows from unrelated page sections.
        if len(store) > 80:
            continue
        retailers[store] = price
    return retailers, last_updated


def main():
    models = get_models()
    links = collect_product_links(models)
    result = {'updatedAt': datetime.now(timezone.utc).isoformat(), 'source': 'Price4 UK price comparison', 'models': {}}
    for i, model in enumerate(models, 1):
        url = links.get(model)
        if not url:
            result['models'][model] = {'status': 'not_found_on_price4'}
            continue
        try:
            prices, updated = parse_product(url)
            result['models'][model] = {
                'status': 'ok' if prices else 'no_prices_found',
                'prices': prices,
                'price4Updated': updated,
                'sourceUrl': url,
            }
        except Exception as exc:
            result['models'][model] = {'status': 'error', 'error': str(exc), 'sourceUrl': url}
        time.sleep(0.35)
    Path('price-data.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    ok = sum(1 for v in result['models'].values() if v.get('status') == 'ok')
    print(f'Updated {ok}/{len(models)} models from Price4')

if __name__ == '__main__':
    main()
