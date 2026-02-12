import os
import re
from dataclasses import dataclass
from datetime import time
from typing import List, Optional

from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from playwright.sync_api import sync_playwright

DEBUG = os.environ.get("DEBUG", "false").strip().lower() == "true"

def ensure_debug_dir():
    if DEBUG:
        os.makedirs("debug", exist_ok=True)

@dataclass
class TeeTime:
    course: str
    play_date: str  # YYYY-MM-DD
    tee_time: str   # HH:MM (24h)
    players_hint: Optional[str]
    booking_url: str

def parse_hhmm_24(s: str) -> time:
    hh, mm = s.strip().split(":")
    return time(int(hh), int(mm))

def ampm_to_24h(t: str) -> Optional[str]:
    t = t.strip()
    try:
        dt = dtparser.parse(t)
        return dt.strftime("%H:%M")
    except Exception:
        return None

def is_before_or_equal(hhmm: str, latest: time) -> bool:
    t = parse_hhmm_24(hhmm)
    return (t.hour, t.minute) <= (latest.hour, latest.minute)

def get_timesheet_context(page):
    """
    Returns (ctx, kind) where ctx is either:
      - the main page, or
      - a frame that actually contains the MiClub timesheet.

    This matters because MiClub often renders tee times inside an iframe,
    which screenshots show but page.content() does NOT.
    """
    # 1) If the main page already has the timesheet, use it
    try:
        if page.locator(".time-wrapper").count() > 0:
            return page, "page"
    except Exception:
        pass

    # 2) Otherwise search frames (very common on MiClub)
    for fr in page.frames:
        try:
            if fr.locator(".time-wrapper").count() > 0:
                return fr, "frame"
        except Exception:
            continue

    # Fallback: just return the page
    return page, "page"
    
def looks_like_players_ok(players_hint: Optional[str], min_players: int) -> bool:
    """
    Best-effort parsing of strings like:
      - "1 to 4 players"
      - "1 or 2 players"
      - "players up to 4"
    If unknown, returns True.
    """
    if not players_hint:
        return True

    s = players_hint.lower()
    nums = [int(x) for x in re.findall(r"\d+", s)]
    if not nums:
        return True

    if "to" in s and len(nums) >= 2:
        return nums[1] >= min_players

    if "or" in s and len(nums) >= 2:
        return max(nums) >= min_players

    return max(nums) >= min_players

