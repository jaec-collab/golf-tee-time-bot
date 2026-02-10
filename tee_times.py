import os
import re
DEBUG = os.environ.get("DEBUG", "false").strip().lower() == "true"

def ensure_debug_dir():
    if DEBUG:
        os.makedirs("debug", exist_ok=True)
from dataclasses import dataclass
from datetime import datetime, time
from typing import List, Optional

from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from playwright.sync_api import sync_playwright


@dataclass
class TeeTime:
    course: str
    play_date: str
    tee_time: str
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


def looks_like_players_ok(players_hint: Optional[str], min_players: int) -> bool:
    if not players_hint:
        return True

    s = players_hint.lower()
    nums = [int(x) for x in re.findall(r"\d+", s)]
    if not nums:
        return True

    if "to" in s and len(nums) >= 2:
        lo, hi = nums[0], nums[1]
        return hi >= min_players

    if "or" in s and len(nums) >= 2:
        return max(nums) >= min_players

    return max(nums) >= min_players


def scrape_quick18_hamersley(play_date: str, min_players: int, latest: time) -> List[TeeTime]:
    yyyymmdd = play_date.replace("-", "")
    url = f"https://hamersley.quick18.com/teetimes/searchmatrix?teedate={yyyymmdd}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        # Quick18 sometimes renders a bit after DOMContentLoaded
        page.wait_for_timeout(1500)

        html = page.content()

        try:
            ensure_debug_dir()
            if DEBUG:
                page.screenshot(path=f"debug/hamersley_{play_date}.png", full_page=True)
                with open(f"debug/hamersley_{play_date}.html", "w", encoding="utf-8") as f:
                    f.write(html)
        except Exception:
            pass

        browser.close()

    soup = BeautifulSoup(html, "lxml")
    results: List[TeeTime] = []

    def expand_cells(cells) -> List[BeautifulSoup]:
        """Expand a row's cells by colspan so indexes align with logical columns."""
        expanded = []
        for c in cells:
            colspan = 1
            try:
                colspan = int(c.get("colspan", 1))
            except Exception:
                colspan = 1
            expanded.extend([c] * max(colspan, 1))
        return expanded

    # Pick the table that actually looks like the tee time matrix
    candidate_tables = soup.find_all("table")
    target_table = None
    for t in candidate_tables:
        t_text = t.get_text(" ", strip=True).lower()
        if ("18 holes" in t_text) and ("9 holes" in t_text) and ("select" in t_text):
            target_table = t
            break

    if not target_table:
        return results

    # Find the header row that contains "9 Holes" / "18 Holes"
    header_row = None
    for tr in target_table.find_all("tr")[:6]:
        tr_text = tr.get_text(" ", strip=True).lower()
        if ("18 holes" in tr_text) or ("9 holes" in tr_text):
            header_row = tr

    if not header_row:
        return results

    header_cells = expand_cells(header_row.find_all(["th", "td"]))
    headers = [c.get_text(" ", strip=True).lower() for c in header_cells]

    def find_col_idx_contains(needle: str) -> Optional[int]:
        for i, h in enumerate(headers):
            if needle in h:
                return i
        return None

    col_18 = find_col_idx_contains("18 holes")
    if col_18 is None:
        # Some variants show "18 Hole" instead
        for i, h in enumerate(headers):
            if ("18" in h) and ("hole" in h):
                col_18 = i
                break

    if col_18 is None:
        return results

    time_re = re.compile(r"\b(\d{1,2}:\d{2}\s*(AM|PM))\b", re.IGNORECASE)

    # Data rows: only those that have a Select somewhere
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
        select_link = cell_18.find("a", string=re.compile(r"select", re.IGNORECASE))

        # Sometimes it's a button/input, not an <a>
        if not select_link:
            select_link = cell_18.find("a") or cell_18.find("button") or cell_18.find("input")

        href = None
        if select_link and select_link.get("href"):
            href = select_link["href"]

        # If there's no link href, still treat it as not actionable for now
        if not href:
            continue

        booking_url = href if href.startswith("http") else f"https://hamersley.quick18.com{href}"
        
        # NEW: open the slot page and confirm it supports min_players
        slot_supports_min = True
        slot_players_hint = players_hint

        try:
            with sync_playwright() as p2:
                b2 = p2.chromium.launch(headless=True)
                pg2 = b2.new_page()
                pg2.goto(booking_url, wait_until="domcontentloaded", timeout=60_000)
                pg2.wait_for_timeout(1200)
                slot_html = pg2.content()
                b2.close()

            slot_soup = BeautifulSoup(slot_html, "lxml")
            page_text = slot_soup.get_text(" ", strip=True).lower()

            # Common patterns: "1 player", "2 players", "1 to 4 players", dropdown options, etc.
            # If it explicitly mentions only 1 player, reject for min_players >= 2
            if min_players >= 2 and re.search(r"\b1\s+player\b", page_text) and not re.search(r"\b2\s+player", page_text):
                slot_supports_min = False

            # If we can find a "to X players" hint, use it
            m_range = re.search(r"\b(\d+)\s*(?:to|-)\s*(\d+)\s*players?\b", page_text)
            if m_range:
                hi = int(m_range.group(2))
                slot_supports_min = hi >= min_players
                slot_players_hint = m_range.group(0)

            # Or “Up to X players”
            m_upto = re.search(r"\bup to\s*(\d+)\s*players?\b", page_text)
            if m_upto:
                hi = int(m_upto.group(1))
                slot_supports_min = hi >= min_players
                slot_players_hint = m_upto.group(0)

            # Or explicit list of player counts (e.g., dropdown)
            # If we see any number >= min_players next to the word player(s), treat as ok.
            if not m_range and not m_upto:
                nums = [int(x) for x in re.findall(r"\b(\d+)\s*players?\b", page_text)]
                if nums:
                    slot_supports_min = max(nums) >= min_players
                    slot_players_hint = f"players up to {max(nums)}"

        except Exception:
            # If validation fails due to a transient page issue, keep the slot (conservative)
            slot_supports_min = True

        if not slot_supports_min:
            continue

        # Prefer the more specific hint found on the slot page
        players_hint = slot_players_hint
        players_hint = tr_text if "player" in tr_text.lower() else None
        if not looks_like_players_ok(players_hint, min_players):
            continue

        results.append(
            TeeTime(
                course="Hamersley Public Golf Course",
                play_date=play_date,
                tee_time=hhmm,
                players_hint=players_hint,
                booking_url=booking_url,
            )
        )

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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        for product_label in ["18 Holes", "18 Hole", "All"]:
            locator = page.get_by_text(product_label, exact=False)
            if locator.count() > 0:
                try:
                    locator.first.click(timeout=3_000)
                    page.wait_for_timeout(1_000)
                    break
                except Exception:
                    pass

        html = page.content()
        ensure_debug_dir()
        if DEBUG:
            safe = re.sub(r"[^a-z0-9]+", "_", course_name.lower()).strip("_")
            page.screenshot(path=f"debug/{safe}_{play_date}.png", full_page=True)
            with open(f"debug/{safe}_{play_date}.html", "w", encoding="utf-8") as f:
                f.write(html)
        browser.close()

    soup = BeautifulSoup(html, "lxml")
    time_re = re.compile(r"\b(\d{1,2}:\d{2}\s*(AM|PM))\b", re.IGNORECASE)

    text_nodes = soup.find_all(string=time_re)
    for node in text_nodes:
        m = time_re.search(str(node))
        if not m:
            continue
        t12 = m.group(1)
        hhmm = ampm_to_24h(t12)
        if not hhmm or not is_before_or_equal(hhmm, latest):
            continue

        players_hint = None
        parent_text = node.parent.get_text(" ", strip=True) if node.parent else ""
        if "player" in parent_text.lower():
            players_hint = parent_text

        if not looks_like_players_ok(players_hint, min_players):
            continue

        results.append(
            TeeTime(
                course=course_name,
                play_date=play_date,
                tee_time=hhmm,
                players_hint=players_hint,
                booking_url=url,
            )
        )

    uniq = {}
    for r in results:
        uniq[(r.course, r.play_date, r.tee_time)] = r
    return sorted(uniq.values(), key=lambda x: x.tee_time)


def render_markdown(all_results: List[TeeTime], play_date: str, min_players: int, latest_time: str) -> str:
    if not all_results:
        return (
            f"# Tee time lookup\n\n"
            f"- Date: **{play_date}**\n"
            f"- Filter: **{min_players}+ players**, **before {latest_time}**\n\n"
            f"Nothing matched. Could be full, or a site layout changed.\n"
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

    for name, template in miclub_courses:
        try:
            all_results += scrape_miclub_public_calendar(
                name, template, play_date, min_players, latest
            )
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
