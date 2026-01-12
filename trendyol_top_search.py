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

API_SUBSTR = "discovery-sfint-search-service/api/search/products"

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


def set_query_param(url, key, value):
    p = urlparse(url)
    qs = parse_qs(p.query)
    qs[key] = [str(value)]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(qs, doseq=True), p.fragment))


def fetch_json(page, url):
    # in-page fetch with credentials => avoids direct 403 in many cases
    return page.evaluate(
        """async (u) => {
            const r = await fetch(u, { credentials: 'include' });
            let data = null;
            try { data = await r.json(); } catch(e) {}
            return { ok: r.ok, status: r.status, data };
        }""",
        url
    )


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
        p.get("recommendedRetailPrice", {}).get("discountedPromotionPriceNumerized"),
        p.get("price", {}).get("discountedPrice"),
        p.get("price", {}).get("current"),
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


# ================= CORE =================

def collect_current(page, cfg):
    """
    Collect current list for one category (up to target, stops when price_after_code > price_max).
    """
    state = {"template": None}
    seen, results = set(), []

    def on_response(resp):
        if API_SUBSTR in resp.url and state["template"] is None:
            try:
                if extract_products(resp.json()):
                    state["template"] = resp.url
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto(cfg["listing"], timeout=60000)
        page.wait_for_timeout(1200)

        # light scroll to trigger initial API response
        for _ in range(15):
            if state["template"]:
                break
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(400)

        if not state["template"]:
            return []

        for pi in range(1, MAX_PI + 1):
            res = fetch_json(page, set_query_param(state["template"], "pi", pi))
            if not res["ok"]:
                break

            batch = extract_products(res["data"])
            if not batch:
                break

            for pr in batch:
                if len(results) >= cfg["target"]:
                    return results

                pid = get_model_id(pr)
                if not pid or pid in seen:
                    continue

                price = get_price(pr)
                if price is None:
                    continue

                if apply_code(price) > cfg["price_max"]:
                    return results

                seen.add(pid)
                u = normalize_url(pr.get("url", ""))
                results.append({
                    "key": clean_url(u),
                    "name": pr.get("name") or "",
                    "url": u,
                })

            time.sleep(0.15)

        return results
    finally:
        # IMPORTANT: avoid accumulating handlers for each category
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


def load_base(filename):
    """
    Base file format: a list of dicts which must contain at least 'url'.
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
    if not EMAIL_PASSWORD:
        print("⚠ Missing GMAIL_APP_PASSWORD env var (email not sent).")
        print("Subject would be:", subject)
        return

    msg = EmailMessage()
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(EMAIL_USER, EMAIL_PASSWORD)
        s.send_message(msg)


# ================= MAIN =================

def main():
    os.makedirs(STATE_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        page = context.new_page()

        for label, cfg in CATEGORIES.items():
            base_path = os.path.join(STATE_DIR, cfg["base_file"])
            if not os.path.exists(base_path):
                send_email(
                    f"🟠 Trendyol {label}: BASE MISSING",
                    f"Base file is missing: {base_path}\n"
                    f"Create it and commit it to the repo."
                )
                continue

            try:
                base_set = load_base(cfg["base_file"])
            except Exception as e:
                send_email(
                    f"🟠 Trendyol {label}: BASE READ ERROR",
                    f"Could not read base file: {base_path}\nError: {e}"
                )
                continue

            current = collect_current(page, cfg)
            current_set = {p["key"] for p in current}

            if current_set == base_set:
                send_email(
                    f"🔴 Trendyol {label}: UNCHANGED",
                    f"The product list for {label} is identical to the base list.\n"
                    f"Items checked: {len(current)}"
                )
            else:
                # optional: show which keys are new/missing
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