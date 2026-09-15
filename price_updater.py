import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.price4.co.uk/"
RICHER_BASE = "https://www.richersounds.com/hisense-"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
TIMEOUT = 12


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def get_models():
    html = Path("index.html").read_text(encoding="utf-8")
    m = re.search(r"const\s+TVs\s*=\s*\[(.*?)\];", html, re.S)
    if not m:
        raise RuntimeError("Could not find TVs catalogue in index.html")

    raw = list(dict.fromkeys(
        re.findall(r'\[\s*["\']([^"\']+)["\']\s*,', m.group(1))
    ))

    # Price feed = current STUK generation only.
    # This deliberately keeps current STUK PRO models and removes old QTUK/Q models.
    models = []
    for model in raw:
        code = re.sub(r"\s+", "", model.upper())
        if code.endswith("QTUK"):
            continue
        if re.search(r"STUK(?:PRO)?$", code):
            models.append(model)
    return models


def get(url, timeout=TIMEOUT):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def extract_product_code(href):
    m = re.search(r"/hisense-([^/]+)-\d+\.aspx$", href, re.I)
    return norm(m.group(1)) if m else None


def collect_price4_links():
    links = {}
    # Price4 currently exposes the Hisense catalogue over several pages.
    # Fetch them concurrently so a slow page cannot make the whole job hang.
    urls = [BASE + "departments/televisions/brand/hisense"] + [
        BASE + f"departments/televisions/brand/hisense/{p}" for p in range(2, 11)
    ]

    def fetch(url):
        try:
            r = get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            found = {}
            for a in soup.find_all("a", href=True):
                code = extract_product_code(a["href"])
                if code:
                    found[code] = urljoin(BASE, a["href"])
            return found
        except Exception as exc:
            print(f"Price4 catalogue page skipped: {url} ({exc})")
            return {}

    with ThreadPoolExecutor(max_workers=5) as pool:
        for found in pool.map(fetch, urls):
            links.update(found)
    return links


def money_values(text):
    return [float(v.replace(",", "")) for v in re.findall(
        r"£\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text or ""
    )]


def retailer_from_cell(cell):
    for img in cell.find_all("img", alt=True):
        alt = re.sub(r"\s+", " ", img.get("alt", "")).strip()
        alt = re.sub(r"^Image:\s*", "", alt, flags=re.I).strip()
        if alt and alt.lower() != "image":
            return alt

    for tag in cell.find_all(["a", "span"], attrs={"title": True}):
        title = re.sub(r"\s+", " ", tag.get("title", "")).strip()
        if title:
            return title

    return re.sub(r"^Image:\s*", "", cell.get_text(" ", strip=True), flags=re.I).strip()


def parse_price4(url):
    r = get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    body = soup.get_text(" ", strip=True)

    updated = None
    m = re.search(
        r"Last updated:\s*([^|]+?)(?:\s+An asterisk|\s+ℹ️|\s+Comparing prices)",
        body,
    )
    if m:
        updated = m.group(1).strip()

    retailers = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        prices = []
        for cell in cells[1:]:
            prices.extend(money_values(cell.get_text(" ", strip=True)))
        if not prices:
            continue

        store = retailer_from_cell(cells[0])
        low = store.lower()
        if not store or low in {"store", "total", "visit store", "and", "from"}:
            continue
        if low.startswith("visit store") or "rrp" in low or len(store) > 80:
            continue

        retailers[store] = round(prices[0], 2)

    model_number = None
    mm = re.search(
        r"Model Number:\s*([A-Za-z0-9\s]+?)(?:\s+Screen Size|\s+Screen|\s+Product|\s+About|\s+Specifications|$)",
        body,
        re.I,
    )
    if mm:
        model_number = re.sub(r"\s+", " ", mm.group(1)).strip()

    return retailers, updated, model_number


