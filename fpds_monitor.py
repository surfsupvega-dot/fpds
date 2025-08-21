import json, os, time, asyncio, datetime, re
from typing import List, Dict
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ------- CONFIG FROM ENV -------
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")  # set in GitHub Secrets
AGENCY_NAME   = os.getenv("AGENCY_NAME", "DEPT OF THE NAVY")
NAICS_CODE    = os.getenv("NAICS_CODE", "531311")
PSC_CODE      = os.getenv("PSC_CODE", "R799")
DAYS_BACK     = int(os.getenv("DAYS_BACK", "30"))
MAX_PAGES     = int(os.getenv("MAX_PAGES", "3"))
STATE_FILE    = os.getenv("STATE_FILE", "fpds_seen.json")
ADVANCED_SEARCH_URL = "https://www.fpds.gov/fpdsng_cms/index.php/en/advanced-search.html"
RESULTS_WAIT_MS = 45000
# --------------------------------

def date_range_last_n_days(n: int):
    today = datetime.date.today()
    start = today - datetime.timedelta(days=n)
    return start.strftime("%m/%d/%Y"), today.strftime("%m/%d/%Y")

def load_seen() -> set:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data) if isinstance(data, list) else set()
    except Exception:
        pass
    return set()

def save_seen(seen: set):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, ensure_ascii=False, indent=2)

def send_discord(content: str):
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK set; printing instead:\n", content)
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=20)
    except Exception as e:
        print("Discord error:", e)

async def safe_fill(page, candidates: List[str], value: str) -> bool:
    for sel in candidates:
        try:
            if sel.startswith("label:"):
                label_text = sel.split("label:", 1)[1]
                locator = page.get_by_label(label_text, exact=False)
                await locator.fill(value)
                return True
            else:
                locator = page.locator(sel)
                if await locator.count() > 0:
                    await locator.first.fill(value)
                    return True
        except Exception:
            continue
    return False

async def safe_type(page, candidates: List[str], value: str) -> bool:
    for sel in candidates:
        try:
            if sel.startswith("label:"):
                label_text = sel.split("label:", 1)[1]
                locator = page.get_by_label(label_text, exact=False)
                await locator.click()
                await locator.fill("")
                await locator.type(value)
                return True
            else:
                locator = page.locator(sel)
                if await locator.count() > 0:
                    await locator.first.click()
                    await locator.first.fill("")
                    await locator.first.type(value)
                    return True
        except Exception:
            continue
    return False

async def click_first_that_exists(page, candidates: List[str]) -> bool:
    for sel in candidates:
        try:
            locator = page.locator(sel)
            if await locator.count() > 0:
                await locator.first.click()
                return True
        except Exception:
            continue
    return False

