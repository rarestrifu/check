import json
import os
import time
import ssl
import smtplib
import certifi
import html

from email.message import EmailMessage
from email.utils import make_msgid

from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from urllib.request import Request, urlopen

from collections import defaultdict

from playwright.sync_api import sync_playwright
from rich.progress import Progress, BarColumn, TimeElapsedColumn, TextColumn, TimeRemainingColumn
from rich.console import Console


# ========== CONFIG ==========

PRICE_THRESHOLD_DEFAULT = 110
PRICE_THRESHOLD_BOOTS = 140
PRICE_THRESHOLD_RUNNING_WALKING = 90
PRICE_THRESHOLD_SNEAKERS_PREMIUM = 130
PRICE_THRESHOLD_SNEAKERS_STANDARD = 100

MIN_DROP_PERCENT = 25.0

WELCOME_DISCOUNT_PERCENT = 30.0  
MIN_PRICE_LINK = 130

COOLDOWN_HOURS = 1
COOLDOWN_SECONDS = int(COOLDOWN_HOURS * 3600)

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")

EXCLUDED_KEYWORDS = {
    "șlapi",
    "fălapi",
    "slapi",
    "sandale",
    "sandala",
    "flip",
    "flip-flops",
    "papuci",
    "papuci de casa",
}

TEST_HITS_MODE = False
TEST_HITS_COUNT = 5

EMAIL_ENABLED = True
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

EMAIL_TO = "bluegaming764@gmail.com"
EMAIL_USER = "bluegaming764@gmail.com"
EMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
EMAIL_FROM = EMAIL_USER

API_BASE = "https://apigw.trendyol.com/discovery-sfint-search-service/api/search/products"

TRACKED_SIZES = {"41", "41.5", "42", "42.5", "43", "43.5", "44", "44.5", "45"}


# ============================================================
#  IMAGE HELPERS (CID INLINE)
# ============================================================

def normalize_image_url(u):
    if not u:
        return None
    u = str(u).strip()

    # //cdn... -> https://cdn...
    if u.startswith("//"):
        u = "https:" + u

    # http -> https
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]

    # path relativ -> absolut
    if u.startswith("/"):
        u = "https://www.trendyol.com" + u

    return u


def extract_image_url(p: dict):
    candidates = []

    for key in ("imageUrl", "image", "thumbnailUrl"):
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

    return None


def download_image_bytes(url: str):
    """
    Download imaginea ca bytes + content-type, pentru CID inline.
    """
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=20) as r:
            data = r.read()
            ctype = r.headers.get_content_type()  # ex: image/jpeg
            return data, ctype
    except Exception:
        return None, None


# ============================================================
#  BRAND / PRICE HELPERS
# ============================================================

def _ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)

def _cache_path(label: str) -> str:
    _ensure_state_dir()
    safe = "".join(c for c in label if c.isalnum() or c in ("_", "-")).strip()
    return os.path.join(STATE_DIR, f"sent_cache_{safe}.json")

def load_sent_cache(label: str) -> dict:
    path = _cache_path(label)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

def save_sent_cache(label: str, cache: dict) -> None:
    path = _cache_path(label)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def apply_cooldown_filter(hits: list, label: str) -> list:
    """
    Trimite un produs doar dacă:
    - nu a mai fost trimis niciodată
    - SAU au trecut >= COOLDOWN_SECONDS
    - SAU s-a schimbat prețul new_price față de ultima trimitere
    """
    if not hits:
        return hits

    now = int(time.time())
    cache = load_sent_cache(label)

    filtered = []
    for it in hits:
        key = str(it.get("model_id") or it.get("url") or it.get("name"))
        prev = cache.get(key)

        last_ts = int(prev.get("ts", 0)) if isinstance(prev, dict) else 0
        last_price = float(prev.get("new_price", -1)) if isinstance(prev, dict) else -1

        price_now = float(it.get("new_price", -1))

        should_send = (
            not prev
            or (now - last_ts) >= COOLDOWN_SECONDS
            or abs(price_now - last_price) >= 0.01
        )

        if should_send:
            filtered.append(it)
            cache[key] = {"ts": now, "new_price": price_now}

    # curățare: șterge intrări foarte vechi (ex: > 14 zile) ca să nu crească la infinit
    cutoff = now - 14 * 24 * 3600
    cache = {k: v for k, v in cache.items() if isinstance(v, dict) and int(v.get("ts", 0)) >= cutoff}

    save_sent_cache(label, cache)

    if len(filtered) != len(hits):
        print(f"🕒 Cooldown: {len(hits) - len(filtered)} item(s) skipped in [{label}]")

    return filtered

