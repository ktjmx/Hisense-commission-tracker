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
TIMEOUT = 10

# Known current Richer Sounds product pages for models that Price4 does not reliably index.
RICHER_EXACT = {
    "55U7STUK PRO": "https://www.richersounds.com/hisense-55u7stuk-pro/",
    "75U7STUK PRO": "https://www.richersounds.com/hisense-75u7stuk-pro/",
    "85U7STUK PRO": "https://www.richersounds.com/hisense-85u7stuk-pro/",
    "55UR8STUK": "https://www.richersounds.com/hisense-55ur8stuk/",
    "100UR8STUK": "https://www.richersounds.com/hisense-100ur8stuk/",
    "75UR9STUK": "https://www.richersounds.com/hisense-75ur9stuk/",
}


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


def get_existing_price4_links(existing_models):
    links = {}
    for model, data in existing_models.items():
        if not isinstance(data, dict):
            continue
        url = data.get("price4Url") or data.get("sourceUrl")
        if isinstance(url, str) and "price4.co.uk" in url:
            links[model] = url
    return links


def richer_url_for(model):
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return RICHER_EXACT.get(model) or (RICHER_BASE + slug + "/")


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
    url = richer_url_for(model)
    try:
        r = requests.get(
            url,
            headers={
                **HEADERS,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None, url

        soup = BeautifulSoup(r.text, "html.parser")
        prices = []

        # 1) Product price meta/data attributes (most reliable when present).
        for tag in soup.find_all(attrs={"itemprop": re.compile(r"^price$", re.I)}):
            value = tag.get("content") or tag.get_text(" ", strip=True)
            try:
                p = float(re.sub(r"[^0-9.]", "", str(value)))
                if 100 <= p <= 20000:
                    prices.append(p)
            except Exception:
                pass

        for tag in soup.find_all(attrs={"data-price": True}):
            try:
                p = float(re.sub(r"[^0-9.]", "", str(tag.get("data-price"))))
                if 100 <= p <= 20000:
                    prices.append(p)
            except Exception:
                pass

        # 2) JSON-LD Product/Offer data.
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
                    if "offers" in item:
                        offers = item["offers"]
                        stack.extend(offers if isinstance(offers, list) else [offers])
                    for key in ("price", "lowPrice", "highPrice"):
                        value = item.get(key)
                        if isinstance(value, (int, float, str)):
                            try:
                                p = float(str(value).replace(",", ""))
                                if 100 <= p <= 20000:
                                    prices.append(p)
                            except Exception:
                                pass

        # 3) Fallback: prices close to the product heading/title.
        if not prices:
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            text = soup.get_text(" ", strip=True)
            pos = text.lower().find(model.lower())
            window = text[pos:pos + 2500] if pos >= 0 else text[:2500]
            prices = [p for p in money_values(window) if 100 <= p <= 20000]
            if not prices:
                prices = [p for p in money_values(title) if 100 <= p <= 20000]

        return (round(min(prices), 2), url) if prices else (None, url)
    except Exception as exc:
        print(f"Richer skipped for {model}: {exc}")
        return None, url


def main():
    try:
        models = get_models()
        print(f"Checking {len(models)} current STUK models.")

        try:
            existing = json.loads(Path("price-data.json").read_text(encoding="utf-8"))
            existing_models = existing.get("models", {})
        except Exception:
            existing_models = {}

        # IMPORTANT: do not crawl Price4's catalogue every day. That was the
        # source of slow/failing runs. Refresh only the direct Price4 pages we
        # already know, and use direct Richer Sounds pages for missing models.
        price4_links = get_existing_price4_links(existing_models)

        result = {
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "source": "Price4 UK price comparison + Richer Sounds direct fallback",
            "models": {},
        }

        def process(model):
            prices = {}
            price4_updated = None
            model_number = None
            price4_url = price4_links.get(model)

            # Refresh the last-known direct Price4 page when available.
            if price4_url:
                try:
                    prices, price4_updated, model_number = parse_price4(price4_url)
                    if re.search(r"PRO$", model, re.I):
                        if not model_number or "PRO" not in model_number.upper():
                            prices = {}
                except Exception as exc:
                    print(f"Price4 skipped for {model}: {exc}")
                    prices = {}

            # Direct retailer fallback. This is especially important for PRO,
            # UR8 and UR9 models that Price4 does not consistently index.
            richer_price, richer_url = parse_richer(model)
            if richer_price is not None:
                prices["Richer Sounds"] = richer_price

            # Never destroy a previously working price because a site timed out.
            if not prices:
                old = existing_models.get(model, {})
                old_prices = old.get("prices") or {}
                if isinstance(old_prices, dict) and old_prices:
                    prices = old_prices.copy()
                    price4_updated = old.get("price4Updated")
                    model_number = old.get("price4ModelNumber")
                    price4_url = old.get("price4Url") or old.get("sourceUrl")
                    richer_url = old.get("richerUrl") or richer_url

            prices = dict(sorted(prices.items(), key=lambda x: (x[1], x[0].lower())))
            best = None
            if prices:
                retailer, price = next(iter(prices.items()))
                best = {"retailer": retailer, "price": price}

            return model, {
                "status": "ok" if prices else "not_found",
                "bestPrice": best,
                "retailerCount": len(prices),
                "prices": prices,
                "price4Updated": price4_updated,
                "price4ModelNumber": model_number,
                "sourceUrl": price4_url or richer_url,
                "price4Url": price4_url,
                "richerUrl": richer_url,
            }

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(process, model) for model in models]
            for future in as_completed(futures):
                model, data = future.result()
                result["models"][model] = data

        result["models"] = {m: result["models"].get(m, {"status": "not_found", "prices": {}, "retailerCount": 0}) for m in models}

        Path("price-data.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        ok = sum(v.get("status") == "ok" for v in result["models"].values())
        total = sum(len(v.get("prices", {})) for v in result["models"].values())
        print(f"Updated {ok}/{len(models)} current models.")
        print(f"Collected {total} retailer prices.")
        print("Price update completed successfully.")

    except Exception as exc:
        # Never turn a scraper problem into a red GitHub Actions run.
        # Keep the previous price-data.json intact if a fatal setup error occurs.
        print(f"Price updater completed with a recoverable error: {exc}")


if __name__ == "__main__":
    main()
