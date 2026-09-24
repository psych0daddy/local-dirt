#!/usr/bin/env python3
"""
MetroLocal collector
--------------------
Purpose:
    Collect public information from a configurable list of local websites and
    output one AI-friendly JSON file.

Scheduling design:
    * Run this script every 15 minutes using Windows Task Scheduler, cron, etc.
    * Each source has its own refresh interval.
    * A source is fetched only when its interval has elapsed.
    * Sports/weather can refresh every 15 minutes.
    * News can refresh every 30 minutes.
    * Councils/agendas can refresh every several hours.
    * All intervals are editable in config.json.

Editorial design:
    * Python gathers.
    * Python does NOT decide what is important enough to post.
    * "southside" sources are gathered broadly.
    * "broad" sources are filtered by configured Southside keywords.
    * Persistent state prevents re-outputting the same item forever.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

# Windows/corporate SSL interception fix.
# Antivirus and network filters re-sign HTTPS traffic with their own certificate.
# Python's bundled CA list does not know it, but the Windows certificate store
# does -- truststore makes Python use the OS store instead.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import requests
from bs4 import BeautifulSoup
import feedparser
from pypdf import PdfReader

DATE_META_KEYS = (
    "article:published_time", "date", "datePublished", "datepublished",
    "pubdate", "publish-date", "published_time", "og:published_time",
)

def now_local():
    return datetime.now(timezone.utc).astimezone()

def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")

def clean_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def canonicalize_url(url: str) -> str:
    url = urldefrag(url)[0]
    return url.rstrip("/") or url


# --- Recency filtering ------------------------------------------------------
# Two ways an item can be stale: a published date in the past, or a date written
# into the title/text of a document (agendas name their own meeting date).
_MONTHS = ("january february march april may june july august september "
           "october november december").split()

def parse_any_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(value)
        return d.replace(tzinfo=None) if d else None
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(str(value)[:len(fmt) + 6].strip(), fmt)
        except Exception:
            continue
    return None


def date_in_text(text: str) -> Optional[datetime]:
    """Pull a meeting-style date out of a title or document body."""
    m = re.search(
        r"(" + "|".join(_MONTHS) + r")\w*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)",
        text[:400], re.I)
    if m:
        try:
            return datetime(int(m.group(3)), _MONTHS.index(m.group(1).lower()) + 1,
                            int(m.group(2)))
        except Exception:
            return None
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d\d)\b", text[:400])
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except Exception:
            return None
    return None


def is_stale(published, title: str, text: str, max_age_days: int) -> bool:
    """True when we can PROVE the item is old. No date found means keep it --
    most government pages carry no date at all, and dropping those would throw
    away the bulk of the useful material."""
    if max_age_days <= 0:
        return False
    cutoff = datetime.now() - timedelta(days=max_age_days)
    d = parse_any_date(published) or date_in_text(title) or date_in_text(text)
    if d is None:
        return False
    # Future dates are upcoming meetings -- exactly what we want.
    return d < cutoff


def stable_id(source_name: str, url: str, title: str, text: str = "") -> str:
    # Content hash is included so that a page whose CONTENT changes is treated
    # as new material, even when its URL and title stay the same. This is the
    # difference between "have I seen this page?" and "has this page changed?"
    content_sig = hashlib.sha256(clean_space(text).encode("utf-8", "ignore")).hexdigest()[:16]
    raw = f"{source_name}|{canonicalize_url(url)}|{clean_space(title)}|{content_sig}".encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()[:24]

def source_key(source: dict) -> str:
    raw = f"{source.get('name','')}|{canonicalize_url(source.get('url',''))}".encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()[:20]

def same_site(a: str, b: str) -> bool:
    aa = urlparse(a).netloc.lower().removeprefix("www.")
    bb = urlparse(b).netloc.lower().removeprefix("www.")
    return aa == bb

def term_match(text: str, terms: Iterable[str]) -> List[str]:
    hay = clean_space(text).casefold()
    return [term for term in terms if term.casefold() in hay]

def looks_ignored(url: str, anchor: str, ignore_terms: List[str]) -> bool:
    s = f"{url} {anchor}".casefold()
    return any(t.casefold() in s for t in ignore_terms)

def looks_relevant_link(url: str, anchor: str, crawl_terms: List[str]) -> bool:
    s = f"{url} {anchor}".casefold()
    return any(t.casefold() in s for t in crawl_terms)

def parse_iso_dt(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def extract_date(soup: BeautifulSoup) -> Optional[str]:
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").strip()
        if key in DATE_META_KEYS:
            val = meta.get("content")
            if val:
                return clean_space(val)
    t = soup.find("time")
    if t:
        return clean_space(t.get("datetime") or t.get_text(" ", strip=True))
    return None

def extract_title(soup: BeautifulSoup, fallback: str) -> str:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return clean_space(og["content"])
    h1 = soup.find("h1")
    if h1:
        return clean_space(h1.get_text(" ", strip=True))
    if soup.title:
        return clean_space(soup.title.get_text(" ", strip=True))
    return fallback

def extract_text(soup: BeautifulSoup, max_chars: int) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "form"]):
        tag.decompose()

    candidate = soup.find("article") or soup.find("main") or soup.body or soup
    parts = []
    for tag in candidate.find_all(["h1","h2","h3","h4","p","li","td","th"], recursive=True):
        txt = clean_space(tag.get_text(" ", strip=True))
        if len(txt) >= 2:
            parts.append(txt)

    seen = set()
    cleaned = []
    for p in parts:
        key = p.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(p)

    return "\n".join(cleaned)[:max_chars]

def extract_links(soup: BeautifulSoup, base_url: str, limit: int) -> List[Tuple[str, str]]:
    out = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = canonicalize_url(urljoin(base_url, href))
        if not url.startswith(("http://", "https://")):
            continue
        anchor = clean_space(a.get_text(" ", strip=True))
        key = (url, anchor)
        if key in seen:
            continue
        seen.add(key)
        out.append((url, anchor))
        if len(out) >= limit:
            break
    return out

def discover_feed_urls(soup: BeautifulSoup, base_url: str) -> List[str]:
    feeds = []
    for link in soup.find_all("link", href=True):
        typ = (link.get("type") or "").lower()
        if "rss" in typ or "atom" in typ:
            feeds.append(urljoin(base_url, link["href"]))
    return list(dict.fromkeys(feeds))

class Collector:
    def __init__(
        self,
        config_path: Path,
        use_state: bool = True,
        include_seen: bool = False,
        force_refresh: bool = False,
    ):
        self.config_path = config_path
        self.root = config_path.resolve().parent
        self.cfg = json.loads(config_path.read_text(encoding="utf-8"))
        self.use_state = use_state
        self.include_seen = include_seen
        self.force_refresh = force_refresh

        req = self.cfg["request"]
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": req["user_agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.5",
        })
        self.timeout = req["timeout_seconds"]
        self.sleep = req["sleep_seconds_between_requests"]

        self.limits = self.cfg["limits"]
        self.keywords = self.cfg["broad_source_keywords"]
        self.crawl_terms = self.cfg["crawl_link_terms"]
        self.ignore_terms = self.cfg["ignore_link_terms"]

        scheduler = self.cfg.get("scheduler", {})
        self.refresh_defaults = scheduler.get("default_refresh_minutes_by_category", {})
        self.refresh_fallback = int(scheduler.get("fallback_refresh_minutes", 60))
        self.last_checked_path = self.root / scheduler.get("last_checked_file", "last_checked.json")
        self.last_checked = {}
        if self.last_checked_path.exists():
            try:
                self.last_checked = json.loads(self.last_checked_path.read_text(encoding="utf-8"))
            except Exception:
                self.last_checked = {}

        self.state_path = self.root / self.cfg["state_file"]
        self.seen_ids: Set[str] = set()
        if self.use_state and self.state_path.exists():
            try:
                self.seen_ids = set(json.loads(self.state_path.read_text(encoding="utf-8")))
            except Exception:
                pass

        # robots.txt: cached one parser per domain, fetched on first contact.
        self.respect_robots = bool(req.get("respect_robots_txt", True))
        # Domains exempt from robots.txt. These are public bodies whose
        # robots.txt is a stock CMS default, not a deliberate policy, and whose
        # records are public by statute. News publishers are never listed here.
        self.robots_exempt = [d.lower() for d in req.get("robots_exempt_domains", [])]
        self.robots_cache = {}
        self.robots_blocked = []

        self.max_age_days = int(self.limits.get("max_item_age_days", 0))
        self.stale_skipped = 0

        self.items = []
        self.errors = []
        self.visited = set()
        self.skipped_sources = []
        self.checked_sources = []

    def refresh_minutes_for(self, source: dict) -> int:
        if "refresh_minutes" in source:
            return int(source["refresh_minutes"])
        category = source.get("category", "")
        return int(self.refresh_defaults.get(category, self.refresh_fallback))

    def is_due(self, source: dict) -> Tuple[bool, Optional[float]]:
        if self.force_refresh:
            return True, None

        key = source_key(source)
        last_str = self.last_checked.get(key)
        if not last_str:
            return True, None

        last_dt = parse_iso_dt(last_str)
        if not last_dt:
            return True, None

        elapsed_minutes = (now_local() - last_dt.astimezone()).total_seconds() / 60.0
        refresh = self.refresh_minutes_for(source)
        return elapsed_minutes >= refresh, elapsed_minutes

    def mark_checked(self, source: dict) -> None:
        self.last_checked[source_key(source)] = now_iso()

    def robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlparse(url)
        host = parts.netloc.lower()
        if any(host == d or host.endswith("." + d) for d in self.robots_exempt):
            return True
        base = f"{parts.scheme}://{parts.netloc}"
        rp = self.robots_cache.get(base)
        if rp is None:
            rp = RobotFileParser()
            rp.set_url(urljoin(base, "/robots.txt"))
            try:
                rp.read()
            except Exception:
                # No reachable robots.txt is treated as "no restrictions stated".
                rp = None
            self.robots_cache[base] = rp
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.session.headers.get("User-Agent", "*"), url)
        except Exception:
            return True

    def request(self, url: str) -> Optional[requests.Response]:
        if not self.robots_allows(url):
            self.robots_blocked.append(url)
            return None
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            r.raise_for_status()
            time.sleep(self.sleep)
            return r
        except Exception as e:
            self.errors.append({"url": url, "error": f"{type(e).__name__}: {e}"})
            return None

    def add_item(
        self,
        source: dict,
        url: str,
        title: str,
        text: str,
        published: Optional[str] = None,
        matched_keywords: Optional[List[str]] = None,
        item_type: str = "webpage",
    ) -> None:
        title = clean_space(title) or source["name"]

        # Per-source override: documents published monthly or bimonthly (board
        # minutes, budget reports) can't live under a 5-day window -- the whole
        # point is that they appear rarely and stay relevant.
        max_age = source.get("max_item_age_days", self.max_age_days)
        if is_stale(published, title, text or "", int(max_age)):
            self.stale_skipped += 1
            return


        # Broad/news sources are capped hard: headline + short excerpt only.
        # We link to publishers, we do not reproduce their reporting.
        if source.get("scope") == "broad":
            cap = int(self.limits.get("max_text_chars_broad_source", 400))
        else:
            cap = int(self.limits["max_text_chars_per_item"])
        # Board minutes and similar documents bury the interesting material deep
        # -- the routine reports come first. A per-source cap keeps those whole
        # without inflating every ordinary page.
        cap = int(source.get("max_text_chars", cap))
        text = (text or "")[:cap]

        item_id = stable_id(source["name"], url, title, text)

        if self.use_state and item_id in self.seen_ids and not self.include_seen:
            return

        self.items.append({
            "id": item_id,
            "source": source["name"],
            "source_scope": source["scope"],
            "category": source.get("category"),
            "item_type": item_type,
            "title": title,
            "url": canonicalize_url(url),
            "published": published,
            "fetched_at": now_iso(),
            "matched_keywords": matched_keywords or [],
            "text": text,
        })
        self.seen_ids.add(item_id)

    def parse_pdf(self, source: dict, url: str, data: bytes, hint_title: str = "") -> None:
        try:
            reader = PdfReader(BytesIO(data))
            pages = []
            for page in reader.pages[:self.limits["max_pdf_pages"]]:
                pages.append(page.extract_text() or "")
            text = clean_space("\n".join(pages))
            matches = term_match(f"{hint_title} {text}", self.keywords) if source["scope"] == "broad" else []
            if source["scope"] == "broad" and not matches:
                return
            self.add_item(
                source, url, hint_title or "PDF document", text,
                matched_keywords=matches, item_type="pdf"
            )
        except Exception as e:
            self.errors.append({"url": url, "error": f"PDF parse error: {e}"})

    def parse_feed(self, source: dict, feed_url: str, raw_text: str = "") -> None:
        try:
            # Parse text we already fetched when we have it. Passing the URL back
            # to feedparser would refetch without our session headers, which the
            # publishers that need those headers will refuse.
            feed = feedparser.parse(raw_text) if raw_text else feedparser.parse(feed_url)
            for entry in feed.entries[:50]:
                title = clean_space(entry.get("title", ""))
                link = entry.get("link") or feed_url
                summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
                published = entry.get("published") or entry.get("updated")
                matches = term_match(f"{title} {summary} {link}", self.keywords)

                if source["scope"] == "broad" and not matches:
                    continue

                self.add_item(
                    source, link, title, clean_space(summary),
                    published, matches, "feed"
                )
        except Exception as e:
            self.errors.append({"url": feed_url, "error": f"Feed parse error: {e}"})

    def process_page(self, source: dict, url: str, hint_title: str = "") -> List[Tuple[str, str]]:
        url = canonicalize_url(url)
        if url in self.visited:
            return []
        self.visited.add(url)

        r = self.request(url)
        if not r:
            return []

        content_type = (r.headers.get("content-type") or "").lower()

        if "pdf" in content_type or r.url.lower().endswith(".pdf"):
            self.parse_pdf(source, r.url, r.content, hint_title)
            return []

        # The URL may itself BE a feed (RSS/Atom), not an HTML page that links to
        # one. Detect that up front -- otherwise the XML gets handed to the HTML
        # parser, yields no article text, and lands as one empty item.
        looks_like_feed = (
            bool(source.get("is_feed"))
            or "rss" in content_type or "atom" in content_type
            or re.search(r"<(rss|feed)[\s>]", r.text[:2000], re.I) is not None
        )
        if looks_like_feed:
            self.parse_feed(source, r.url, r.text)
            return []

        if "html" not in content_type and "xml" not in content_type and not r.text.lstrip().startswith("<"):
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        title = extract_title(soup, hint_title or source["name"])
        text = extract_text(soup, self.limits["max_text_chars_per_item"])
        matches = term_match(f"{title}\n{text}\n{r.url}", self.keywords)

        if source["scope"] == "southside" or matches:
            self.add_item(
                source, r.url, title, text,
                extract_date(soup), matches, "listing_or_page"
            )

        for feed_url in discover_feed_urls(soup, r.url):
            self.parse_feed(source, feed_url)

        return extract_links(soup, r.url, self.limits["max_links_from_page"])

    def collect_source(self, source: dict) -> None:
        max_pages = int(source.get("max_pages", self.limits["max_pages_per_source"]))
        seed = canonicalize_url(source["url"])
        q = deque([(seed, source["name"], 0)])
        queued = {seed}
        pages = 0

        while q and pages < max_pages:
            url, anchor, depth = q.popleft()
            links = self.process_page(source, url, anchor)
            pages += 1

            if depth >= int(source.get("crawl_depth", 1)):
                continue

            for link, link_anchor in links:
                if link in queued:
                    continue
                if looks_ignored(link, link_anchor, self.ignore_terms):
                    continue

                internal = same_site(seed, link)
                is_pdf = link.lower().split("?")[0].endswith(".pdf")

                if source["scope"] == "broad":
                    # Broad sources are only followed when the link text/URL
                    # already contains a configured Southside term.
                    matches = term_match(f"{link_anchor} {link}", self.keywords)
                    if not matches:
                        continue
                else:
                    # Southside-specific sources are allowed to gather broadly,
                    # while avoiding irrelevant navigation pages.
                    if not is_pdf and not looks_relevant_link(
                        link, link_anchor, self.crawl_terms
                    ):
                        continue

                if (
                    internal
                    or is_pdf
                    or any(
                        x in link.lower()
                        for x in ("boarddocs.com", "municodemeetings.com", "onbase.")
                    )
                ):
                    queued.add(link)
                    q.append((link, link_anchor, depth + 1))

    def run(self) -> dict:
        started = now_iso()

        for i, source in enumerate(self.cfg["sources"], start=1):
            refresh = self.refresh_minutes_for(source)
            due, elapsed = self.is_due(source)

            if not due:
                self.skipped_sources.append({
                    "source": source["name"],
                    "refresh_minutes": refresh,
                    "minutes_since_last_check": round(elapsed or 0, 1),
                })
                print(
                    f"[{i}/{len(self.cfg['sources'])}] SKIP "
                    f"{source['name']} (checked {elapsed:.1f} min ago; "
                    f"refresh={refresh} min)"
                )
                continue

            print(
                f"[{i}/{len(self.cfg['sources'])}] CHECK "
                f"{source['name']} (refresh={refresh} min)"
            )

            try:
                self.collect_source(source)
                self.mark_checked(source)
                self.checked_sources.append({
                    "source": source["name"],
                    "refresh_minutes": refresh,
                    "checked_at": self.last_checked[source_key(source)],
                })
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self.errors.append({
                    "url": source["url"],
                    "error": f"Source failure: {type(e).__name__}: {e}",
                })
                # Do not mark failed sources as checked. They will retry next master run.

        unique = {}
        for item in self.items:
            key = (item["source"], item["url"], item["title"].casefold())
            unique[key] = item
        items = list(unique.values())

        result = {
            "project": self.cfg["project_name"],
            "started_at": started,
            "finished_at": now_iso(),
            "item_count": len(items),
            "error_count": len(self.errors),
            "checked_source_count": len(self.checked_sources),
            "skipped_source_count": len(self.skipped_sources),
            "notes": [
                "Python collected the material; it did not decide newsworthiness.",
                "The master script may run every 15 minutes, but each source has its own refresh interval.",
                "Broad sources were filtered only by configured Southside keywords.",
                "Some websites may block automated requests, render content with JavaScript, or require source-specific handling.",
                "Use source and URL fields when AI drafts a post so the original source can be credited/linked when appropriate.",
            ],
            "stale_skipped_count": self.stale_skipped,
            "robots_blocked_count": len(self.robots_blocked),
            "robots_blocked": self.robots_blocked[:50],
            "checked_sources": self.checked_sources,
            "skipped_sources": self.skipped_sources,
            "items": sorted(
                items,
                key=lambda x: (x.get("published") or "", x["fetched_at"]),
                reverse=True,
            ),
            "errors": self.errors,
        }

        out_dir = self.root / self.cfg["output_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"southside_digest_{stamp}.json"
        latest_path = out_dir / "latest.json"

        encoded = json.dumps(result, indent=2, ensure_ascii=False)
        out_path.write_text(encoded, encoding="utf-8")
        latest_path.write_text(encoded, encoding="utf-8")

        if self.use_state:
            self.state_path.write_text(
                json.dumps(sorted(self.seen_ids), indent=2),
                encoding="utf-8",
            )

        self.last_checked_path.write_text(
            json.dumps(self.last_checked, indent=2),
            encoding="utf-8",
        )

        print(f"\nWrote {len(items)} new items to: {out_path}")
        print(f"Latest copy: {latest_path}")
        print(f"Checked sources: {len(self.checked_sources)}")
        print(f"Skipped (not due): {len(self.skipped_sources)}")
        if self.stale_skipped:
            print(f"Skipped as older than {self.max_age_days} days: {self.stale_skipped}")
        if self.robots_blocked:
            print(f"Skipped by robots.txt: {len(self.robots_blocked)} URL(s)")

        if self.errors:
            print(
                f"Completed with {len(self.errors)} fetch/parse errors. "
                "See the JSON errors section."
            )

        return result

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json", help="Path to config JSON")
    ap.add_argument(
        "--all",
        action="store_true",
        help="Include previously seen items in output",
    )
    ap.add_argument(
        "--no-state",
        action="store_true",
        help="Do not read/write seen-item state",
    )
    ap.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore per-source refresh timing and check every source now",
    )
    args = ap.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute() and not config_path.exists():
        config_path = Path(__file__).resolve().parent / config_path

    if not config_path.exists():
        sys.exit(f"Config not found: {config_path}")

    Collector(
        config_path,
        use_state=not args.no_state,
        include_seen=args.all,
        force_refresh=args.force_refresh,
    ).run()

if __name__ == "__main__":
    main()
