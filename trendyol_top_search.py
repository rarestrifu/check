import json
import os
import time
import ssl
import smtplib
from email.message import EmailMessage
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from playwright.sync_api import sync_playwright

# ================= CONFIG =================

STATE_DIR = "state"
WELCOME_CODE_PERCENT = 30

MAX_PI = 6
MIN_ITEMS_OK = 2
RETRY_DELAYS = [0, 8, 20, 45]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

API_BASE = "https://apigw.trendyol.com/discovery-sfint-search-service/api/search/products"
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
    if not u:
        return ""
    p = urlparse(u)
    qs = dict(parse_qsl(p.query))
    qs.pop("boutiqueId", None)
    qs.pop("merchantId", None)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(qs, doseq=True), p.fragment))


def normalize_url(u: str) -> str:
    if not u:
        return ""
    if u.startswith("http"):
        return u
    return "https://www.trendyol.com" + u


def apply_code(price: float) -> float:
    return round(price * (1 - WELCOME_CODE_PERCENT / 100), 2)


def parse_price(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("Lei", "").replace(",", ".").replace(" ", ""))
    except Exception:
        return None


def get_price(p: dict):
    for v in (
        (p.get("recommendedRetailPrice") or {}).get("discountedPromotionPriceNumerized"),
        (p.get("price") or {}).get("discountedPrice"),
        (p.get("price") or {}).get("current"),
    ):
        val = parse_price(v)
        if val is not None:
            return val
    return None


def get_model_id(p: dict):
    return p.get("contentId") or p.get("id") or p.get("groupId")


def accept_cookies(page):
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
    qs = dict(parse_qsl(urlparse(listing_url).query))
    qs.update(API_EXTRA_PARAMS)
    qs["pi"] = str(pi)
    return API_BASE + "?" + urlencode(qs, doseq=True)


def fetch_products_page(page, listing_url: str, pi: int):
    api_url = build_api_url(listing_url, pi)
    js = r"""
    async (u) => {
      let resp;
      try {
        resp = await fetch(u, {
          credentials: "include",
          headers: {
            "accept": "application/json, text/plain, */*",
            "accept-language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            "x-country-code": "RO"
          }
        });
      } catch (e) {
        return { ok:false, status:-1, data:null, error:String(e) };
      }

      if (!resp.ok) {
        let txt = "";
        try { txt = await resp.text(); } catch(e) {}
        return { ok:false, status:resp.status, data:null, error:(txt||"").slice(0,300) };
      }

      let data = null;
      try { data = await resp.json(); } catch(e) {}
      return { ok:true, status:200, data, error:"" };
    }
    """
    return page.evaluate(js, api_url)


def load_base(filename: str) -> set:
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


def send_email(subject: str, body: str):
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

# ================= CORE =================

def collect_current(page, cfg):
    """
    Returns (items, status)
    status: ok | empty | http_error
    """
    page.goto("https://www.trendyol.com/ro", timeout=120000, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    accept_cookies(page)
    page.wait_for_timeout(400)

    page.goto(cfg["listing"], timeout=120000, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    accept_cookies(page)
    page.wait_for_timeout(400)
    try:
        page.reload(timeout=120000, wait_until="networkidle")
    except Exception:
        pass
    page.wait_for_timeout(700)

    seen = set()
    results = []
    any_products_seen = False
    over_max_streak = 0

    for pi in range(1, MAX_PI + 1):
        res = fetch_products_page(page, cfg["listing"], pi)
        if not res.get("ok"):
            page.screenshot(path=f"debug_http_{int(time.time())}.png", full_page=True)
            return [], "http_error"

        data = res.get("data") or {}
        batch = data.get("products", []) or []
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

            if apply_code(price) > cfg["price_max"]:
                over_max_streak += 1
                if over_max_streak >= 40 and len(results) > 0:
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

        time.sleep(0.5)

    if not any_products_seen:
        page.screenshot(path=f"debug_empty_{int(time.time())}.png", full_page=True)
        return [], "empty"

    return results, "ok"

# ================= MAIN =================

def main():
    os.makedirs(STATE_DIR, exist_ok=True)

    run_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    summary_lines = []
    all_new_items = []  # (label, item)
    blocked_labels = []
    ok_labels = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        for label, cfg in CATEGORIES.items():
            base_path = os.path.join(STATE_DIR, cfg["base_file"])
            if not os.path.exists(base_path):
                summary_lines.append(f"[{label}] BASE MISSING: {base_path}")
                blocked_labels.append(label)
                continue

            try:
                base_set = load_base(cfg["base_file"])
            except Exception as e:
                summary_lines.append(f"[{label}] BASE READ ERROR: {e}")
                blocked_labels.append(label)
                continue

            context = browser.new_context(
                user_agent=UA,
                locale="ro-RO",
                timezone_id="Europe/Bucharest",
                viewport={"width": 1366, "height": 768},
            )
            context.set_extra_http_headers({
                "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            })
            page = context.new_page()

            current = []
            status = "empty"

            for delay in RETRY_DELAYS:
                if delay:
                    time.sleep(delay)
                context.clear_cookies()
                current, status = collect_current(page, cfg)
                if status == "ok" and len(current) >= MIN_ITEMS_OK:
                    break

            context.close()

            if status != "ok" or len(current) < MIN_ITEMS_OK:
                summary_lines.append(f"[{label}] BLOCKED/EMPTY (status={status}, items={len(current)})")
                blocked_labels.append(label)
                continue

            ok_labels.append(label)

            current_set = {p["key"] for p in current}
            new_items = [p for p in current if p["key"] not in base_set]

            summary_lines.append(
                f"[{label}] OK items={len(current)} new={len(new_items)} missing_vs_base={len(base_set - current_set)}"
            )

            for it in new_items:
                all_new_items.append((label, it))

            time.sleep(2)

        browser.close()

    # ====== EMAIL ALWAYS (even if none found) ======
    lines = []
    lines.append(f"Trendyol run report @ {run_ts}")
    lines.append("")
    if summary_lines:
        lines.append("Summary:")
        lines.extend(summary_lines)
        lines.append("")

    if all_new_items:
        lines.append(f"NEW ITEMS TOTAL: {len(all_new_items)}")
        lines.append("")
        for label, it in all_new_items:
            lines.append(f"[{label}] {it['name']}\n  {it['url']}")
    else:
        lines.append("NO NEW ITEMS found this run.")

    subject_parts = []
    if all_new_items:
        subject_parts.append(f"NEW {len(all_new_items)}")
    else:
        subject_parts.append("NO NEW")

    if blocked_labels:
        subject_parts.append(f"BLOCKED {len(blocked_labels)}")

    subject = "🟢 Trendyol: " + " | ".join(subject_parts)

    send_email(subject, "\n".join(lines))


if __name__ == "__main__":
    main()