def parse_results_table(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    tables = soup.find_all("table")
    best_table, max_rows = None, 0
    for t in tables:
        rows = t.find_all("tr")
        if len(rows) > max_rows and len(rows) > 1:
            best_table, max_rows = t, len(rows)
    if not best_table:
        return results

    headers = [th.get_text(strip=True) for th in best_table.find_all("th")]
    for tr in best_table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        row_texts = [td.get_text(" ", strip=True) for td in tds]
        row_links = [a for a in tr.find_all("a", href=True)]
        link = row_links[0]["href"] if row_links else ""

        title = row_links[0].get_text(strip=True) if row_links else "FPDS Result"
        award_id = title or (row_links[0]["href"].rsplit("/",1)[-1] if row_links else "|".join(row_texts)[:120])
        vendor = ""
        date_signed = ""
        amount = ""

        for idx, head in enumerate(headers):
            val = row_texts[idx] if idx < len(row_texts) else ""
            h = head.lower()
            if "date" in h and not date_signed:
                date_signed = val
            if ("vendor" in h or "contractor" in h) and not vendor:
                vendor = val
            if ("amount" in h or "value" in h or "$" in val) and not amount:
                amount = val

        if not date_signed:
            m = re.search(r"\d{2}/\d{2}/\d{4}", " ".join(row_texts))
            date_signed = m.group(0) if m else ""

        if link.startswith("/"):
            link = "https://www.fpds.gov" + link
        elif link and not link.startswith("http"):
            link = "https://www.fpds.gov/" + link.lstrip("./")

        results.append({
            "id": award_id.strip(),
            "title": title or "FPDS Result",
            "vendor": vendor,
            "date": date_signed,
            "amount": amount,
            "link": link or "https://www.fpds.gov",
        })
    return results

async def run_once() -> List[Dict]:
    start_date, end_date = date_range_last_n_days(DAYS_BACK)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(ADVANCED_SEARCH_URL, wait_until="domcontentloaded")

        # Fill fields (robust guesses; adjust if FPDS changes markup)
        await safe_fill(page, [
            'label:Contracting Agency', 'input[placeholder*="Agency"]',
            'input[name*="agency"]', 'input[id*="agency"]'
        ], AGENCY_NAME)

        await safe_fill(page, [
            'label:NAICS', 'input[placeholder*="NAICS"]',
            'input[name*="naics"]', 'input[id*="naics"]'
        ], NAICS_CODE)

        await safe_fill(page, [
            'label:PSC', 'input[placeholder*="PSC"]',
            'input[name*="psc"]', 'input[id*="psc"]'
        ], PSC_CODE)

        await safe_type(page, [
            'label:Date Signed From', 'label:Signed From',
            'label:From Date', 'input[placeholder*="From"]',
            'input[name*="dateFrom"]', 'input[id*="dateFrom"]'
        ], start_date)

        await safe_type(page, [
            'label:Date Signed To', 'label:Signed To',
            'label:To Date', 'input[placeholder*="To"]',
            'input[name*="dateTo"]', 'input[id*="dateTo"]'
        ], end_date)

        await click_first_that_exists(page, [
            'button:has-text("Search")', 'input[type="submit"]',
            'button[type="submit"]', 'text=Search'
        ])

        await page.wait_for_timeout(4500)
        try:
                    # 4) Wait for results to render (be generous; FPDS can be slow)
        await page.wait_for_timeout(4500)  # small pause for initial layout
        try:
            # Try a few selectors that commonly represent the results table
            await page.wait_for_selector(
                'table, #searchResults table, table.results, div.results table',
                state='visible',
                timeout=RESULTS_WAIT_MS
            )
        except PlaywrightTimeoutError:
            # Send a friendly notice instead of crashing the job
            send_discord(f"⚠️ FPDS monitor: no results table appeared within {RESULTS_WAIT_MS/1000:.0f}s (site slow or no data).")
            return []

        # 5) Parse first page
        html = await page.content()
        page_results = parse_results_table(html)
        if not page_results:
            send_discord("ℹ️ FPDS monitor: results table found but no rows parsed (filters may have no matches).")
        results_collected.extend(page_results)

        pages_done = 1
        while pages_done < MAX_PAGES:
            moved = await click_first_that_exists(page, [
                'a[rel="next"]', 'a:has-text("Next")',
                'button:has-text("Next")', 'li.next a'
            ])
            if not moved:
                break
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=RESULTS_WAIT_MS)
            except PlaywrightTimeoutError:
                pass
            html = await page.content()
            results.extend(parse_results_table(html))
            pages_done += 1

        await ctx.close()
        await browser.close()
    return results

async def main():
    seen = load_seen()
    start_date, end_date = date_range_last_n_days(DAYS_BACK)
    try:
        results = await run_once()
    except Exception as e:
        send_discord(f"❗ FPDS monitor error: `{e}`")
        return

    if not results:
        send_discord(f"⚠️ No FPDS results found for this run ({start_date} – {end_date}).")
        return

    new_count = 0
    for r in results:
        rid = (r.get("id") or "").strip()
        if rid and rid not in seen:
            parts = [f"🆕 **{r.get('title') or 'FPDS Result'}**"]
            if r.get("vendor"): parts.append(f"Vendor: {r['vendor']}")
            if r.get("date"): parts.append(f"Date: {r['date']}")
            if r.get("amount"): parts.append(f"Amount: {r['amount']}")
            parts.append(r.get("link") or "https://www.fpds.gov")
            send_discord("\n".join(parts))
            seen.add(rid)
            new_count += 1

    if new_count == 0:
        send_discord("ℹ️ No new FPDS items since last check.")
    save_seen(seen)

if __name__ == "__main__":
    asyncio.run(main())
