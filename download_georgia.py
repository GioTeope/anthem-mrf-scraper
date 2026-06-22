import os
import re
import time
import logging
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from playwright.sync_api import sync_playwright


# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
BASE_URL = "https://www.anthem.com/machine-readable-file/search/"
BASE_FOLDER = "Georgia"
SEARCH_TERM = "Georgia"

MAX_WORKERS = 4
RETRY_COUNT = 3
CHUNK_SIZE = 1024 * 512  # 512 KB
HEADLESS = False         # set True later when stable

os.makedirs(BASE_FOLDER, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("download_georgia.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "", name)
    cleaned = "".join(c for c in name if c.isalnum() or c in " _-().,&").strip()
    return cleaned or "Unknown"


def normalize_space(text: str) -> str:
    return " ".join((text or "").split()).strip()


def is_download_link(url: str) -> bool:
    url = (url or "").lower()
    return any(ext in url for ext in [".json", ".json.gz", ".gz", ".pdf"])


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        }
    )
    return s


def download_file(url: str, dest: str) -> str:
    """
    Returns one of: downloaded / skipped / failed
    """
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        log.info(f"    SKIP (exists): {os.path.basename(dest)}")
        return "skipped"

    os.makedirs(os.path.dirname(dest), exist_ok=True)

    session = new_session()

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            with session.get(url, stream=True, timeout=180) as r:
                if r.status_code == 200:
                    tmp = dest + ".tmp"
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(CHUNK_SIZE):
                            if chunk:
                                f.write(chunk)
                    os.replace(tmp, dest)
                    log.info(f"    ✓ {os.path.basename(dest)}")
                    return "downloaded"
                else:
                    log.warning(f"    HTTP {r.status_code} — {url}")
        except Exception as e:
            log.warning(f"    Attempt {attempt}/{RETRY_COUNT} failed for {url}: {e}")
            time.sleep(2 * attempt)

    log.error(f"    ✗ FAILED after {RETRY_COUNT} attempts: {url}")
    return "failed"


def download_tasks(tasks, max_workers=MAX_WORKERS):
    """
    Download a list of (url, dest) tasks immediately.
    Returns tuple: (downloaded, skipped, failed)
    """
    if not tasks:
        return 0, 0, 0

    downloaded = 0
    skipped = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_file, url, dest): (url, dest) for url, dest in tasks}

        for future in as_completed(futures):
            url, dest = futures[future]
            try:
                status = future.result()
                if status == "downloaded":
                    downloaded += 1
                elif status == "skipped":
                    skipped += 1
                else:
                    failed += 1
            except Exception as e:
                log.error(f"    Unexpected error for {url}: {e}")
                failed += 1

    return downloaded, skipped, failed


# ──────────────────────────────────────────────
# PLAYWRIGHT HELPERS
# ──────────────────────────────────────────────
def goto_search_page(page):
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    try:
        select_loc = page.locator("select").first
        if select_loc.count() > 0:
            select_loc.select_option(label="Search by Name")
            page.wait_for_timeout(1000)
    except Exception:
        pass


def get_search_input(page):
    candidates = [
        page.get_by_role("textbox").first,
        page.locator("input[type='text']").first,
        page.locator("input:visible").first,
    ]

    for loc in candidates:
        try:
            loc.wait_for(timeout=5000)
            return loc
        except Exception:
            pass

    raise RuntimeError("Could not find the search input.")


def open_dropdown(page, term: str):
    input_box = get_search_input(page)
    input_box.click()
    page.wait_for_timeout(300)

    try:
        input_box.fill("")
    except Exception:
        input_box.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")

    page.wait_for_timeout(300)
    input_box.type(term, delay=100)
    page.wait_for_timeout(1800)
    return input_box


def get_visible_georgia_suggestions(page, term: str) -> list:
    """
    Use JS to grab visible text blocks that start with the search term.
    """
    suggestions = page.evaluate(
        """
        (term) => {
            function isVisible(el) {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' &&
                       style.visibility !== 'hidden' &&
                       rect.width > 0 &&
                       rect.height > 0;
            }

            const els = Array.from(document.querySelectorAll('*'));
            const results = [];

            for (const el of els) {
                if (!isVisible(el)) continue;

                const text = (el.innerText || '').trim();
                if (!text) continue;

                const lines = text
                    .split('\\n')
                    .map(x => x.trim())
                    .filter(Boolean);

                for (const line of lines) {
                    const lower = line.toLowerCase();
                    if (!lower.startsWith(term.toLowerCase())) continue;
                    if (lower === term.toLowerCase()) continue;
                    if (lower === 'search') continue;
                    if (lower === 'search name') continue;

                    if (!results.includes(line)) {
                        results.push(line);
                    }
                }
            }

            return results;
        }
        """,
        term,
    )

    cleaned = []
    seen = set()

    for s in suggestions:
        s = normalize_space(s)
        if s and s not in seen and s.lower().startswith(term.lower()) and s.lower() != term.lower():
            seen.add(s)
            cleaned.append(s)

    return cleaned