def parse_richer(model):
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    url = RICHER_BASE + slug + "/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None, url

        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if norm(model) not in norm(text):
            return None, url

        # Product structured data is the safest source on Richer Sounds.
        prices = []
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or script.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            stack = data if isinstance(data, list) else [data]
            while stack:
                item = stack.pop()
                if isinstance(item, list):
                    stack.extend(item)
                elif isinstance(item, dict):
                    offers = item.get("offers")
                    if offers:
                        stack.extend(offers if isinstance(offers, list) else [offers])
                    value = item.get("price")
                    if isinstance(value, (int, float, str)):
                        try:
                            p = float(str(value).replace(",", ""))
                            if 100 <= p <= 20000:
                                prices.append(p)
                        except Exception:
                            pass

        if not prices:
            pos = text.lower().find(model.lower())
            window = text[pos:pos + 1800] if pos >= 0 else text[:1800]
            prices = [p for p in money_values(window) if 100 <= p <= 20000]

        return (round(min(prices), 2), url) if prices else (None, url)
    except Exception as exc:
        print(f"Richer skipped for {model}: {exc}")
        return None, url


def main():
    models = get_models()
    print(f"Checking {len(models)} current STUK models.")

    price4_links = collect_price4_links()
    wanted = {norm(m): m for m in models}
    links = {wanted[k]: v for k, v in price4_links.items() if k in wanted}

    # PRO models often share the Price4 URL of the base STUK listing.
    for model in models:
        if model in links:
            continue
        n = norm(model)
        if n.endswith("pro"):
            base = n[:-3]
            if base in price4_links:
                links[model] = price4_links[base]

    result = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "Price4 UK price comparison + Richer Sounds fallback",
        "models": {},
    }

    def process(model):
        prices = {}
        price4_updated = None
        model_number = None
        price4_url = links.get(model)
        richer_url = None

        if price4_url:
            try:
                prices, price4_updated, model_number = parse_price4(price4_url)
                # Never assign base-model Price4 prices to a PRO model unless
                # Price4 itself identifies the page as PRO.
                if re.search(r"PRO$", model, re.I):
                    if not model_number or "PRO" not in model_number.upper():
                        prices = {}
            except Exception as exc:
                print(f"Price4 skipped for {model}: {exc}")
                prices = {}

        # Only one lightweight fallback request per model. It is isolated and
        # cannot fail the whole job.
        richer_price, richer_url = parse_richer(model)
        if richer_price is not None:
            prices["Richer Sounds"] = richer_price

        prices = dict(sorted(prices.items(), key=lambda x: (x[1], x[0].lower())))
        best = None
        if prices:
            retailer, price = next(iter(prices.items()))
            best = {"retailer": retailer, "price": price}

        if prices:
            status = "ok"
        elif price4_url:
            status = "no_prices_found"
        else:
            status = "not_found_on_price4"

        return model, {
            "status": status,
            "bestPrice": best,
            "retailerCount": len(prices),
            "prices": prices,
            "price4Updated": price4_updated,
            "price4ModelNumber": model_number,
            "sourceUrl": price4_url or richer_url,
            "price4Url": price4_url,
            "richerUrl": richer_url,
        }

    # Keep the fallback requests parallel so the daily job stays comfortably
    # inside GitHub Actions' normal runtime even if a retailer is slow.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(process, model) for model in models]
        for future in as_completed(futures):
            model, data = future.result()
            result["models"][model] = data

    # Restore catalogue order for a stable JSON file.
    result["models"] = {m: result["models"].get(m, {"status": "error"}) for m in models}

    Path("price-data.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    ok = sum(v.get("status") == "ok" for v in result["models"].values())
    total = sum(len(v.get("prices", {})) for v in result["models"].values())
    pro_ok = sum(
        v.get("status") == "ok"
        for k, v in result["models"].items()
        if re.search(r"PRO$", k, re.I)
    )
    ur_ok = sum(
        v.get("status") == "ok"
        for k, v in result["models"].items()
        if re.search(r"UR[89]", k, re.I)
    )

    print(f"Updated {ok}/{len(models)} current models.")
    print(f"Collected {total} retailer prices.")
    print(f"PRO models with prices: {pro_ok}")
    print(f"UR8/UR9 models with prices: {ur_ok}")


if __name__ == "__main__":
    main()