def extract_brand(p: dict, url: str = ""):
    for key in ("brand", "brandName"):
        b = p.get(key)
        if b:
            return str(b).strip().title()

    if url:
        parts = url.split("/")
        if len(parts) > 3:
            return parts[3].replace("-", " ").title()

    return "Unknown"


def parse_price_value(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)

    s = str(v)
    s = s.replace("Lei", "").strip()
    s = s.replace(" ", "")

    if "," in s and "." in s:
        s = s.replace(".", "")
        s = s.replace(",", ".")
    else:
        s = s.replace(",", ".")

    try:
        return float(s)
    except Exception:
        return None


def get_effective_price(p: dict):
    rrp = p.get("recommendedRetailPrice") or {}
    pb = p.get("price") or {}
    sp = p.get("singlePrice") or {}
    bp = p.get("binaryPrice") or {}

    candidates = []

    candidates.append(rrp.get("discountedPromotionPriceNumerized"))
    candidates.append(pb.get("discountedPrice"))
    candidates.append(parse_price_value(sp.get("salePriceWihoutCurrency") or sp.get("salePrice")))
    candidates.append(parse_price_value(bp.get("salePriceWihoutCurrency") or bp.get("salePrice")))

    candidates.append(rrp.get("sellingPriceNumerized"))
    candidates.append(pb.get("current"))

    for c in candidates:
        if c is None:
            continue
        val = parse_price_value(c)
        if val is not None:
            return val

    return None


def get_model_id(p: dict):
    return p.get("contentId") or p.get("id") or p.get("groupId")


def normalize_size(size_raw):
    if not size_raw:
        return None
    s = str(size_raw).strip()
    s = s.replace(",", ".")
    return s


def build_size_url(base_url: str, size_param: str):
    if not size_param:
        return base_url
    if "?" in base_url:
        return f"{base_url}&v={size_param}"
    else:
        return f"{base_url}?v={size_param}"


def clean_product_url(url: str):
    if not url:
        return url

    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query))

    for key in ["merchantId", "boutiqueId"]:
        if key in params:
            del params[key]

    new_query = urlencode(params)
    new_parts = list(parsed)
    new_parts[4] = new_query
    return urlunparse(new_parts)


# ============================================================
#  EMAIL (HTML + CID images)
# ============================================================

def send_email(hits, label, price_threshold):
    if not EMAIL_ENABLED or not hits:
        return
    if not EMAIL_PASSWORD:
        print("⚠ EMAIL password missing (set GMAIL_APP_PASSWORD env var)")
        return

    subject = f"🟢 Trendyol drops under {price_threshold} Lei [{label}]"

    # -------- plain text fallback --------
    hits_by_brand = defaultdict(list)
    for item in hits:
        hits_by_brand[item["brand"]].append(item)

    for brand in hits_by_brand:
        hits_by_brand[brand].sort(key=lambda x: x["new_price"])

    text_lines = [f"Big drops in category {label} (grouped by brand)\n"]
    for brand, items in hits_by_brand.items():
        text_lines.append(f"\n=== {brand.upper()} ===\n")
        for it in items:
            text_lines.append(
                f"- {it['name']}\n"
                f"  NEW: {it['new_price']} Lei | OLD: {it['old_price']} Lei\n"
                f"  DROP: {it['drop_amount']} Lei ({it['drop_percent']}%)\n"
                f"  URL: {it['url']}\n"
            )
    plain_text = "\n".join(text_lines)

    # -------- HTML + inline images (CID) --------
    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(plain_text)

    html_lines = [f"<h2>Big drops in category {html.escape(label)}</h2>"]
    attachments = []  # (cid, bytes, maintype, subtype)

    for brand, items in hits_by_brand.items():
        html_lines.append(f"<h3>{html.escape(brand.upper())}</h3>")

        for item in items:
            img_html = ""
            img_url = item.get("image")

            # încercăm să o includem inline (CID)
            if isinstance(img_url, str) and img_url.startswith("https://"):
                img_bytes, ctype = download_image_bytes(img_url)
                if img_bytes and ctype and ctype.startswith("image/"):
                    maintype, subtype = ctype.split("/", 1)
                    cid = make_msgid()  # '<...@...>'
                    cid_ref = cid[1:-1]
                    attachments.append((cid, img_bytes, maintype, subtype))

                    img_html = (
                        f"<img src=\"cid:{cid_ref}\" "
                        f"style=\"width:150px;display:block;margin-bottom:6px;\">"
                    )

            name_html = html.escape(item["name"])
            url_html = html.escape(item["url"], quote=True)

            html_lines.append(
                f"""
                <div style="margin-bottom:20px;">
                    {img_html}
                    <strong>{name_html}</strong><br>
                    <b>NEW:</b> {item['new_price']} Lei &nbsp;
                    <b>OLD:</b> {item['old_price']} Lei<br>
                    <b>DROP:</b> {item['drop_amount']} Lei ({item['drop_percent']}%)<br>
                    <a href="{url_html}">Open product</a>
                </div>
                """
            )

    msg.add_alternative("\n".join(html_lines), subtype="html")
    html_part = msg.get_payload()[-1] 

    for cid, data, maintype, subtype in attachments:
        html_part.add_related(data, maintype=maintype, subtype=subtype, cid=cid)

    ctx = ssl.create_default_context(cafile=certifi.where())
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls(context=ctx)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)

    print(f"📧 Email sent for category [{label}] (items={len(hits)})")


