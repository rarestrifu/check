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
STATE_DIR = "state"

# IMPORTANT: nu ai nevoie de 80 pagini ca să găsești 25 produse sub prag
# în CI, multe request-uri => soft-block (200 + products: [])
MAX_PI = 12

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# Endpoint stabil (ca în workflow-ul tău bun)
API_BASE = "https://apigw.trendyol.com/discovery-sfint-search-service/api/search/products"

# Parametri “magici” care fac diferența (exact ca în workflow-ul tău bun)
API_EXTRA_PARAMS = {
    "culture": "ro-RO",
    "storefrontId": "29",
    "channelId": "1",
    "pathModel": "sr",
    "countryCode": "RO",
    "language": "ro",
}

# ================= EMAIL =================

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

EMAIL_USER = "bluegaming764@gmail.com"
EMAIL_TO = "bluegaming764@gmail.com"
EMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()

# ================= CATEGORIES =================

CATEGORIES = {
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
    },
    "jackets": {
        "listing": "https://www.trendyol.com/ro/sr?wc=118&wb=300%2C768%2C54%2C156%2C44%2C333%2C146279%2C33&wg=2&sst=PRICE_BY_ASC",
        "price_max": 140.0,
        "target": 25,
        "base_file": "jackets_base.json",
    },
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


def get_model_id(p):
    return p.get("contentId") or p.get("id") or p.get("groupId")


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


def accept_cookies(page):
    # best-effort (nu crăpăm dacă nu există)
    for sel in [
        "//button[contains(., 'Accept')]",
        "//button[contains(., 'Acceptați')]",
        "//button[contains(., 'Accept toate')]",
        "//button[contains(., 'Accept all')]",
    ]:
        try:
            page.locator(sel).click(timeout=2500)
            page.wait_for_timeout(250)
            return
        except Exception:
            pass


def build_api_url(listing_url: str, pi: int) -> str:
    parsed = urlparse(listing_url)
    qs = dict(parse_qsl(parsed.query))

    qs.update(API_EXTRA_PARAMS)
    qs["pi"] = str(pi)

    return API_BASE + "?" + urlencode(qs, doseq=True)


def fetch_json(page, url):
    # in-page fetch cu headers + credentials, ca în workflow-ul bun
    return page.evaluate(
        """async (u) => {
            let r;
            try {
              r = await fetch(u, {
                credentials: 'include',
                headers: {
                  'accept': 'application/json, text/plain, */*',
                  'accept-language': 'ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7',
                  'x-country-code': 'RO'
                }
              });
            } catch (e) {
              return { ok: false, status: -1, data: null, error: String(e) };
            }

            let data = null;
            try { data = await r.json(); } catch(e) {}

            return { ok: r.ok, status: r.status, data, error: '' };
        }""",
        url
    )

# ================= CORE =================

def collect_current(page, cfg):
    """
    Return: (results, status)
      status: "ok" | "http_error" | "empty"
    """
    seen, results = set(), []

    # Hard warm-up per category (stabil în CI)
    page.goto("https://www.trendyol.com/ro", timeout=120000, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    accept_cookies(page)
    page.wait_for_timeout(400)

    # Open listing + accept + reload (consent chiar intră în vigoare)
    page.goto(cfg["listing"], timeout=120000, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    accept_cookies(page)
    page.wait_for_timeout(400)
    try:
        page.reload(timeout=120000, wait_until="networkidle")
    except Exception:
        # uneori networkidle poate fi instabil; nu forțăm
        pass
    page.wait_for_timeout(800)

    any_products_seen = False
    over_max_streak = 0

    # Fetch incremental: luăm pagini până strângem target
    for pi in range(1, MAX_PI + 1):
        api_url = build_api_url(cfg["listing"], pi)
        res = fetch_json(page, api_url)

        if not res.get("ok"):
            page.screenshot(path=f"debug_http_{int(time.time())}.png", full_page=True)
            return results, "http_error"

        batch = extract_products(res.get("data"))
        if batch:
            any_products_seen = True
        else:
            break

        for pr in batch:
            if len(results) >= cfg["target"]:
                return results, "ok"

            pid = get_model_id(pr)
            if not pid or pid in seen:
                continue

            price = get_price(pr)
            if price is None:
                continue

            # IMPORTANT: nu ieșim la primul produs peste prag (poate fi random/promoted)
            if apply_code(price) > cfg["price_max"]:
                over_max_streak += 1
                # dacă am văzut multe peste prag și deja avem ceva, ne oprim
                if over_max_streak >= 35 and len(results) > 0:
                    return results, "ok"
                continue
            else:
                over_max_streak = 0

            seen.add(pid)
            u = normalize_url(pr.get("url", ""))
            results.append({
                "key": clean_url(u),
                "name": pr.get("name") or "",
                "url": u,
            })

        time.sleep(0.25)  # mic delay anti-rate-limit

    if not any_products_seen:
        page.screenshot(path=f"debug_empty_{int(time.time())}.png", full_page=True)
        return [], "empty"

    return results, "ok"


def load_base(filename):
    path = os.path.join(STATE_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    keys = set()
    if isinstance(data, list):
        for p in data:
            if isinstance(p, dict):
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
            ]
        )

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

            # Context NOU per categorie (elimină problema cu ordinea / sesiunea “stricată”)
            context = browser.new_context(
                user_agent=UA,
                locale="ro-RO",
                timezone_id="Europe/Bucharest",
                viewport={"width": 1366, "height": 768},
                device_scale_factor=1,
            )
            context.set_extra_http_headers({
                "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            })
            page = context.new_page()

            # colectare + retry (o singură dată)
            current, status = collect_current(page, cfg)
            if status != "ok" or len(current) == 0:
                time.sleep(2)
                context.clear_cookies()
                current, status = collect_current(page, cfg)

            # închidem contextul (reset total)
            context.close()

            # safety: 0 produse NU e “changed”
            if status != "ok" or len(current) == 0:
                send_email(
                    f"🟠 Trendyol {label}: ERROR ({status})",
                    f"Could not collect a valid current list for {label}.\n"
                    f"Status: {status}\n"
                    f"Items: {len(current)}\n"
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

                # dacă NU există produse noi, dar lipsesc unele din base, poți trimite un mail separat
                if not new_items:
                    send_email(
                        f"🟠 Trendyol {label}: CHANGED (no new items)",
                        f"List differs from base, but there are NO new items.\n"
                        f"Missing vs base: {missing_count}\n"
                        f"Current items checked: {len(current)}\n"
                        f"Listing: {cfg['listing']}"
                    )
                    continue

                lines = []
                lines.append(f"New items found for {label} (only new vs base): {len(new_items)}")
                lines.append(f"Missing vs base: {missing_count}")
                lines.append("")
                lines.append("NEW items:")
                for it in new_items:
                    lines.append(f"- {it['name']}\n  {it['url']}")

                send_email(
                    f"🟢 Trendyol {label}: NEW ({len(new_items)})",
                    "\n".join(lines)
                )


            time.sleep(2)  # mic cooldown între categorii

        browser.close()


if __name__ == "__main__":
    main()
