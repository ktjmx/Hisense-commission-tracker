import json, re, time
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
    # Price Match / daily feed is for the current STUK generation only.
    return [m for m in models if re.search(r"STUK(?:\s*PRO)?$", m, re.I)
            and not re.sub(r"\s+", "", m.upper()).endswith("QTUK")]


def extract_product_code(href):
    m = re.search(r"/hisense-([^/]+)-\d+\.aspx$", href, re.I)
    return norm(m.group(1)) if m else None


def collect_product_links(models):
    wanted = {norm(m): m for m in models}
    found = {}
    all_links = {}
    for page in range(1, 11):
        url = BASE + "departments/televisions/brand/hisense" + ("" if page == 1 else f"/{page}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=40)
            r.raise_for_status()
        except Exception:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            code = extract_product_code(a["href"])
            if not code:
                continue
            href = urljoin(BASE, a["href"])
            all_links[code] = href
            if code in wanted:
                found[wanted[code]] = href
        if len(found) == len(wanted):
            break
        time.sleep(0.3)

    # Price4 often uses the non-PRO URL even when its product page's model
    # number is actually the PRO model. Map each current PRO catalogue item
    # explicitly to its base STUK listing rather than dropping it.
    for model in models:
        if model in found:
            continue
        n = norm(model)
        if n.endswith("pro"):
            base = n[:-3]
            if base in all_links:
                found[model] = all_links[base]
    return found


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
        text = re.sub(r"\s+", " ", tag.get("title", "")).strip()
        if text:
            return text
    text = re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
    return re.sub(r"^Image:\s*", "", text, flags=re.I).strip()


def parse_price4(url):
    r = requests.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    body = soup.get_text(" ", strip=True)
    last_updated = None
    m = re.search(r"Last updated:\s*([^|]+?)(?:\s+An asterisk|\s+ℹ️|\s+Comparing prices)", body)
    if m:
        last_updated = m.group(1).strip()

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
    mm = re.search(r"Model Number:\s*([A-Za-z0-9\s]+?)(?:\s+Screen Size|\s+Screen|\s+Product|\s+About|\s+Specifications|$)", body, re.I)
    if mm:
        model_number = re.sub(r"\s+", " ", mm.group(1)).strip()
    return retailers, last_updated, model_number


def extract_jsonld_prices(soup):
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
                if isinstance(item.get("price"), (int, float, str)):
                    try:
                        p = float(str(item["price"]).replace(",", ""))
                        if 100 <= p <= 20000:
                            prices.append(p)
                    except Exception:
                        pass
    return prices


def parse_richer(model):
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    url = RICHER_BASE + slug + "/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=40)
        if r.status_code != 200:
            return None, None
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        # Confirm this really is the requested model, not a generic 404 page.
        if norm(model) not in norm(text):
            return None, None
        prices = extract_jsonld_prices(soup)
        if not prices:
            # Richer pages visibly repeat the current price near the product title.
            title_pos = text.lower().find(model.lower())
            window = text[title_pos:title_pos + 2500] if title_pos >= 0 else text[:2500]
            prices = money_values(window)
            prices = [p for p in prices if 100 <= p <= 20000]
        if not prices:
            return None, url
        # Prefer the first structured offer; dedupe and choose the lowest current
        # product price, avoiding finance amounts and tiny voucher/cashback values.
        price = min(prices)
        return round(price, 2), url
    except Exception:
        return None, url


def main():
    models = get_models()
    links = collect_product_links(models)
    result = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "Price4 UK price comparison + Richer Sounds fallback",
        "models": {}
    }

    for model in models:
        prices = {}
        source_url = None
        price4_updated = None
        model_number = None
        price4_url = links.get(model)
        if price4_url:
            try:
                prices, price4_updated, model_number = parse_price4(price4_url)
                # A PRO catalogue item may use the base Price4 URL. Only trust that
                # fallback when the Price4 page itself identifies the requested PRO
                # model number; otherwise do not mix base-model pricing into PRO.
                if " PRO" in model.upper() and model_number and "PRO" not in model_number.upper():
                    prices = {}
                source_url = price4_url
            except Exception:
                prices = {}

        # Always try Richer Sounds as a second source. This fills gaps where
        # Price4 has no listing, including current UR8/UR9 and U7S PRO models.
        richer_price, richer_url = parse_richer(model)
        if richer_price is not None:
            prices["Richer Sounds"] = richer_price
            if not source_url:
                source_url = richer_url

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

        result["models"][model] = {
            "status": status,
            "bestPrice": best,
            "retailerCount": len(prices),
            "prices": prices,
            "price4Updated": price4_updated,
            "price4ModelNumber": model_number,
            "sourceUrl": source_url,
            "price4Url": price4_url,
            "richerUrl": richer_url,
        }
        time.sleep(0.2)

    Path("price-data.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ok = sum(v.get("status") == "ok" for v in result["models"].values())
    total = sum(len(v.get("prices", {})) for v in result["models"].values())
    pro_ok = sum(v.get("status") == "ok" for k, v in result["models"].items() if " PRO" in k)
    ur_ok = sum(v.get("status") == "ok" for k, v in result["models"].items() if "UR8" in k or "UR9" in k)
    print(f"Updated {ok}/{len(models)} current models.")
    print(f"Collected {total} retailer prices.")
    print(f"PRO models with prices: {pro_ok}")
    print(f"UR8/UR9 models with prices: {ur_ok}")

if __name__ == "__main__":
    main()