# ============================================================
#  UTILS
# ============================================================

def format_duration(sec):
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def extract_query_params(url: str):
    parsed = urlparse(url)
    return dict(parse_qsl(parsed.query))


def accept_cookies(page):
    for sel in [
        "//button[contains(., 'Accept')]",
        "//button[contains(., 'Acceptați')]",
        "//button[contains(., 'Accept toate')]",
        "//button[contains(., 'Accept all')]",
    ]:
        try:
            page.locator(sel).click(timeout=2000)
            time.sleep(0.2)
            return
        except Exception:
            pass


# ============================================================
#  SUPER-FAST PLAYWRIGHT FETCH (parallel 3×)
# ============================================================

def fetch_new_products_via_page_fetch(page, listing_url: str):
    params_base = extract_query_params(listing_url)

    page.goto(listing_url, timeout=120000, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    accept_cookies(page)
    page.wait_for_timeout(800)

    js = r"""
    async ({ apiBase, baseParams }) => {
      const paramsBase = new URLSearchParams();
      Object.entries(baseParams).forEach(([k, v]) => {
        if (v != null) paramsBase.append(k, v);
      });
    
      async function fetchPage(pi) {
        const params = new URLSearchParams(paramsBase);
        params.set("pi", String(pi));
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
              "x-country-code": "RO",
            }
          });
        } catch (e) {
          // aici e cazul tău: Failed to fetch
          return { products: [], next: false, status: -1, error: String(e) };
        }
    
        if (!resp.ok) {
          let txt = "";
          try { txt = await resp.text(); } catch (e) {}
          return { products: [], next: false, status: resp.status, error: (txt || "").slice(0, 300) };
        }
    
        const data = await resp.json();
        const arr = data.products || [];
        const hasNext = !!(data._links && data._links.next);
    
        return { products: arr, next: hasNext, status: 200, error: "" };
      }
    
      let all = [];
      let pageIndex = 1;
    
      while (true) {
        const b = await fetchPage(pageIndex);
    
        if (b.status !== 200) {
          return { status: b.status, error: b.error || "", products: all };
        }
    
        all = all.concat(b.products);
    
        if (!b.next) break;
    
        pageIndex += 1;
        if (pageIndex > 200) break;
    
        // mic delay anti-rate-limit
        await new Promise(r => setTimeout(r, 200));
      }
    
      return { status: 200, error: "", products: all };
    }
    """


    result = page.evaluate(js, {"apiBase": API_BASE, "baseParams": params_base})
    status = result.get("status", 200)
    if status != 200:
        print(f"[FAST API ERROR] status={status} url={listing_url}")
        print(f"[FAST API ERROR] snippet={result.get('error','')}")
        raise RuntimeError(f"API blocked/failed (status={status})")

    products = result.get("products", [])

    print(f"[FAST API] {listing_url} → {len(products)} products")
    return products


# ============================================================
#  SINGLE CATEGORY
# ============================================================

def main_single(products_file, listing_url, label, price_threshold, progress=None, page=None):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, products_file)

    with open(path, "r", encoding="utf-8") as f:
        old_products = json.load(f)

    old_products_list = [p for p in old_products if get_model_id(p)]
    total = len(old_products_list)

    task_id = None
    if progress:
        task_id = progress.add_task(f"{label}", total=total)

    start = time.perf_counter()

    new_products = fetch_new_products_via_page_fetch(page, listing_url)

    # model_id -> cea mai ieftină variantă din listingul NOU
    new_best_by_model = {}

    for p in new_products:
        model_id = get_model_id(p)
        if not model_id:
            continue

        price = get_effective_price(p)
        if price is None:
            continue

        current = new_best_by_model.get(model_id)
        if (not current) or (price < current["price"]):
            new_best_by_model[model_id] = {"product": p, "price": price}

    hits = []
    results = []
    missing_products = []

    for idx, old_p in enumerate(old_products_list, start=1):
        if progress and task_id is not None:
            progress.update(task_id, completed=idx)

        model_id = get_model_id(old_p)

        display_name = old_p.get("name", "(no name)")
        name_lc = display_name.lower()

        # EXCLUDE slapi / sandale
        if any(k in name_lc for k in EXCLUDED_KEYWORDS):
            continue

        url = old_p.get("url", "")
        if url.startswith("/"):
            url = "https://www.trendyol.com" + url

        url = clean_product_url(url)

        new_entry = new_best_by_model.get(model_id)
        if not new_entry:
            missing_products.append({"key": model_id, "name": display_name, "url": url})
            continue

        new_p = new_entry["product"]

        # imagine din NEW product (și normalizată)
        image_url = extract_image_url(new_p)

        old_price = get_effective_price(old_p)
        new_price_raw = new_entry["price"]

        # aplicăm cod bun venit (-30%)
        new_price = round(new_price_raw * (1 - WELCOME_DISCOUNT_PERCENT / 100), 2)

        if old_price is None or new_price is None:
            continue

        drop = old_price - new_price
        drop_percent = (drop / old_price) * 100 if drop > 0 else 0.0

        if new_price < old_price:
            status = "hit" if (drop_percent >= MIN_DROP_PERCENT and new_price <= price_threshold) else "drop"
        elif new_price > old_price:
            status = "increase"
        else:
            status = "no_change"

        old_size = normalize_size(old_p.get("variantValue"))
        new_size = normalize_size(new_p.get("variantValue"))

        # brand preferabil din new_p, fallback old_p/url
        brand = extract_brand(new_p, url) or extract_brand(old_p, url)

        entry = {
            "model_id": model_id,
            "brand": brand,
            "name": display_name,
            "url": url,
            "image": image_url,
            "old_price": old_price,
            "new_price": new_price,
            "drop_amount": round(drop, 2),
            "drop_percent": round(drop_percent, 2),
            "status": status,
            "old_prices_per_size": {},
            "new_prices_per_size": {},
        }

        # OLD size url
        if old_size in TRACKED_SIZES and old_price is not None:
            size_param_old = old_p.get("variantId") or old_p.get("variantValue") or old_size
            entry["old_prices_per_size"][old_size] = {
                "price": old_price,
                "url": build_size_url(url, size_param_old),
            }

        # NEW size url
        if new_size in TRACKED_SIZES and new_price is not None:
            size_param_new = new_p.get("variantId") or new_p.get("variantValue") or new_size
            entry["new_prices_per_size"][new_size] = {
                "price": new_price,
                "url": build_size_url(url, size_param_new),
            }

        results.append(entry)
        if status == "hit":
            hits.append(entry)

        if TEST_HITS_MODE and len(results) >= TEST_HITS_COUNT:
            break

    duration = time.perf_counter() - start

    with open(f"price_changes_{label}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    with open(f"missing_{label}.json", "w", encoding="utf-8") as f:
        json.dump(missing_products, f, indent=2, ensure_ascii=False)

    hits = apply_cooldown_filter(hits, label)
    send_email(hits, label, price_threshold)

    return {
        "label": label,
        "count": len(results),
        "old_total": total,
        "missing": len(missing_products),
        "hits": len(hits),
        "duration": duration,
    }


# ============================================================
#  MULTI CATEGORY
# ============================================================

CATEGORIES = {
    "walking": {
        "file": "products_ro_walking.json",
        "listing": "https://www.trendyol.com/ro/sr?wc=101429&wb=658%2C33%2C44%2C128%2C300%2C156%2C636%2C768%2C369&wg=2&vr=size%7C41_41-5_42_42-5_43_43-5_44_44-5_41-1-3_42-2-3_43-1-3_44-2-3_45-1-3&prc="
                   + str(MIN_PRICE_LINK) + "-*&sst=PRICE_BY_ASC",
        "price_threshold": PRICE_THRESHOLD_RUNNING_WALKING,
    },
    "running": {
        "file": "products_ro_running.json",
        "listing": "https://www.trendyol.com/ro/sr?wc=101426&wb=33%2C44%2C128%2C658%2C636%2C768&wg=2&vr=size%7C41_41-5_42_42-5_43_43-5_44_44-5&prc=110-*&sst=PRICE_BY_ASC",
        "price_threshold": PRICE_THRESHOLD_RUNNING_WALKING,
    },
    "sneakers_standard": {
        "file": "products_ro_sneakers_standard_merged.json",
        "listing": "https://www.trendyol.com/ro/sr?wc=1172&wb=33%2C128%2C333%2C104189&wg=2&vr=size%7C40-5_41_41-5_42_42-5_43_43-5_44_44-5_45_40-2-3_41-1-3_42-2-3_43-1-3_44-2-3&prc="
                   + str(MIN_PRICE_LINK) + "-*&sst=PRICE_BY_ASC&pi=2",
        "price_threshold": PRICE_THRESHOLD_SNEAKERS_STANDARD,
    },
    "sneakers_premium": {
        "file": "products_ro_sneakers_premium_merged.json",
        "listing": "https://www.trendyol.com/ro/sr?wc=1172&wb=44%2C54%2C658%2C172588&wg=2&vr=size%7C40-5_41_41-5_42_42-5_43_43-5_44_44-5_45&prc="
                   + str(MIN_PRICE_LINK) + "-*&sst=PRICE_BY_ASC",
        "price_threshold": PRICE_THRESHOLD_SNEAKERS_PREMIUM,
    },
    "boots": {
        "file": "products_ro_boots.json",
        "listing": "https://www.trendyol.com/ro/sr?wc=1025&wb=369%2C300%2C156%2C33%2C160%2C54%2C658%2C768&wg=2&vr=size%7C41_41-5_42_42-5_43_43-5_44_44-5_45_40-2-3_41-1-3_42-2-3_43-1-3_44-2-3&sst=PRICE_BY_ASC",
        "price_threshold": PRICE_THRESHOLD_BOOTS,
    },
    "campus": {
        "file": "products_ro_campus.json",
        "listing": "https://www.trendyol.com/ro/sr?wb=33&vr=size%7C36_38_39-5_40_40-5_41-5_42_42-5_43-5_44_46_36-2-3_37-1-3_38-2-3_40-2-3_41-1-3_42-2-3_43-1-3_44-2-3&qt=adidas+campus+00s&st=adidas+campus+00s&os=1&q=campus+00s&sst=PRICE_BY_ASC",
        "price_threshold": PRICE_THRESHOLD_BOOTS,
    }
}


def main():
    console = Console()
    print("\n================ MULTI CATEGORY START ================\n")

    summary = []
    global_start = time.perf_counter()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--window-size=1280,720",
                "--window-position=-2000,-2000",
            ],
        )
        context = browser.new_context(
            locale="ro-RO",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        )
        context.set_extra_http_headers({
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        page = context.new_page()

        for label, cfg in CATEGORIES.items():
            print(f"\n====== CATEGORY: {label} ======\n")

            with Progress(
                TextColumn("[bold blue]{task.description}[/]"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                expand=True,
            ) as progress:
                try:
                    result = main_single(
                        cfg["file"],
                        cfg["listing"],
                        label,
                        cfg.get("price_threshold", PRICE_THRESHOLD_DEFAULT),
                        progress=progress,
                        page=page,
                    )
                    summary.append(result)
                except Exception as e:
                    print(f"❌ CATEGORY FAILED: {label} | {e}")
                    summary.append({
                        "label": label,
                        "count": 0,
                        "old_total": 0,
                        "missing": 0,
                        "hits": 0,
                        "duration": 0,
                    })
                    continue



        browser.close()

    print("\n================ SUMMARY ================\n")

    for r in summary:
        print(
            f"- {r['label']}: "
            f"{r['count']}/{r['old_total']} products, "
            f"{r['hits']} hits, "
            f"{r['missing']} missing, "
            f"time: {format_duration(r['duration'])}"
        )

    total_checked = sum(r["count"] for r in summary)
    total_old = sum(r["old_total"] for r in summary)
    total_hits = sum(r["hits"] for r in summary)
    total_missing = sum(r["missing"] for r in summary)
    total_time = time.perf_counter() - global_start

    print("\n-----------------------------------------------")
    print(f"TOTAL PRODUCTS CHECKED: {total_checked} / {total_old}")
    print(f"TOTAL HITS:             {total_hits}")
    print(f"TOTAL MISSING:          {total_missing}")
    print(f"TOTAL TIME:             {format_duration(total_time)}")
    print("-----------------------------------------------")
    print("\n================ FINISHED ================\n")


if __name__ == "__main__":
    main()