def scrape_quick18_hamersley(play_date: str, min_players: int, latest: time) -> List[TeeTime]:
    """
    Quick18 search matrix shows 9 Holes + 18 Holes columns.
    We only accept rows where the 18 Holes column has a clickable Select.
    Then we open the slot page and try to confirm it supports min_players.
    """
    yyyymmdd = play_date.replace("-", "")
    base_url = "https://hamersley.quick18.com"
    url = f"{base_url}/teetimes/searchmatrix?teedate={yyyymmdd}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1500)  # allow late render
        html = page.content()

        ensure_debug_dir()
        if DEBUG:
            page.screenshot(path=f"debug/hamersley_{play_date}.png", full_page=True)
            with open(f"debug/hamersley_{play_date}.html", "w", encoding="utf-8") as f:
                f.write(html)

        browser.close()

    soup = BeautifulSoup(html, "lxml")
    results: List[TeeTime] = []

    def expand_cells(cells) -> List[BeautifulSoup]:
        expanded = []
        for c in cells:
            try:
                colspan = int(c.get("colspan", 1))
            except Exception:
                colspan = 1
            expanded.extend([c] * max(colspan, 1))
        return expanded

    # Pick the table that actually contains both 9 and 18 holes and select links
    target_table = None
    for t in soup.find_all("table"):
        t_text = t.get_text(" ", strip=True).lower()
        if ("18 holes" in t_text) and ("9 holes" in t_text) and ("select" in t_text):
            target_table = t
            break

    if not target_table:
        return results

    # Find a header row containing "18 Holes"
    header_row = None
    for tr in target_table.find_all("tr")[:8]:
        tr_text = tr.get_text(" ", strip=True).lower()
        if ("18 holes" in tr_text) or (("18" in tr_text) and ("hole" in tr_text)):
            header_row = tr
            break

    if not header_row:
        return results

    header_cells = expand_cells(header_row.find_all(["th", "td"]))
    headers = [c.get_text(" ", strip=True).lower() for c in header_cells]

    col_18 = None
    for i, h in enumerate(headers):
        if "18 holes" in h:
            col_18 = i
            break
    if col_18 is None:
        for i, h in enumerate(headers):
            if ("18" in h) and ("hole" in h):
                col_18 = i
                break
    if col_18 is None:
        return results

    time_re = re.compile(r"\b(\d{1,2}:\d{2}\s*(AM|PM))\b", re.IGNORECASE)

    # Rows with Select somewhere (cheaper filter)
    for tr in target_table.find_all("tr"):
        tr_text = tr.get_text(" ", strip=True)
        if "select" not in tr_text.lower():
            continue

        m = time_re.search(tr_text)
        if not m:
            continue

        hhmm = ampm_to_24h(m.group(1))
        if not hhmm or not is_before_or_equal(hhmm, latest):
            continue

        row_cells = expand_cells(tr.find_all(["td", "th"]))
        if col_18 >= len(row_cells):
            continue

        cell_18 = row_cells[col_18]

        # Find a real link in the 18-holes cell
        select_link = cell_18.find("a", string=re.compile(r"select", re.IGNORECASE))
        href = select_link.get("href") if select_link else None
        if not href:
            continue

        booking_url = href if href.startswith("http") else f"{base_url}{href}"

        # Initialize hint early so later code can reference safely
        players_hint = tr_text if tr_text else None

        # --- Validate min_players by opening the slot page ---
        slot_supports_min = True
        slot_players_hint = players_hint

        try:
            with sync_playwright() as p2:
                b2 = p2.chromium.launch(headless=True)
                pg2 = b2.new_page()
                pg2.goto(booking_url, wait_until="domcontentloaded", timeout=60_000)
                pg2.wait_for_timeout(1200)
                slot_html = pg2.content()

                ensure_debug_dir()
                if DEBUG:
                    pg2.screenshot(path=f"debug/hamersley_slot_{play_date}_{hhmm.replace(':','')}.png", full_page=True)
                    with open(f"debug/hamersley_slot_{play_date}_{hhmm.replace(':','')}.html", "w", encoding="utf-8") as f:
                        f.write(slot_html)

                b2.close()

            slot_soup = BeautifulSoup(slot_html, "lxml")
            page_text = slot_soup.get_text(" ", strip=True).lower()

            # Hard reject if page clearly indicates only 1 player
            if min_players >= 2:
                only_one_patterns = [
                    r"\bonly\s*1\s*player\b",
                    r"\b1\s*player\s*only\b",
                    r"\bonly\s*one\s*player\b",
                    r"\bfor\s*1\s*player\b",
                    r"\bsingle\s*player\b",
                ]
                if any(re.search(pat, page_text) for pat in only_one_patterns):
                    slot_supports_min = False

            # Broad dropdown scan: pick the largest option value among selects that look like counts
            selects = slot_soup.find_all("select")
            best_max = None

            for sel in selects:
                attr_blob = " ".join([
                    (sel.get("id") or ""),
                    (sel.get("name") or ""),
                    " ".join(sel.get("class") or []),
                ]).lower()

                option_texts = [opt.get_text(" ", strip=True).lower() for opt in sel.find_all("option")]
                options_blob = " ".join(option_texts)

                # Only treat as player dropdown if "player" appears in select attrs or option text
                if "player" not in attr_blob and "player" not in options_blob:
                    continue

                nums = []
                for txt in option_texts:
                    mm = re.search(r"\b(\d+)\b", txt)
                    if mm:
                        nums.append(int(mm.group(1)))

                if nums:
                    mx = max(nums)
                    if best_max is None or mx > best_max:
                        best_max = mx

            if best_max is not None:
                if best_max < min_players:
                    slot_supports_min = False

                # Only use as a display hint if it looks sane
                # (most tee time slots are max 4; some clubs allow 5 or 6)
                if best_max <= 6:
                    slot_players_hint = f"up to {best_max} players"
                # else: don't overwrite the existing hint

            # Range text (if present)
            m_range = re.search(r"\b(\d+)\s*(?:to|-)\s*(\d+)\s*players?\b", page_text)
            if m_range:
                hi = int(m_range.group(2))
                slot_players_hint = m_range.group(0)
                if hi < min_players:
                    slot_supports_min = False

            m_upto = re.search(r"\bup to\s*(\d+)\s*players?\b", page_text)
            if m_upto:
                hi = int(m_upto.group(1))
                slot_players_hint = m_upto.group(0)
                if hi < min_players:
                    slot_supports_min = False

        except Exception:
            # If the slot page fails to load/transient error, don't block the whole run.
            slot_supports_min = True

        if not slot_supports_min:
            continue

        # Use the hint only if it actually looks like a player hint
        if slot_players_hint and ("player" in slot_players_hint.lower()) and ("up to 20" not in slot_players_hint.lower()):
            players_hint = slot_players_hint
        # otherwise: keep the existing players_hint from the matrix row
        else:
            players_hint = None

        results.append(
            TeeTime(
                course="Hamersley Public Golf Course",
                play_date=play_date,
                tee_time=hhmm,
                players_hint=players_hint,
                booking_url=booking_url,
            )
        )

    # De-dupe by time
    uniq = {}
    for r in results:
        uniq[(r.course, r.play_date, r.tee_time)] = r
    return sorted(uniq.values(), key=lambda x: x.tee_time)


