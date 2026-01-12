import json
import os
import time
import ssl
import smtplib
from email.message import EmailMessage
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, urlunparse
from playwright.sync_api import sync_playwright

# ================= CONFIG =================

WELCOME_CODE_PERCENT = 30
MAX_PI = 80
STATE_DIR = "state"

# Endpoint stabil (ca în workflow-ul tău care merge)
API_BASE = "https://apigw.trendyol.com/discovery-sfint-search-service/api/search/products"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# ================= EMAIL =================

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

EMAIL_USER = "bluegaming764@gmail.com"
EMAIL_TO = "bluegaming764@gmail.com"
EMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()

# ================= CATEGORIES =================

CATEGORIES = {
    "jackets": {
        "listing": "https://www.trendyol.com/ro/sr?wc=118&wb=300%2C768%2C54%2C156%2C44%2C333%2C146279%2C33&wg=2&sst=PRICE_BY_ASC",
        "price_max": 140.0,
        "target": 25,
        "base_file": "jackets_base.json",
    },
    "boots": {
        "listing": "https://www.trendyol.com/ro/sr?wc=1025&wb=369%2C300%2C156%2C44%2C33%2C101990%2C54%2C160%2C658%2C768&wg=2&sst=PRICE_BY_ASC",
        "price_max": 150.0,
        "target": 25,
        "base_file": "boots_base.json",
    },
    "sneakers": {
        "listing": "https://www.trendyol.com/ro/sr?wc=1172&wb=44%2C54%2C300%2C172588&wg=2&dcr=20&sst=PRICE_BY_ASC",
        "price_max": 140.0,
        "target": 25,
        "base_file": "sneakers_base.json",
    }
}

# ================= HELPERS =================

def clean_url(u: str) -> str:
    """Remove boutiqueId/merchantId so the same product compares equal."""
    if not u:
        return ""
    p = urlparse(u)
    qs = dict(parse_qsl(p.query))
    qs.pop("boutiqueId", None)
    qs.pop("merchantId", None)
    query = urlencode(qs, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, query, p.fragment))


def extract_products(payload):
    return payload.get("products", []) if isinstance(payload, dict) else []


def parse_price(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("Lei", "").replace(",", ".").replace(" ", ""))
    except Exception:
        return None


def get_price(p):
    for v in (
        (p.get("recommendedRetailPrice") or {}).get("discountedPromotionPriceNumerized"),
        (p.get("price") or {}).get("discountedPrice"),
        (p.get("price") or {}).get("current"),
    ):
        val = parse_price(v)
        if val is not None:
            return val
    return None


def apply_code(price):
    return round(price * (1 - WELCOME_CODE_PERCENT / 100), 2)


def normalize_url(u):
    if not u:
        return ""
    if u.startswith("http"):
        return u
    return "https://www.trendyol.com" + u


def get_model_id(p):
    return p.get("contentId") or p.get("id") or p.get("groupId")


def extract_query_params(url: str):
    p = urlparse(url)
    return dict(parse_qsl(p.query))


def accept_cookies(page):
    # best-effort: nu crăpa dacă nu există banner
    for sel in [
        "//button[contains(., 'Accept')]",
        "//button[contains(., 'Acceptați')]",
        "//button[contains(., 'Accept toate')]",
        "//button[contains(., 'Accept all')]",
    ]:
        try:
            page.locator(sel).click(timeout=2000)
            page.wait_for_timeout(200)
            return
        except Exception:
            pass


# ================= CORE =================