def collect_all_dropdown_suggestions(page, term: str) -> list:
    open_dropdown(page, term)

    all_suggestions = []
    seen = set()

    for _ in range(12):
        current = get_visible_georgia_suggestions(page, term)

        for item in current:
            if item not in seen:
                seen.add(item)
                all_suggestions.append(item)

        try:
            page.mouse.wheel(0, 500)
            page.wait_for_timeout(700)
        except Exception:
            break

    log.info(f"Collected {len(all_suggestions)} suggestion(s) for '{term}'")
    for s in all_suggestions:
        log.info(f"  - {s}")

    return all_suggestions


def click_exact_suggestion(page, term: str, suggestion: str) -> bool:
    input_box = open_dropdown(page, term)
    page.wait_for_timeout(1200)

    clicked = page.evaluate(
        """
        (targetText) => {
            function isVisible(el) {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' &&
                       style.visibility !== 'hidden' &&
                       rect.width > 0 &&
                       rect.height > 0;
            }

            const els = Array.from(document.querySelectorAll('*'));

            for (const el of els) {
                if (!isVisible(el)) continue;
                const text = (el.innerText || '').trim();

                if (text === targetText) {
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return true;
                }
            }

            for (const el of els) {
                if (!isVisible(el)) continue;
                const text = (el.innerText || '').trim();
                if (!text) continue;

                const lines = text.split('\\n').map(x => x.trim()).filter(Boolean);
                if (lines.includes(targetText)) {
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return true;
                }
            }

            return false;
        }
        """,
        suggestion,
    )

    if not clicked:
        log.error(f"Could not click suggestion: {suggestion}")
        return False

    page.wait_for_timeout(1200)

    try:
        current_value = input_box.input_value()
        log.info(f"Input after click: {current_value!r}")
    except Exception:
        pass

    return True


def click_search(page):
    candidates = [
        page.get_by_role("button", name="Search"),
        page.locator("button:has-text('Search')"),
        page.locator("input[type='submit'][value='Search']"),
    ]

    for loc in candidates:
        try:
            if loc.count() > 0:
                btn = loc.first
                btn.scroll_into_view_if_needed(timeout=3000)
                btn.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(3500)
                return
        except Exception:
            pass

    raise RuntimeError("Could not click Search button.")


def collect_file_links(page) -> list:
    links = set()

    try:
        anchors = page.locator("a")
        count = anchors.count()
    except Exception:
        count = 0

    for i in range(count):
        try:
            href = anchors.nth(i).get_attribute("href")
            if href and is_download_link(href):
                links.add(urljoin(page.url, href))
        except Exception:
            pass

    return sorted(links)


def process_one_suggestion(page, suggestion: str) -> list:
    log.info(f"Processing suggestion: {suggestion}")

    folder = os.path.join(BASE_FOLDER, safe_name(suggestion))
    os.makedirs(folder, exist_ok=True)

    goto_search_page(page)

    selected = click_exact_suggestion(page, SEARCH_TERM, suggestion)
    if not selected:
        raise RuntimeError(f"Selection failed for '{suggestion}'")

    click_search(page)

    links = collect_file_links(page)
    log.info(f"  Files found for '{suggestion}': {len(links)}")

    tasks = []
    for href in links:
        file_name = href.split("/")[-1].split("?")[0] or "file"
        dest = os.path.join(folder, file_name)
        tasks.append((href, dest))

    return tasks


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    grand_downloaded = 0
    grand_skipped = 0
    grand_failed = 0
    processed = 0

    # Prevent duplicate downloads across employers if the same URL appears again
    seen_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()
        page.set_default_timeout(30000)

        goto_search_page(page)
        suggestions = collect_all_dropdown_suggestions(page, SEARCH_TERM)

        if not suggestions:
            log.error("No dropdown suggestions found.")
            browser.close()
            return

        for idx, suggestion in enumerate(suggestions, start=1):
            log.info(f"\n[{idx}/{len(suggestions)}] {suggestion}")

            try:
                tasks = process_one_suggestion(page, suggestion)

                # Remove duplicates by URL
                unique_tasks = []
                for url, dest in tasks:
                    if url not in seen_urls:
                        seen_urls.add(url)
                        unique_tasks.append((url, dest))

                log.info(f"  Unique files to download now: {len(unique_tasks)}")

                d, s, f = download_tasks(unique_tasks, max_workers=MAX_WORKERS)

                grand_downloaded += d
                grand_skipped += s
                grand_failed += f
                processed += 1

                log.info(
                    f"  Completed '{suggestion}' → "
                    f"downloaded={d}, skipped={s}, failed={f}"
                )

            except Exception as e:
                log.error(f"Failed processing '{suggestion}': {e}")
                grand_failed += 1

        browser.close()

    log.info("\n" + "=" * 60)
    log.info(f"Employers processed: {processed}")
    log.info(f"TOTAL  ✓ {grand_downloaded} downloaded  ⏭ {grand_skipped} skipped  ✗ {grand_failed} failed")
    log.info(f"Files saved under: {os.path.abspath(BASE_FOLDER)}")


if __name__ == "__main__":
    main()