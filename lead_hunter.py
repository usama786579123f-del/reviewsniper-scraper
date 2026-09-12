"""
ReviewSniper - Lead Hunter
---------------------------
Google Maps par ek niche + city search karta hai, listings kholta hai,
reviews ko "Newest" sort karta hai, aur pichle 24 ghanton mein aayi
1-star/2-star reviews wali businesses ko "crisis lead" ke tor par
MongoDB mein save karta hai.

USAGE:
    python lead_hunter.py --query "Dental Clinics in Dubai"

ENV VARS (.env file mein daalo, GitHub Actions mein secrets se):
    MONGODB_URI   -> MongoDB Atlas connection string
    DB_NAME       -> default: reviewsniper
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("DB_NAME", "reviewsniper")

BREVO_API_KEY = os.getenv("BREVO_API_KEY")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")

TIME_UNIT_TO_HOURS = {
    "minute": 1 / 60,
    "hour": 1,
    "day": 24,
    "week": 24 * 7,
}

CRISIS_WINDOW_HOURS = 48
TARGET_STARS = {1, 2}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def get_db():
    if not MONGODB_URI:
        print("[FATAL] MONGODB_URI env var missing. .env file check karo.")
        sys.exit(1)
    client = MongoClient(MONGODB_URI)
    return client[DB_NAME]


def save_debug(page, tag: str):
    """Har failure point par screenshot + full page HTML dono save karo,
    taake agla run dekh kar pata chal sake Google ne asal mein kya
    dikhaya tha (normal maps, consent wall, ya bot-check page)."""
    try:
        page.screenshot(path=f"debug_{tag}.png")
    except Exception:
        pass
    try:
        with open(f"debug_{tag}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass
    print(f"[DEBUG] Saved debug_{tag}.png and debug_{tag}.html")


def parse_relative_time(text: str) -> float | None:
    text = text.lower().strip()
    match = re.search(r"(a|an|\d+)\s+(minute|hour|day|week)", text)
    if not match:
        return None
    qty_raw, unit = match.groups()
    qty = 1 if qty_raw in ("a", "an") else int(qty_raw)
    return qty * TIME_UNIT_TO_HOURS.get(unit, 9999)


def extract_star_rating(aria_label: str) -> int | None:
    match = re.search(r"(\d+)\s+star", aria_label.lower())
    return int(match.group(1)) if match else None


def dismiss_consent_screen(page):
    consent_buttons = [
        page.get_by_role("button", name=re.compile(r"^Accept all$", re.I)),
        page.get_by_role("button", name=re.compile(r"^I agree$", re.I)),
        page.get_by_role("button", name=re.compile(r"^Reject all$", re.I)),
    ]
    for btn in consent_buttons:
        try:
            if btn.count() > 0 and btn.first.is_visible(timeout=3000):
                btn.first.click()
                page.wait_for_timeout(1500)
                print("[INFO] Dismissed a Google consent screen.")
                return
        except Exception:
            continue


def find_search_box(page):
    strategies = [
        lambda: page.get_by_placeholder("Search Google Maps"),
        lambda: page.locator("#searchboxinput"),
        lambda: page.locator('input[aria-label="Search Google Maps"]'),
        lambda: page.get_by_role("combobox", name=re.compile("Search Google Maps", re.I)),
    ]
    for build_locator in strategies:
        try:
            box = build_locator()
            box.wait_for(state="visible", timeout=20000)
            return box
        except Exception:
            continue
    return None


def scrape_niche(page, query: str, max_listings: int = 20):
    print(f"[INFO] Searching Google Maps: {query}")

    search_box = None
    for attempt in range(1, 3):
        page.goto("https://www.google.com/maps", timeout=90000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        dismiss_consent_screen(page)
        search_box = find_search_box(page)
        if search_box:
            break
        print(f"[WARN] Search box not found on attempt {attempt}, retrying...")

    if not search_box:
        save_debug(page, "no_searchbox")
        print(
            "[FATAL] Search box never appeared after 2 attempts. Debug "
            "files saved so you can see what page Google served."
        )
        raise RuntimeError("Search box not found")

    search_box.fill(query)
    page.keyboard.press("Enter")

    page.wait_for_timeout(4000)

    results_panel = page.locator('div[role="feed"]')
    try:
        results_panel.wait_for(timeout=15000)
    except PWTimeout:
        save_debug(page, "no_results_panel")
        print("[WARN] Results panel not found - Google Maps ne layout change kiya ho sakta hai, ya block screen dikha rahi hai.")
        return []

    listing_links = set()
    prev_count = 0
    for _ in range(6):
        cards = page.locator('div[role="feed"] a[href*="/maps/place/"]')
        count = cards.count()
        for i in range(count):
            href = cards.nth(i).get_attribute("href")
            if href:
                listing_links.add(href)
        if count == prev_count:
            break
        prev_count = count
        results_panel.evaluate("el => el.scrollBy(0, 1500)")
        page.wait_for_timeout(1500)

    listing_links = list(listing_links)[:max_listings]
    print(f"[INFO] Found {len(listing_links)} listings to check.")
    return listing_links


def get_phone_number(page) -> str | None:
    """Google Maps ka phone number nikalta hai, 4 alag strategies try kar ke
    (search box wale style mein). Jaise hi ek kaam kar jaye, wahin ruk jata hai."""

    # Strategy 1: sabse aam format - button jiska data-item-id "phone:" se shuru hota hai
    try:
        btn = page.locator('button[data-item-id^="phone:"]')
        if btn.count() > 0:
            label = btn.first.get_attribute("aria-label") or ""
            label = label.replace("Phone:", "").strip()
            if label:
                return label
    except Exception:
        pass

    # Strategy 2: kabhi kabhi ye button ki jagah link (a tag) hota hai
    try:
        link = page.locator('a[data-item-id^="phone:"]')
        if link.count() > 0:
            label = link.first.get_attribute("aria-label") or ""
            label = label.replace("Phone:", "").strip()
            if label:
                return label
    except Exception:
        pass

    # Strategy 3: data-tooltip attribute wala button (Google kabhi kabhi is naam se deta hai)
    try:
        tooltip_btn = page.locator('button[data-tooltip="Copy phone number"]')
        if tooltip_btn.count() > 0:
            label = tooltip_btn.first.get_attribute("aria-label") or ""
            label = label.replace("Phone:", "").strip()
            if label:
                return label
    except Exception:
        pass

    # Strategy 4: aakhri fallback - poore info panel ke text mein phone-jaisi
    # digit pattern dhoondo (jab upar wale teeno selectors fail ho jayein)
    try:
        panel_text = page.locator('div[role="main"]').first.inner_text(timeout=3000)
        match = re.search(r'(\+?\d[\d\-\s\(\)]{6,}\d)', panel_text)
        if match:
            return match.group(1).strip()
    except Exception:
        pass

    return None


def check_listing_for_crisis(page, url: str, niche: str) -> dict | None:
    try:
        page.goto(url, timeout=30000)
        page.wait_for_timeout(2500)

        name_el = page.locator("h1").first
        business_name = name_el.inner_text(timeout=5000).strip() if name_el else "Unknown"

        website = None
        website_btn = page.get_by_role("link", name=re.compile("Website", re.I))
        if website_btn.count() > 0:
            website = website_btn.first.get_attribute("href")

        # Phone number ab Reviews tab par click karne SE PEHLE nikal rahe hain,
        # kyun ki tab switch hone ke baad ye info panel se gayab ho sakta hai
        phone = get_phone_number(page)

        reviews_tab = page.get_by_role("tab", name=re.compile("Reviews", re.I))
        if reviews_tab.count() == 0:
            return None
        reviews_tab.first.click()
        page.wait_for_timeout(2000)

        sort_button = page.get_by_role("button", name=re.compile("Sort", re.I))
        if sort_button.count() > 0:
            sort_button.first.click()
            page.wait_for_timeout(800)
            newest_option = page.get_by_role("menuitemradio", name=re.compile("Newest", re.I))
            if newest_option.count() > 0:
                newest_option.first.click()
                page.wait_for_timeout(2000)

        review_cards = page.locator('div[data-review-id]')
        if review_cards.count() == 0:
            return None

        first_review = review_cards.first
        star_el = first_review.locator('span[role="img"]').first
        aria_label = star_el.get_attribute("aria-label") or ""
        stars = extract_star_rating(aria_label)

        time_el = first_review.locator("span").filter(has_text=re.compile(r"ago|week|month|year"))
        time_text = time_el.first.inner_text(timeout=3000) if time_el.count() > 0 else ""
        hours_ago = parse_relative_time(time_text)

        if stars is None or hours_ago is None:
            return None

        if stars not in TARGET_STARS or hours_ago > CRISIS_WINDOW_HOURS:
            return None

        review_text_el = first_review.locator('span[jsan]').first
        review_text = ""
        try:
            review_text = review_text_el.inner_text(timeout=2000)
        except Exception:
            pass

        return {
            "business_name": business_name,
            "niche": niche,
            "maps_url": url,
            "website": website,
            "phone": phone,
            "stars": stars,
            "review_text": review_text.strip(),
            "hours_ago": round(hours_ago, 1),
            "status": "new",
            "scraped_at": datetime.now(timezone.utc),
        }

    except PWTimeout:
        print(f"[WARN] Timeout on {url}, skipping.")
        return None
    except Exception as e:
        print(f"[WARN] Error on {url}: {e}")
        return None


def send_alert_email(lead: dict) -> tuple[bool, str]:
    """Brevo ke Transactional Email API se ek alert email bhejta hai.
    Return: (success, error_message). Agar keys set nahi hain to
    (False, "not configured") deta hai, taake caller graceful handle kar sake."""
    if not (BREVO_API_KEY and ALERT_EMAIL_FROM and ALERT_EMAIL_TO):
        return False, "BREVO_API_KEY / ALERT_EMAIL_FROM / ALERT_EMAIL_TO env vars missing"

    subject = f"[ReviewSniper] New crisis lead: {lead['business_name']} ({lead['stars']} star)"
    html_content = f"""
    <div style="font-family: Arial, sans-serif; font-size: 14px; color: #222;">
      <h2 style="margin-bottom: 4px;">New Crisis Lead Found</h2>
      <p><strong>Business:</strong> {lead['business_name']}</p>
      <p><strong>Niche:</strong> {lead['niche']}</p>
      <p><strong>Rating:</strong> {lead['stars']} star ({lead['hours_ago']}h ago)</p>
      <p><strong>Review:</strong> {lead['review_text'] or '(no text)'}</p>
      <p><strong>Phone:</strong> {lead['phone'] or 'N/A'}</p>
      <p><strong>Website:</strong> {lead['website'] or 'N/A'}</p>
      <p><a href="{lead['maps_url']}">View on Google Maps</a></p>
    </div>
    """

    payload = {
        "sender": {"email": ALERT_EMAIL_FROM, "name": "ReviewSniper Alerts"},
        "to": [{"email": ALERT_EMAIL_TO}],
        "subject": subject,
        "htmlContent": html_content,
    }

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "accept": "application/json",
            "api-key": BREVO_API_KEY,
            "content-type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True, ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, str(e)


def log_email_alert(db, lead: dict, success: bool, error: str = ""):
    """Har email attempt (kamyaab ya nakaam) ko 'email_alerts' collection
    mein save karta hai, taake EmailAlerts.tsx real history dikha sake."""
    db["email_alerts"].insert_one(
        {
            "business_name": lead["business_name"],
            "niche": lead["niche"],
            "maps_url": lead["maps_url"],
            "recipient": ALERT_EMAIL_TO or "",
            "status": "sent" if success else "failed",
            "error": error,
            "sent_at": datetime.now(timezone.utc),
        }
    )


def save_lead(db, lead: dict):
    leads = db["leads"]
    existing = leads.find_one({"maps_url": lead["maps_url"], "review_text": lead["review_text"]})
    if existing:
        print(f"[SKIP] Already saved: {lead['business_name']}")
        return
    leads.insert_one(lead)
    print(f"[SAVED] {lead['business_name']} ({lead['stars']}*, {lead['hours_ago']}h ago)")

    success, error = send_alert_email(lead)
    log_email_alert(db, lead, success, error)
    if success:
        print(f"[EMAIL] Alert sent for {lead['business_name']}")
    else:
        print(f"[EMAIL] Alert NOT sent for {lead['business_name']}: {error}")


def run(query: str, max_listings: int):
    # Sab matrix jobs ek hi second par Google Maps na maarein, is liye
    # thoda random jitter (0-8 seconds) start mein daal do.
    time.sleep(random.uniform(0, 8))

    db = get_db()
    with sync_playwright() as p:
        headless = os.getenv("HEADLESS", "true").lower() != "false"
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            locale="en-US",
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()

        listing_links = scrape_niche(page, query, max_listings=max_listings)

        found = 0
        for url in listing_links:
            lead = check_listing_for_crisis(page, url, niche=query)
            if lead:
                save_lead(db, lead)
                found += 1
            time.sleep(1)

        browser.close()
        print(f"[DONE] {found} crisis lead(s) saved out of {len(listing_links)} checked.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True, help='e.g. "Dental Clinics in Dubai"')
    parser.add_argument("--max-listings", type=int, default=20)
    args = parser.parse_args()
    run(args.query, args.max_listings)