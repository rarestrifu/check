import json
import os
import time
import ssl
import smtplib
import hashlib
import html as html_escape

from email.message import EmailMessage
from email.utils import make_msgid

from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from urllib.request import Request, urlopen

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

# max imagini inline (restul vor avea doar link)
MAX_INLINE_IMAGES = 12

# ================= CATEGORIES =================

CATEGORIES = {
    "boots": {
        "listing": "https://www.trendyol.com/en/sr?wc=1025&wb=369%2C768%2C156%2C300%2C101990%2C33%2C658%2C54%2C160%2C151014%2C44&wg=2&sst=PRICE_BY_ASC",
        "price_max": 150.0,
        "target": 25,
        "base_file": "boots_base.json",
    },
    "sneakers": {
        "listing": "https://www.trendyol.com/en/sr?wc=1172&wb=44%2C54%2C300%2C172588&wg=2&dcr=20&sst=PRICE_BY_ASC",
        "price_max": 140.0,
        "target": 25,
        "base_file": "sneakers_base.json",
    },
    "air_force": {
        "listing": "https://www.trendyol.com/en/sr?lc=1172&wb=44&qt=nike%20air%20force&st=nike%20air%20force&os=1&sst=PRICE_BY_ASC&q=air%20force",
        "price_max": 160.0,
        "target": 25,
        "base_file": "air_force_base.json",
    },
    "air_jordan": {
        "listing": "https://www.trendyol.com/en/sr?lc=1172&wb=44&qt=nike%20air%20jordan&st=nike%20air%20jordan&os=1&sst=PRICE_BY_ASC&q=air%20jordan",
        "price_max": 170.0,
        "target": 25,
        "base_file": "air_jordan_base.json",
    },
    "jackets": {
        "listing": "https://www.trendyol.com/en/sr?wc=118&wb=300%2C768%2C54%2C156%2C44%2C333%2C146279%2C33&wg=2&sst=PRICE_BY_ASC",
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

def fingerprint_new_items(all_new_items, n=6):
    if not all_new_items:
        return "NONE"
    keys = []
    for label, it in all_new_items:
        k = it.get("key") or clean_url(it.get("url", ""))
        if k:
            keys.append(f"{label}:{k}")
    keys.sort()
    blob = "\n".join(keys).encode("utf-8", errors="ignore")
    return hashlib.sha1(blob).hexdigest().upper()[:n]

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

def normalize_image_url(u: str):
    if not u:
        return None
    u = str(u).strip()
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    if u.startswith("/"):
        u = "https://www.trendyol.com" + u
    return u

def extract_image_url(p: dict):
    candidates = []
    for key in ("imageUrl", "thumbnailUrl", "image"):
        v = p.get(key)
        if isinstance(v, dict):
            v = v.get("url") or v.get("imageUrl") or v.get("path")
        if isinstance(v, str):
            candidates.append(v)

    imgs = p.get("images") or p.get("imageUrls") or []
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        if isinstance(first, dict):
            candidates.append(first.get("url") or first.get("imageUrl") or first.get("path"))
        elif isinstance(first, str):
            candidates.append(first)

    for c in candidates:
        u = normalize_image_url(c)
        if u and u.startswith("https://"):
            return u
    return ""

def download_image_bytes(url: str):
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=20) as r:
            data = r.read()
            ctype = r.headers.get_content_type()
            return data, ctype
    except Exception:
        return None, None

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

# ========= THE ONLY send_email (NEW SIGNATURE) =========

def send_email(subject: str, text_body: str, html_body: str, inline_images: list):
    if not EMAIL_PASSWORD:
        print("⚠ GMAIL_APP_PASSWORD missing in env/secrets")
        return

    msg = EmailMessage()
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(text_body)

    msg.add_alternative(html_body, subtype="html")
    html_part = msg.get_payload()[-1]

    for img in inline_images:
        html_part.add_related(
            img["data"],
            maintype=img["maintype"],
            subtype=img["subtype"],
            cid=img["cid"],
        )

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(EMAIL_USER, EMAIL_PASSWORD)
        s.send_message(msg)

    print("📧 Email sent:", subject)

# ================= CORE =================

def collect_current(page, cfg):
    page.goto("https://www.trendyol.com/ro", timeout=120000, wait_until="domcontentloaded")
    page.wait_for_timeout(900)
    accept_cookies(page)

    page.goto(cfg["listing"], timeout=120000, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    accept_cookies(page)

    seen = set()
    results = []

    stats = {
        "api_pages": 0,
        "batch_products": 0,
        "added": 0,
        "dup": 0,
        "price_none": 0,
        "over_max": 0,
    }

    any_batch = False
    over_max_streak = 0

    for pi in range(1, MAX_PI + 1):
        res = fetch_products_page(page, cfg["listing"], pi)
        if not res.get("ok"):
            return [], "http_error", {**stats, "http_status": res.get("status"), "error": res.get("error")}

        data = res.get("data") or {}
        batch = data.get("products", []) if isinstance(data, dict) else []
        stats["api_pages"] += 1

        if batch:
            any_batch = True
        else:
            break

        stats["batch_products"] += len(batch)

        for pr in batch:
            if len(results) >= cfg["target"]:
                return results, "ok", stats

            pid = pr.get("contentId") or pr.get("id") or pr.get("groupId")
            if not pid:
                continue
            if pid in seen:
                stats["dup"] += 1
                continue

            price = get_price(pr)
            if price is None:
                stats["price_none"] += 1
                continue

            if apply_code(price) > cfg["price_max"]:
                stats["over_max"] += 1
                over_max_streak += 1
                if over_max_streak >= 40 and len(results) > 0:
                    return results, "ok", stats
                continue
            else:
                over_max_streak = 0

            seen.add(pid)
            u = normalize_url(pr.get("url", ""))
            img = extract_image_url(pr)

            results.append({
                "key": clean_url(u),
                "name": pr.get("name") or "",
                "url": u,
                "image": img or "",
            })
            stats["added"] += 1

        time.sleep(0.4)

    if not any_batch:
        return [], "empty_api", stats
    if len(results) == 0:
        return [], "filtered_empty", stats
    return results, "ok", stats

# ================= MAIN =================

def main():
    os.makedirs(STATE_DIR, exist_ok=True)

    run_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    summary_lines = []
    all_new_items = []  # (label, item)
    blocked_labels = []

    # debug list
    if os.path.isdir(STATE_DIR):
        summary_lines.append(f"[DEBUG] state files: {sorted(os.listdir(STATE_DIR))}")
    else:
        summary_lines.append("[DEBUG] state dir missing")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])

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
            context.set_extra_http_headers({"Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7"})
            page = context.new_page()

            current = []
            status = "empty"
            stats = {}

            for delay in RETRY_DELAYS:
                if delay:
                    time.sleep(delay)
                context.clear_cookies()
                current, status, stats = collect_current(page, cfg)
                if status == "ok" and len(current) >= MIN_ITEMS_OK:
                    break

            context.close()

            if status != "ok" or len(current) < MIN_ITEMS_OK:
                summary_lines.append(
                    f"[{label}] {status} items={len(current)} | "
                    f"api_pages={stats.get('api_pages')} batch={stats.get('batch_products')} "
                    f"added={stats.get('added')} price_none={stats.get('price_none')} "
                    f"over_max={stats.get('over_max')} dup={stats.get('dup')} "
                    f"http={stats.get('http_status')} err={stats.get('error','')}"
                )
                blocked_labels.append(label)
                continue

            current_set = {p["key"] for p in current}
            new_items = [p for p in current if p["key"] not in base_set]
            missing_vs_base = len(base_set - current_set)

            summary_lines.append(
                f"[{label}] OK items={len(current)} new={len(new_items)} missing_vs_base={missing_vs_base} | "
                f"api_pages={stats.get('api_pages')} batch={stats.get('batch_products')} added={stats.get('added')}"
            )

            for it in new_items:
                all_new_items.append((label, it))

            time.sleep(1)

        browser.close()

    # ===== SUBJECT =====
    parts = [f"NEW {len(all_new_items)}" if all_new_items else "NO NEW"]
    if blocked_labels:
        parts.append(f"BLOCKED {len(blocked_labels)}")

    if all_new_items:
        dot, tag = "🟢", "[NEW]"
    elif blocked_labels:
        dot, tag = "🟠", "[BLOCKED]"
    else:
        dot, tag = "🔴", "[NO-NEW]"

    fp = fingerprint_new_items(all_new_items, n=6)
    subject = f"{dot} {tag} Trendyol: " + " | ".join(parts)
    if all_new_items:
        subject += f" #{fp}"

    # ===== TEXT BODY =====
    text_lines = [f"Trendyol run report @ {run_ts}", "", "Summary:"]
    text_lines.extend(summary_lines)
    text_lines.append("")

    if all_new_items:
        text_lines.append(f"NEW ITEMS TOTAL: {len(all_new_items)}\n")
        for label, it in all_new_items:
            text_lines.append(f"[{label}] {it['name']}\n  {it['url']}\n  IMG: {it.get('image','')}\n")
    else:
        text_lines.append("NO NEW ITEMS found this run.")

    # ===== HTML BODY + INLINE IMAGES =====
    html_lines = [
        "<h2>Trendyol run report</h2>",
        f"<div><b>Time:</b> {html_escape.escape(run_ts)}</div>",
        "<hr>",
        "<h3>Summary</h3>",
        "<pre style='white-space:pre-wrap'>",
        html_escape.escape("\n".join(summary_lines)),
        "</pre>",
        "<hr>",
    ]

    inline_images = []
    cid_map = {}
    shown = 0

    if all_new_items:
        html_lines.append(f"<h3>NEW ITEMS TOTAL: {len(all_new_items)}</h3>")
        for label, it in all_new_items:
            name = html_escape.escape(it.get("name", ""))
            url = html_escape.escape(it.get("url", ""), quote=True)
            img_url = (it.get("image") or "").strip()

            img_html = ""
            # inline doar primele MAX_INLINE_IMAGES
            if img_url and shown < MAX_INLINE_IMAGES:
                if img_url not in cid_map:
                    data, ctype = download_image_bytes(img_url)
                    if data and ctype and ctype.startswith("image/"):
                        maintype, subtype = ctype.split("/", 1)
                        cid = make_msgid()
                        cid_ref = cid[1:-1]
                        cid_map[img_url] = cid_ref
                        inline_images.append({
                            "cid": cid,
                            "data": data,
                            "maintype": maintype,
                            "subtype": subtype,
                        })
                if img_url in cid_map:
                    img_html = (
                        f"<img src='cid:{cid_map[img_url]}' "
                        f"style='width:160px;height:auto;border-radius:10px;display:block;margin:6px 0;'>"
                    )
                    shown += 1
            # pentru restul, arătăm doar link către imagine (dacă există)
            img_link = ""
            if img_url and not img_html:
                img_link = f"<div style='font-size:12px;opacity:0.75'><a href='{html_escape.escape(img_url, quote=True)}'>image link</a></div>"

            html_lines.append(
                f"""
                <div style="border:1px solid #ddd;border-radius:12px;padding:10px;margin:10px 0;">
                  <div style="font-size:12px;opacity:0.75">[{html_escape.escape(label)}]</div>
                  {img_html}
                  <div style="font-weight:700;margin-top:6px">{name}</div>
                  <div><a href="{url}">Open product</a></div>
                  {img_link}
                </div>
                """
            )
    else:
        html_lines.append("<h3>NO NEW ITEMS found this run.</h3>")

    send_email(
        subject=subject,
        text_body="\n".join(text_lines),
        html_body="\n".join(html_lines),
        inline_images=inline_images,
    )

if __name__ == "__main__":
    main()

