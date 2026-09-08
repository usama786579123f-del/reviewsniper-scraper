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
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("DB_NAME", "reviewsniper")

# Google Maps "time ago" text ko hours mein convert karne ke liye
TIME_UNIT_TO_HOURS = {
    "minute": 1 / 60,
    "hour": 1,
    "day": 24,
    "week": 24 * 7,
}

CRISIS_WINDOW_HOURS = 24
TARGET_STARS = {1, 2}


def get_db():
    if not MONGODB_URI:
        print("[FATAL] MONGODB_URI env var missing. .env file check karo.")
        sys.exit(1)
    client = MongoClient(MONGODB_URI)
    return client[DB_NAME]


def parse_relative_time(text: str) -> float | None:
    """'3 hours ago', 'a day ago', '2 weeks ago' -> hours (float) ya None."""
    text = text.lower().strip()
    match = re.search(r"(a|an|\d+)\s+(minute|hour|day|week)", text)
    if not match:
        return None
    qty_raw, unit = match.groups()
    qty = 1 if qty_raw in ("a", "an") else int(qty_raw)
    return qty * TIME_UNIT_TO_HOURS.get(unit, 9999)


def extract_star_rating(aria_label: str) -> int | None:
    """Playwright element ka aria-label kuch is tarah hota hai: '3 stars'"""
    match = re.search(r"(\d+)\s+star", aria_label.lower())
    return int(match.group(1)) if match else None


def dismiss_consent_screen(page):
    """Google sometimes shows a cookie-consent interstitial ('Before you
    continue to Google...') instead of Maps on the first visit. Click
    through it if present so the real search box becomes available."""
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


def scrape_niche(page, query: str, max_listings: int = 20):
    print(f"[INFO] Searching Google Maps: {query}")
    page.goto("https://www.google.com/maps", timeout=90000, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    dismiss_consent_screen(page)

    try:
        search_box = page.locator("#searchboxinput")
        search_box.wait_for(state="visible", timeout=90000)
    except PWTimeout:
        # Save evidence so we can see what Google actually served us.
        page.screenshot(path="debug_maps_screen.png")
        print(
            "[FATAL] Search box never appeared. Saved a screenshot to "
            "debug_maps_screen.png so you can see what page Google served."
        )
        raise

    search_box.fill(query)
    search_box.press("Enter")

    # Results panel load hone ka wait
    page.wait_for_timeout(4000)

    # Left panel mein listings ko scroll kar ke load karo
    results_panel = page.locator('div[role="feed"]')
    try:
        results_panel.wait_for(timeout=15000)
    except PWTimeout:
        print("[WARN] Results panel not found — Google Maps ne layout change kiya ho sakta hai.")
        return []

    listing_links = set()
    prev_count = 0
    for _ in range(6):  # scroll passes — increase for more results
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


def check_listing_for_crisis(page, url: str, niche: str) -> dict | None:
    try:
        page.goto(url, timeout=30000)
        page.wait_for_timeout(2500)

        name_el = page.locator("h1").first
        business_name = name_el.inner_text(timeout=5000).strip() if name_el else "Unknown"

        # Reviews tab par click karo
        reviews_tab = page.get_by_role("tab", name=re.compile("Reviews", re.I))
        if reviews_tab.count() == 0:
            return None
        reviews_tab.first.click()
        page.wait_for_timeout(2000)

        # "Newest" sort select karo
        sort_button = page.get_by_role("button", name=re.compile("Sort", re.I))
        if sort_button.count() > 0:
            sort_button.first.click()
            page.wait_for_timeout(800)
            newest_option = page.get_by_role("menuitemradio", name=re.compile("Newest", re.I))
            if newest_option.count() > 0:
                newest_option.first.click()
                page.wait_for_timeout(2000)

        # Pehli (sabse nayi) review card check karo
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
            return None  # crisis nahi hai, skip

        review_text_el = first_review.locator('span[jsan]').first
        review_text = ""
        try:
            review_text = review_text_el.inner_text(timeout=2000)
        except Exception:
            pass

        # Website link nikalo (email scraping Step 2 mein alag script se hoga)
        website = None
        website_btn = page.get_by_role("link", name=re.compile("Website", re.I))
        if website_btn.count() > 0:
            website = website_btn.first.get_attribute("href")

        phone = None
        phone_btn = page.locator('button[data-item-id^="phone:"]')
        if phone_btn.count() > 0:
            phone_label = phone_btn.first.get_attribute("aria-label") or ""
            phone = phone_label.replace("Phone:", "").strip()

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


def save_lead(db, lead: dict):
    leads = db["leads"]
    existing = leads.find_one({"maps_url": lead["maps_url"], "review_text": lead["review_text"]})
    if existing:
        print(f"[SKIP] Already saved: {lead['business_name']}")
        return
    leads.insert_one(lead)
    print(f"[SAVED] {lead['business_name']} ({lead['stars']}★, {lead['hours_ago']}h ago)")


def run(query: str, max_listings: int):
    db = get_db()
    with sync_playwright() as p:
        # Locally you can set HEADLESS=false in your terminal to WATCH the
        # browser while debugging. On GitHub Actions there's no screen, so
        # it always runs headless there (the default).
        headless = os.getenv("HEADLESS", "true").lower() != "false"
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(locale="en-US")
        page = context.new_page()

        listing_links = scrape_niche(page, query, max_listings=max_listings)

        found = 0
        for url in listing_links:
            lead = check_listing_for_crisis(page, url, niche=query)
            if lead:
                save_lead(db, lead)
                found += 1
            time.sleep(1)  # thoda politeness delay

        browser.close()
        print(f"[DONE] {found} crisis lead(s) saved out of {len(listing_links)} checked.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True, help='e.g. "Dental Clinics in Dubai"')
    parser.add_argument("--max-listings", type=int, default=20)
    args = parser.parse_args()
    run(args.query, args.max_listings)
