import os
import re
import ssl
import smtplib
import certifi
from email.message import EmailMessage
from playwright.sync_api import sync_playwright

URL = "https://www.evoucher.ro/magazin/trendyol/"

# Gmail
EMAIL_ENABLED = True
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

EMAIL_USER = "bluegaming764@gmail.com"
EMAIL_FROM = EMAIL_USER
EMAIL_TO = "bluegaming764@gmail.com"
EMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# Settings
THRESHOLD = 40            # dacă vrei alertă doar peste 40
HEADLESS = True          # pune True după ce confirmi că merge
ALWAYS_SEND = True        # dacă True -> trimite și când nu găsește nimic

PERCENT_RE = re.compile(r"(\d{1,3})\s*%")

def accept_cookies(page):
    candidates = [
        "button:has-text('Accept')",
        "button:has-text('Accept all')",
        "button:has-text('Acceptă')",
        "button:has-text('Acceptați')",
        "button:has-text('Accept toate')",
        "button:has-text('OK')",
    ]
    for sel in candidates:
        try:
            page.locator(sel).first.click(timeout=1500)
            return
        except Exception:
            pass

def extract_percents_from_text(text: str):
    percents = set()
    for m in PERCENT_RE.finditer(text or ""):
        p = int(m.group(1))
        if 1 <= p <= 100:
            percents.add(p)
    return percents

import re
PERCENT_RE = re.compile(r"(\d{1,3})\s*%")

def get_percents(page):
    percents = set()

    # 1) exact din div-urile care conțin procentul din card (cum ai în poză)
    selectors = [
        "div.font150.sale_letter",          # cel din screenshot
        "div.sale_letter",                  # fallback
        "div.font150",                      # fallback (dacă se schimbă)
        "span.font150.sale_letter",         # în caz că e span
    ]

    for sel in selectors:
        try:
            nodes = page.locator(sel)
            for i in range(nodes.count()):
                txt = (nodes.nth(i).inner_text() or "").strip()
                m = PERCENT_RE.search(txt)
                if m:
                    p = int(m.group(1))
                    if 1 <= p <= 100:
                        percents.add(p)
        except Exception:
            pass

    # 2) fallback: dacă nu găsește nimic, caută doar în "offer cards"
    # (nu în tot body)
    if not percents:
        try:
            cards_text = page.evaluate("""
                () => Array.from(document.querySelectorAll(".hr_grid_img, .deal_string, a[href*='/cupon-trendyol/']"))
                  .map(el => (el.innerText || el.textContent || "").trim())
                  .join("\\n")
            """)
            for m in PERCENT_RE.finditer(cards_text or ""):
                p = int(m.group(1))
                if 1 <= p <= 100:
                    percents.add(p)
        except Exception:
            pass

    return sorted(percents)

def send_email(percents, above_threshold):
    if not EMAIL_ENABLED:
        return
    if not EMAIL_PASSWORD:
        print("⚠ Missing GMAIL_APP_PASSWORD env var")
        return

    subject = "eVoucher Trendyol – procente găsite"
    found_line = ", ".join(map(str, percents)) if percents else "(nimic)"
    above_line = ", ".join(map(str, above_threshold)) if above_threshold else "(nimic)"

    text = (
        f"Pagina: {URL}\n\n"
        f"Procente găsite: {found_line}\n"
        f"Peste {THRESHOLD}%: {above_line}\n"
    )

    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(text)

    ctx = ssl.create_default_context(cafile=certifi.where())
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls(context=ctx)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)

    print("📧 Email sent.")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        accept_cookies(page)

        # scroll ca să declanșeze lazy loading (dacă există)
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)
        except Exception:
            pass

        percents = get_percents(page)
        above = [x for x in percents if x > THRESHOLD]

        print("Found percents:", percents)
        print(f"Above {THRESHOLD}%:", above)

        browser.close()

    if percents or ALWAYS_SEND:
        send_email(percents, above)

if __name__ == "__main__":
    main()