def scrape_miclub_public_calendar(
    course_name: str,
    calendar_url_template: str,
    play_date: str,
    min_players: int,
    latest: time,
) -> List[TeeTime]:
    url = calendar_url_template.format(date=play_date)
    results: List[TeeTime] = []
    final_url = url

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1500)

        ensure_debug_dir()
        safe = re.sub(r"[^a-z0-9]+", "_", course_name.lower()).strip("_")

        # --- DEBUG: grid page ---
        if DEBUG:
            page.screenshot(path=f"debug/{safe}_grid_{play_date}.png", full_page=True)
            with open(f"debug/{safe}_grid_{play_date}.html", "w", encoding="utf-8") as f:
                f.write(page.content())

        # -------- CLICK THROUGH TO THE DAY TIMESHEET (MiClub) --------
        clicked = False

        # Strategy A: "18 Holes" row -> click a price
        try:
            row18 = page.locator("tr", has_text=re.compile(r"\b18\s*Holes\b", re.IGNORECASE))
            if row18.count() == 0:
                row18 = page.locator(":is(tr,div)", has_text=re.compile(r"\b18\s*Holes\b", re.IGNORECASE))

            if row18.count() > 0:
                price_cells = row18.first.locator("text=/\\$\\s*\\d+(?:\\.\\d{2})?/")
                if price_cells.count() > 0:
                    price_cells.first.click(timeout=5_000)
                    clicked = True
        except Exception:
            clicked = False

        # Strategy B: any visible price
        if not clicked:
            try:
                page.locator("text=/\\$\\s*\\d+(?:\\.\\d{2})?/").first.click(timeout=5_000)
                clicked = True
            except Exception:
                clicked = False

        # Strategy C: any obvious link/button
        if not clicked:
            try:
                page.locator("table a, table button, a, button").first.click(timeout=5_000)
                clicked = True
            except Exception:
                clicked = False

        if not clicked:
            browser.close()
            return results

        # wait for navigation or in-place update
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        page.wait_for_timeout(1200)

        if len(page.context.pages) > 1:
            page = page.context.pages[-1]
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            page.wait_for_timeout(800)

        final_url = page.url

        # -------- SWITCH TO REAL TIMESHEET (PAGE OR IFRAME) --------
        ts_ctx, ts_kind = get_timesheet_context(page)

        # give it another beat if the first pass didn’t find the right context
        if ts_ctx is page:
            page.wait_for_timeout(1200)
            ts_ctx, ts_kind = get_timesheet_context(page)

        # DEBUG: screenshot + correct HTML (page vs frame)
        if DEBUG:
            try:
                page.screenshot(path=f"debug/{safe}_times_{play_date}.png", full_page=True)
            except Exception:
                pass

            try:
                with open(f"debug/{safe}_times_{play_date}.html", "w", encoding="utf-8") as f:
                    if ts_kind == "frame":
                        f.write(ts_ctx.content())
                    else:
                        f.write(page.content())
            except Exception:
                pass

        # -------- PARSE TIMESHEET HTML (don’t rely on :visible / clickable) --------
        try:
            ts_html = ts_ctx.content() if ts_kind == "frame" else page.content()
        except Exception:
            ts_html = page.content()

        browser.close()

    soup = BeautifulSoup(ts_html, "lxml")

    candidates = []          # <-- ensure it's always defined
    found_times: List[str] = []

    # These tend to appear when there are genuinely no times
    page_text = soup.get_text(" ", strip=True).lower()
    if "no bookings available" in page_text or "no booking available" in page_text:
        return results

    time_re_ampm = re.compile(r"\b(\d{1,2}:\d{2}\s*(AM|PM))\b", re.IGNORECASE)
    time_re_24h = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")

    # “unavailable” signals vary by theme, but these are common
    unavailable_re = re.compile(r"\b(unavailable|booked|sold\s*out|full|closed)\b", re.IGNORECASE)
    bad_class_re = re.compile(r"(unavailable|disabled|booked|soldout|full|closed)", re.IGNORECASE)

    def element_looks_bookable(node) -> bool:
    try:
        candidates = soup.select(".time-wrapper") or []
    except Exception:
        candidates = []

    if not candidates:
        try:
            candidates = [t.parent for t in soup.find_all(string=time_re_ampm)]
            candidates = [c for c in candidates if c]
        except Exception:
            candidates = []

    found_times: List[str] = []

    for node in candidates:
        block_text = node.get_text("\n", strip=True)  # keep line breaks, helps debugging
        low = block_text.lower()

        # Extract the time
        m = time_re_ampm.search(block_text)
        if m:
            hhmm = ampm_to_24h(m.group(1))
        else:
            m2 = time_re_24h.search(block_text)
            hhmm = m2.group(0) if m2 else None

        if not hhmm or not is_before_or_equal(hhmm, latest):
            continue

        # ✅ Availability rule for your exact markup:
        # include if at least one "Available" and no "Taken"
        has_available = "available" in low
        has_taken = "taken" in low

        if has_available and not has_taken:
            found_times.append(hhmm)

    for hhmm in sorted(set(found_times)):
        results.append(
            TeeTime(
                course=course_name,
                play_date=play_date,
                tee_time=hhmm,
                players_hint=None,
                booking_url=final_url,
            )
        )

    return sorted(results, key=lambda x: x.tee_time)

    browser.close()

