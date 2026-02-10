import os
import re
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
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "lxml")
    results: List[TeeTime] = []
    time_re = re.compile(r"^\s*\d{1,2}:\d{2}\s*(AM|PM)\s*$", re.IGNORECASE)

    for tr in soup.find_all("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if not tds:
            continue

        row_text = " ".join(tds).lower()

        # Drop obvious 9-hole labels (more variants)
        nine_markers = [
            "9 hole", "9 holes", "9-hole", "9holes", "9h",
            "front 9", "back 9", "front nine", "back nine",
            "nine hole", "nine holes"
        ]
        if any(m in row_text for m in nine_markers):
            continue

        # Keep only rows that clearly indicate 18 holes
        eighteen_markers = [
            "18 hole", "18 holes", "18-hole", "18holes", "18h",
            "full 18", "full course"
        ]
        if not any(m in row_text for m in eighteen_markers):
            continue

        time_cell = None
        for cell in tds:
            if time_re.match(cell):
                time_cell = cell
                break
        if not time_cell:
            continue

        hhmm = ampm_to_24h(time_cell)
        if not hhmm or not is_before_or_equal(hhmm, latest):
            continue

        players_hint = None
        for cell in tds:
            if "player" in cell.lower():
                players_hint = cell
                break

        if not looks_like_players_ok(players_hint, min_players):
            continue

        link = tr.find("a", string=re.compile(r"select", re.IGNORECASE))
        booking_url = url
        if link and link.get("href"):
            href = link["href"]
            booking_url = href if href.startswith("http") else f"https://hamersley.quick18.com{href}"

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
