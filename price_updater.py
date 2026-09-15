import json, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

BASE = "https://www.price4.co.uk/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
}

def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())

def get_models():
    """Read the app catalogue and keep current STUK models only.

    2025 Price4 model codes generally end in QTUK. They are deliberately
    excluded from Price Match Finder. Current 2026 models in the app use STUK.
    """
    html = Path("index.html").read_text(encoding="utf-8")
    m = re.search(r"const\s+TVs\s*=\s*\[(.*?)\];", html, re.S)
    if not m:
        raise RuntimeError("Could not find TVs catalogue in index.html")

    all_models = list(dict.fromkeys(
        re.findall(r'\[\s*["\']([^"\']+)["\']\s*,', m.group(1))
    ))

    current = []
    excluded = []
    for model in all_models:
        code = re.sub(r"\s+", "", model.upper())
        # Exclude 2025/QTUK model codes from price matching.
        if code.endswith("QTUK"):
            excluded.append(model)
        else:
            current.append(model)

    print(f"Price matching: {len(current)} current models; excluded {len(excluded)} QTUK/2025 models.")
    return current

def extract_product_code(href):
    m = re.search(r"/hisense-([^/]+)-\d+\.aspx$", href, re.I)
    if not m:
        return None
    return norm(m.group(1))

def collect_product_links(models):
    """Find the best exact Price4 product page for each current model."""
    wanted = {norm(m): m for m in models}
    found = {}

    for page in range(1, 11):
        url = BASE + "departments/televisions/brand/hisense" + ("" if page == 1 else f"/{page}")
        r = requests.get(url, headers=HEADERS, timeout=40)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for a in soup.find_all("a", href=True):
            href = a["href"]
            code = extract_product_code(href)
            if not code:
                continue

            # Only exact current-model matches. This prevents accidentally
            # matching 2025 QTUK or older N/K-series products to a 2026 STUK.
            original = wanted.get(code)
            if original:
                found[original] = urljoin(BASE, href)

        # Stop once every current model has been found.
        if len(found) == len(wanted):
            break
        time.sleep(0.5)

    return found

def money_values(text):
    return [
        float(v.replace(",", ""))
        for v in re.findall(r"£\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text or "")
    ]

def clean_store_name(text):
    text = re.sub(r"\s+", " ", text or "")
    text = text.replace("Image:", "").strip()
    text = re.sub(r"\s*\*+$", "", text).strip()
    return text

def parse_product(url):
    """Parse every retailer row from the Price4 product page."""
    r = requests.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    body = soup.get_text(" ", strip=True)
    last_updated = None
    m = re.search(
        r"Last updated:\s*([^|]+?)(?:\s+An asterisk|\s+ℹ️|\s+Comparing prices)",
        body
    )
    if m:
        last_updated = m.group(1).strip()

    retailers = {}

    # Price4's retailer table is: Store | Total | Visit Store.
    # Be deliberately tolerant: inspect every cell in each row and use the
    # first valid currency value after the store name.
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        texts = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in cells]
        store = clean_store_name(texts[0])

        if not store:
            continue
        if store.lower() in {"store", "total", "visit store"}:
            continue
        if len(store) > 80:
            continue

        prices = []
        for text in texts[1:]:
            prices.extend(money_values(text))

        if not prices:
            # Fallback: sometimes the row text is flattened differently.
            prices = money_values(" ".join(texts[1:]))

        if not prices:
            continue

        # Price4 displays the total price first; if a voucher price follows,
        # keep the first advertised total as the comparable retailer price.
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
            result["models"][model] = {
                "status": "not_found_on_price4"
            }
            continue

        try:
            prices, updated = parse_product(url)

            # Sort cheapest first so the app can easily identify the best match.
            prices = dict(sorted(prices.items(), key=lambda item: (item[1], item[0].lower())))

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
                "sourceUrl": url
            }

        time.sleep(0.35)

    Path("price-data.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    ok = sum(1 for v in result["models"].values() if v.get("status") == "ok")
    total_retailer_prices = sum(len(v.get("prices", {})) for v in result["models"].values())
    print(f"Updated {ok}/{len(models)} current models from Price4.")
    print(f"Collected {total_retailer_prices} retailer prices.")

if __name__ == "__main__":
    main()
