import json, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

BASE = "https://www.price4.co.uk/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())

def get_models():
    html = Path("index.html").read_text(encoding="utf-8")
    m = re.search(r"const\s+TVs\s*=\s*\[(.*?)\];", html, re.S)
    if not m:
        raise RuntimeError("Could not find TVs catalogue in index.html")
    models = list(dict.fromkeys(
        re.findall(r'\[\s*["\']([^"\']+)["\']\s*,', m.group(1))
    ))
    return [m for m in models if not re.sub(r"\s+", "", m.upper()).endswith("QTUK")]

def extract_product_code(href):
    m = re.search(r"/hisense-([^/]+)-\d+\.aspx$", href, re.I)
    return norm(m.group(1)) if m else None

def collect_product_links(models):
    wanted = {norm(m): m for m in models}
    found = {}
    for page in range(1, 11):
        url = BASE + "departments/televisions/brand/hisense" + ("" if page == 1 else f"/{page}")
        r = requests.get(url, headers=HEADERS, timeout=40)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            code = extract_product_code(a["href"])
            if code in wanted:
                found[wanted[code]] = urljoin(BASE, a["href"])
        if len(found) == len(wanted):
            break
        time.sleep(0.4)
    return found

def money_values(text):
    return [float(v.replace(",", "")) for v in re.findall(
        r"£\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text or ""
    )]

def retailer_from_cell(cell):
    # Price4 retailer cells normally contain an image whose alt is "Image: Retailer".
    for img in cell.find_all("img", alt=True):
        alt = re.sub(r"\s+", " ", img.get("alt", "")).strip()
        alt = re.sub(r"^Image:\s*", "", alt, flags=re.I).strip()
        if alt and alt.lower() not in {"image"}:
            return alt
    # Fallback to a meaningful link/title/aria label.
    for tag in cell.find_all(["a", "span"], attrs={"title": True}):
        text = re.sub(r"\s+", " ", tag.get("title", "")).strip()
        if text:
            return text
    text = re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
    text = re.sub(r"^Image:\s*", "", text, flags=re.I).strip()
    return text

def parse_product(url):
    r = requests.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    body = soup.get_text(" ", strip=True)

    last_updated = None
    m = re.search(r"Last updated:\s*([^|]+?)(?:\s+An asterisk|\s+ℹ️|\s+Comparing prices)", body)
    if m:
        last_updated = m.group(1).strip()

    retailers = {}
    # Only inspect rows belonging to the retailer comparison table.
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        row_text = " | ".join(re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in cells)
        prices = []
        for cell in cells[1:]:
            prices.extend(money_values(cell.get_text(" ", strip=True)))
        if not prices:
            prices = money_values(row_text)

        if not prices:
            continue

        store = retailer_from_cell(cells[0])
        if not store:
            continue
        low = store.lower()
        if low in {"store", "total", "visit store"}:
            continue
        if low.startswith("visit store"):
            continue
        # Reject summary/metadata text accidentally appearing as a row.
        if len(store) > 80 or "rrp" in low or low in {"and", "from"}:
            continue

        retailers[store] = round(prices[0], 2)

    return retailers, last_updated

def main():
    models = get_models()
    links = collect_product_links(models)
    result = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "Price4 UK price comparison",
        "models": {}
    }

    for model in models:
        url = links.get(model)
        if not url:
            result["models"][model] = {"status": "not_found_on_price4"}
            continue

        try:
            prices, updated = parse_product(url)
            prices = dict(sorted(prices.items(), key=lambda x: (x[1], x[0].lower())))
            best = None
            if prices:
                retailer, price = next(iter(prices.items()))
                best = {"retailer": retailer, "price": price}
            result["models"][model] = {
                "status": "ok" if prices else "no_prices_found",
                "bestPrice": best,
                "retailerCount": len(prices),
                "prices": prices,
                "price4Updated": updated,
                "sourceUrl": url,
            }
        except Exception as exc:
            result["models"][model] = {
                "status": "error",
                "error": str(exc),
                "sourceUrl": url,
            }
        time.sleep(0.25)

    Path("price-data.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ok = sum(v.get("status") == "ok" for v in result["models"].values())
    total = sum(len(v.get("prices", {})) for v in result["models"].values())
    print(f"Updated {ok}/{len(models)} current models.")
    print(f"Collected {total} retailer prices.")

if __name__ == "__main__":
    main()