def collect_current(page, cfg):
    """
    Return: (results, status)
      status: "ok" | "http_error"
    """
    seen, results = set(), []

    # 0) hard warm-up per category (stabilizează session/consent)
    page.goto("https://www.trendyol.com/ro", timeout=120000, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    accept_cookies(page)
    page.wait_for_timeout(400)
    page.wait_for_load_state("networkidle")
    
    # 1) open listing + accept again + reload (consent chiar intră în vigoare)
    page.goto(cfg["listing"], timeout=120000, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    accept_cookies(page)
    page.wait_for_timeout(400)
    page.reload(timeout=120000, wait_until="networkidle")
    page.wait_for_timeout(800)

    base_params = extract_query_params(cfg["listing"])

    # 2) Fetch products via in-page fetch (credentials included), like your working workflow
    js = r"""
    async ({ apiBase, baseParams, maxPi }) => {
      const paramsBase = new URLSearchParams();
      Object.entries(baseParams).forEach(([k, v]) => {
        if (v != null) paramsBase.append(k, v);
      });

      async function fetchPage(pi) {
        const params = new URLSearchParams(paramsBase);
        params.set("pi", String(pi));

        // "magic" params that help avoid blocks / missing data
        params.set("culture", "ro-RO");
        params.set("storefrontId", "29");
        params.set("channelId", "1");
        params.set("pathModel", "sr");
        params.set("countryCode", "RO");
        params.set("language", "ro");

        const url = apiBase + "?" + params.toString();

        let resp;
        try {
          resp = await fetch(url, {
            credentials: "include",
            headers: {
              "accept": "application/json, text/plain, */*",
              "accept-language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
              "x-country-code": "RO"
            }
          });
        } catch (e) {
          return { ok: false, status: -1, error: String(e), products: [], hasNext: false };
        }

        if (!resp.ok) {
          let txt = "";
          try { txt = await resp.text(); } catch (e) {}
          return { ok: false, status: resp.status, error: (txt || "").slice(0, 300), products: [], hasNext: false };
        }

        const data = await resp.json();
        const products = data.products || [];
        const hasNext = !!(data._links && data._links.next);
        return { ok: true, status: 200, error: "", products, hasNext };
      }

      let all = [];
      for (let pi = 1; pi <= maxPi; pi++) {
        const r = await fetchPage(pi);
        if (!r.ok) return { ok: false, status: r.status, error: r.error, products: all };
        all = all.concat(r.products);
        if (!r.hasNext) break;
        await new Promise(res => setTimeout(res, 200));
      }

      return { ok: true, status: 200, error: "", products: all };
    }
    """

    api_result = page.evaluate(js, {"apiBase": API_BASE, "baseParams": base_params, "maxPi": MAX_PI})
    if not api_result.get("ok", False):
        # debug screenshot ca să vezi ce primește runner-ul
        page.screenshot(path=f"debug_api_{int(time.time())}.png", full_page=True)
        return [], "http_error"

    products = api_result.get("products", []) or []

    if not products:
        # retry after clearing cookies + warm-up
        page.context.clear_cookies()
        page.goto("https://www.trendyol.com/ro", timeout=120000, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        accept_cookies(page)
        page.wait_for_timeout(400)
    
        # re-run evaluate once
        api_result = page.evaluate(js, {"apiBase": API_BASE, "baseParams": base_params, "maxPi": MAX_PI})
        products = api_result.get("products", []) or []

    # 3) Apply your filtering logic (target + price ceiling)
    for pr in products:
        if len(results) >= cfg["target"]:
            return results, "ok"

        pid = get_model_id(pr)
        if not pid or pid in seen:
            continue

        price = get_price(pr)
        if price is None:
            continue

        if apply_code(price) > cfg["price_max"]:
            return results, "ok"

        seen.add(pid)
        u = normalize_url(pr.get("url", ""))
        results.append({
            "key": clean_url(u),
            "name": pr.get("name") or "",
            "url": u,
        })

    return results, "ok"


def load_base(filename):
    """
    Base file format: list of dicts containing at least 'url'
    We compare using cleaned url.
    """
    path = os.path.join(STATE_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    keys = set()
    if isinstance(data, list):
        for p in data:
            if not isinstance(p, dict):
                continue
            u = p.get("url", "")
            if u:
                keys.add(clean_url(u))
    return keys


def send_email(subject, body):
    msg = EmailMessage()
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    if not EMAIL_PASSWORD:
        print("⚠ Missing GMAIL_APP_PASSWORD env var (email not sent).")
        print("Subject would be:", subject)
        print(body)
        return

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(EMAIL_USER, EMAIL_PASSWORD)
        s.send_message(msg)


# ================= MAIN =================

def main():
    os.makedirs(STATE_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            user_agent=UA,
            locale="ro-RO",
            timezone_id="Europe/Bucharest",
            viewport={"width": 1366, "height": 768},
            device_scale_factor=1,
        )

        # extra headers (aproape ca workflow-ul tău bun)
        context.set_extra_http_headers({
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        page = context.new_page()

        # Warm-up
        page.goto("https://www.trendyol.com/ro", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        for label, cfg in CATEGORIES.items():
            base_path = os.path.join(STATE_DIR, cfg["base_file"])

            if not os.path.exists(base_path):
                send_email(
                    f"🟠 Trendyol {label}: BASE MISSING",
                    f"Base file is missing:\n{base_path}\n\n"
                    f"Put {cfg['base_file']} inside the repo under /state and commit it."
                )
                continue

            try:
                base_set = load_base(cfg["base_file"])
            except Exception as e:
                send_email(
                    f"🟠 Trendyol {label}: BASE READ ERROR",
                    f"Could not read base file:\n{base_path}\n\nError:\n{e}"
                )
                continue

            current, status = collect_current(page, cfg)

            # One retry if API blocked transiently
            if status != "ok":
                time.sleep(2)
                page.context.clear_cookies()
                page.goto("https://www.trendyol.com/ro", wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                current, status = collect_current(page, cfg)

            if status != "ok":
                send_email(
                    f"🟠 Trendyol {label}: ERROR ({status})",
                    f"Could not collect current list for {label}.\n"
                    f"Status: {status}\n"
                    f"Listing: {cfg['listing']}\n\n"
                    f"NOTE: this is NOT treated as a list change."
                )
                continue

            current_set = {p["key"] for p in current}

            if current_set == base_set:
                send_email(
                    f"🔴 Trendyol {label}: UNCHANGED",
                    f"The product list for {label} is identical to the base list.\n"
                    f"Items checked: {len(current)}"
                )
            else:
                new_items = [p for p in current if p["key"] not in base_set]
                missing_count = len(base_set - current_set)

                lines = []
                lines.append(f"The product list for {label} has changed.")
                lines.append(f"Current items: {len(current)}")
                lines.append(f"New vs base: {len(new_items)}")
                lines.append(f"Missing vs base: {missing_count}")
                lines.append("")
                lines.append("Current list:")
                for it in current:
                    lines.append(f"- {it['name']}\n  {it['url']}")

                send_email(
                    f"🟢 Trendyol {label}: CHANGED",
                    "\n".join(lines)
                )

        browser.close()


if __name__ == "__main__":
    main()