def render_markdown(all_results: List[TeeTime], play_date: str, min_players: int, latest_time: str) -> str:
    if not all_results:
        return (
            f"# Tee time lookup\n\n"
            f"- Date: **{play_date}**\n"
            f"- Filter: **{min_players}+ players**, **before {latest_time}**\n"
            f"- Tip: right-click a link (or Ctrl/Cmd-click) to open it in a new tab.\n\n"
            f"Nothing matched. Could be full, or one of the booking pages changed.\n"
        )

    lines = [
        "# Tee time lookup",
        "",
        f"- Date: **{play_date}**",
        f"- Filter: **{min_players}+ players**, **before {latest_time}**",
        "- Tip: right-click a link (or Ctrl/Cmd-click) to open it in a new tab.",
        "",
    ]

    by_course = {}
    for r in all_results:
        by_course.setdefault(r.course, []).append(r)

    for course, items in by_course.items():
        lines += [f"## {course}", ""]
        for r in items:
            hint = f" ({r.players_hint})" if r.players_hint else ""
            lines.append(f"- **{r.tee_time}**{hint}  [open booking page]({r.booking_url})")
        lines.append("")

    return "\n".join(lines)


def main():
    play_date = os.environ.get("PLAY_DATE", "").strip()
    if not play_date:
        raise SystemExit("PLAY_DATE env var missing.")

    min_players = int(os.environ.get("MIN_PLAYERS", "2").strip())
    latest_time_str = os.environ.get("LATEST_TIME", "10:00").strip()
    latest = parse_hhmm_24(latest_time_str)

    miclub_courses = [
        (
            "Collier Park Golf Course",
            "https://bookings.collierparkgolf.com.au/guests/bookings/ViewPublicCalendar.msp?mobile=true&selectedDate={date}",
        ),
        (
            "Marangaroo Golf Course",
            "https://marangaroo.miclub.com.au/guests/bookings/ViewPublicCalendar.msp?mobile=true&selectedDate={date}",
        ),
        (
            "Whaleback Golf Course",
            "https://www.whalebackgolfcourse.com.au/guests/bookings/ViewPublicCalendar.msp?booking_resource_id=3000000&mobile=true&selectedDate={date}",
        ),
    ]

    all_results: List[TeeTime] = []

    # MiClub (best effort for now)
    for name, template in miclub_courses:
        try:
            all_results += scrape_miclub_public_calendar(name, template, play_date, min_players, latest)
        except Exception as e:
            all_results.append(
                TeeTime(
                    course=name,
                    play_date=play_date,
                    tee_time="",
                    players_hint=f"ERROR: {e}",
                    booking_url=template.format(date=play_date),
                )
            )

    # Hamersley Quick18
    try:
        all_results += scrape_quick18_hamersley(play_date, min_players, latest)
    except Exception as e:
        all_results.append(
            TeeTime(
                course="Hamersley Public Golf Course",
                play_date=play_date,
                tee_time="",
                players_hint=f"ERROR: {e}",
                booking_url=f"https://hamersley.quick18.com/teetimes/searchmatrix?teedate={play_date.replace('-', '')}",
            )
        )

    good = [r for r in all_results if r.tee_time]
    bad = [r for r in all_results if not r.tee_time]

    good_sorted = sorted(good, key=lambda x: (x.course, x.tee_time))
    md = render_markdown(good_sorted + bad, play_date, min_players, latest_time_str)

    with open("tee_time_summary.md", "w", encoding="utf-8") as f:
        f.write(md)


if __name__ == "__main__":
    main()
