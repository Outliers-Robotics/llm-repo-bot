"""Tools for looking up official FRC documentation, CTRE API, WPILib docs, and Game Manual resources."""

from collections import OrderedDict
from html import unescape
import json
import logging
import re
from threading import Lock
from urllib.parse import urlparse

import requests

from tools.game_manual import (
    GAME_MANUAL_HOST,
    GAME_MANUAL_URL,
    MANUAL,
    read_game_manual_page,
    read_game_manual_rule,
    search_game_manual,
)


logger = logging.getLogger(__name__)

USER_AGENT = "OutliersRobotics-FRC-Bot/1.0 (+https://github.com/Outliers-Robotics/llm-repo-bot)"
SEARCH_TIMEOUT = 5
READ_TIMEOUT = 8
MAX_DOC_CHARS = 16000

APPROVED_FRC_DOMAINS = {
    "docs.wpilib.org",
    "github.wpilib.org",
    "v6.docs.ctr-electronics.com",
    "api.ctr-electronics.com",
    "store.ctr-electronics.com",
    "chiefdelphi.com",
    "www.chiefdelphi.com",
    "firstinspires.org",
    "www.firstinspires.org",
    "frc-qa.firstinspires.org",
    "docs.revrobotics.com",
    "pathplanner.dev",
    "docs.limelightvision.io",
    "docs.photonvision.org",
    "thebluealliance.com",
    "www.thebluealliance.com",
    "firstinspires.blob.core.windows.net",
    GAME_MANUAL_HOST,
    "choreo.autos",
    "advantagescope.org",
}

WPILIB_INDEX_URL = "https://docs.wpilib.org/en/stable/searchindex.js"
WPILIB_BASE_URL = "https://docs.wpilib.org/en/stable"

CTRE_INDEX_URL = "https://v6.docs.ctr-electronics.com/en/stable/searchindex.js"
CTRE_BASE_URL = "https://v6.docs.ctr-electronics.com/en/stable"
CTRE_API_BASE_URL = "https://api.ctr-electronics.com/phoenix6/stable/cpp"


class FRCDocsRegistry:
    """Thread-safe search index and document cache for FRC technical resources."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._index_cache: dict[str, dict] = {}
        self._page_cache: OrderedDict[str, dict] = OrderedDict()
        self._lock = Lock()
        self._max_pages = 50

    def get_sphinx_index(self, index_url: str) -> dict | None:
        """Fetch and parse a Sphinx searchindex.js file, caching the result."""
        with self._lock:
            if index_url in self._index_cache:
                return self._index_cache[index_url]

        try:
            resp = self.session.get(index_url, timeout=SEARCH_TIMEOUT)
            resp.raise_for_status()
            text = resp.text
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1:
                return None
            data = json.loads(text[start : end + 1])
            with self._lock:
                self._index_cache[index_url] = data
            return data
        except Exception:
            logger.exception("Failed to load Sphinx search index from %s", index_url)
            return None

    def search_sphinx(
        self,
        index_url: str,
        base_url: str,
        source_name: str,
        query: str,
        max_results: int = 5,
    ) -> list[dict]:
        """Search a Sphinx search index for matching documentation pages."""
        data = self.get_sphinx_index(index_url)
        if not data:
            return []

        terms = re.findall(r"[a-zA-Z0-9]+", query.lower())
        if not terms:
            return []

        scores: dict[int, int] = {}
        title_terms = data.get("titleterms", {})
        body_terms = data.get("terms", {})

        for term in terms:
            # Score matches in titles (higher weight)
            for word, val in title_terms.items():
                if term == word:
                    weight = 20
                elif term in word or word.startswith(term):
                    weight = 8
                else:
                    continue
                items = val if isinstance(val, list) else [val]
                for item in items:
                    idx = item if isinstance(item, int) else item[0]
                    scores[idx] = scores.get(idx, 0) + weight

            # Score matches in body terms
            for word, val in body_terms.items():
                if term == word:
                    weight = 5
                elif term in word:
                    weight = 2
                else:
                    continue
                items = val if isinstance(val, list) else [val]
                for item in items:
                    idx = item if isinstance(item, int) else item[0]
                    scores[idx] = scores.get(idx, 0) + weight

        top_indices = sorted(scores.keys(), key=lambda i: scores[i], reverse=True)[:max_results]
        results = []
        titles = data.get("titles", [])
        docnames = data.get("docnames", [])

        for idx in top_indices:
            if idx < len(titles) and idx < len(docnames):
                title = titles[idx]
                docname = docnames[idx]
                results.append({
                    "title": title,
                    "url": f"{base_url}/{docname}.html",
                    "source": source_name,
                })
        return results

    def search_chief_delphi(self, query: str, max_results: int = 5) -> list[dict]:
        """Search Chief Delphi discourse forums for discussions, rules, and Q&A."""
        try:
            resp = self.session.get(
                "https://www.chiefdelphi.com/search.json",
                params={"q": query},
                timeout=SEARCH_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            results = []
            topics_by_id = {t["id"]: t for t in data.get("topics", [])}
            posts = data.get("posts", [])

            # Extract top matching posts or topics
            seen_topics = set()
            for post in posts:
                topic_id = post.get("topic_id")
                if not topic_id or topic_id in seen_topics:
                    continue
                seen_topics.add(topic_id)
                topic = topics_by_id.get(topic_id, {})
                title = topic.get("title") or f"Chief Delphi Topic #{topic_id}"
                snippet = (post.get("blurb") or "").strip()
                post_num = post.get("post_number", 1)
                url = f"https://www.chiefdelphi.com/t/{topic_id}/{post_num}"
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "source": "Chief Delphi (FRC Community & Rules)",
                })
                if len(results) >= max_results:
                    break

            # If fewer post matches, backfill from topic list
            if len(results) < max_results:
                for topic in data.get("topics", []):
                    topic_id = topic.get("id")
                    if topic_id in seen_topics:
                        continue
                    seen_topics.add(topic_id)
                    results.append({
                        "title": topic.get("title", ""),
                        "url": f"https://www.chiefdelphi.com/t/{topic_id}",
                        "snippet": "",
                        "source": "Chief Delphi (FRC Community & Rules)",
                    })
                    if len(results) >= max_results:
                        break

            return results
        except Exception:
            logger.exception("Chief Delphi search failed for query: %r", query)
            return []

    def get_cached_page(self, url: str) -> dict | None:
        with self._lock:
            if url in self._page_cache:
                self._page_cache.move_to_end(url)
                return dict(self._page_cache[url])
            return None

    def cache_page(self, url: str, data: dict):
        with self._lock:
            if len(self._page_cache) >= self._max_pages and url not in self._page_cache:
                self._page_cache.popitem(last=False)
            self._page_cache[url] = dict(data)
            self._page_cache.move_to_end(url)


REGISTRY = FRCDocsRegistry()


def is_approved_frc_domain(url: str) -> bool:
    """Validate that the URL belongs to an approved FRC technical domain."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = (parsed.netloc or "").split(":")[0].lower()
        return hostname in APPROVED_FRC_DOMAINS
    except Exception:
        return False


def _clean_html_to_markdown(html: str) -> str:
    """Convert HTML content into clean, readable Markdown."""
    # Remove script, style, nav, header, footer, aside
    cleaned = re.sub(
        r"<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>",
        "",
        html,
        flags=re.DOTALL | re.I,
    )
    # Target main content container if available
    match = re.search(
        r'<div role=[\"\']main[\"\'][^>]*>(.*?)</div>\s*<div class=[\"\']rst-footer-buttons',
        cleaned,
        flags=re.DOTALL | re.I,
    )
    if not match:
        match = re.search(r"<article[^>]*>(.*?)</article>", cleaned, flags=re.DOTALL | re.I)
    if not match:
        match = re.search(r"<main[^>]*>(.*?)</main>", cleaned, flags=re.DOTALL | re.I)

    body = match.group(1) if match else cleaned

    # Convert headings
    body = re.sub(r"<h1[^>]*>(.*?)</h1>", r"\n\n# \1\n", body, flags=re.DOTALL | re.I)
    body = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n\n## \1\n", body, flags=re.DOTALL | re.I)
    body = re.sub(r"<h3[^>]*>(.*?)</h3>", r"\n\n### \1\n", body, flags=re.DOTALL | re.I)
    body = re.sub(r"<h[4-6][^>]*>(.*?)</h[4-6]>", r"\n\n#### \1\n", body, flags=re.DOTALL | re.I)

    # Convert code blocks
    body = re.sub(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", r"\n```\n\1\n```\n", body, flags=re.DOTALL | re.I)
    body = re.sub(r"<pre[^>]*>(.*?)</pre>", r"\n```\n\1\n```\n", body, flags=re.DOTALL | re.I)
    body = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", body, flags=re.DOTALL | re.I)

    # Convert list items
    body = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", body, flags=re.DOTALL | re.I)

    # Remove all other HTML tags
    body = re.sub(r"<[^>]+>", " ", body)
    body = unescape(body)

    # Normalize whitespace
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n\s*\n\s*\n+", "\n\n", body)
    return body.strip()


def search_frc_docs(query: str, source: str = "all") -> dict:
    """Search online FIRST Robotics Competition (FRC) technical documentation,
    CTRE Phoenix 6 API, WPILib documentation, Game Manual rules, and Chief Delphi forums.

    Args:
        query: Specific search terms or question (e.g. 'TalonFX current limit',
            'SwerveDriveKinematics', 'PIDController', 'reefscape bumper rules',
            'PathPlannerLib auto', 'CANcoder magnet offset', 'G401 rule').
        source: Source filter. Permitted values:
            - 'all': Search across WPILib, CTRE, and Chief Delphi (default)
            - 'wpilib': Search official WPILib documentation
            - 'ctre': Search CTRE Phoenix 6 / Phoenix 5 documentation and API
            - 'game_manual': Search the official FRC Game Manual PDF plus Chief Delphi
            - 'chiefdelphi': Search Chief Delphi discussions only
            - 'rev': Search REV Robotics documentation and discussions

    Returns:
        Matching documentation titles, URLs, and summaries.
        Call read_frc_doc with a returned URL to read full page content.
    """
    if not query or not query.strip():
        return {"error": "Provide a non-empty search query."}

    query = query.strip()
    source_lower = (source or "all").lower()
    matches = []

    # 1. WPILib Docs
    if source_lower in ("all", "wpilib"):
        matches.extend(REGISTRY.search_sphinx(
            WPILIB_INDEX_URL, WPILIB_BASE_URL, "WPILib Docs", query, max_results=4,
        ))

    # 2. CTRE Docs
    if source_lower in ("all", "ctre"):
        matches.extend(REGISTRY.search_sphinx(
            CTRE_INDEX_URL, CTRE_BASE_URL, "CTRE Phoenix 6 Docs", query, max_results=4,
        ))

    # 3. The Game Manual PDF itself, which is authoritative for rules
    if source_lower in ("all", "game_manual", "manual", "rules"):
        manual = search_game_manual(query, max_results=4)
        matches.extend(manual.get("matches", []))

    # 4. Chief Delphi (community analyses, Q&A rulings, and workarounds)
    if source_lower in ("all", "chiefdelphi", "game_manual", "rules", "rev"):
        cd_query = query
        if source_lower == "rev" and "rev" not in query.lower() and "spark" not in query.lower():
            cd_query = f"REV {query}"
        matches.extend(REGISTRY.search_chief_delphi(cd_query, max_results=4))

    # De-duplicate by URL
    seen_urls = set()
    unique_matches = []
    for m in matches:
        if m["url"] not in seen_urls:
            seen_urls.add(m["url"])
            unique_matches.append(m)

    if not unique_matches:
        return {
            "query": query,
            "source": source,
            "matches": [],
            "note": "No direct matches found. Try shorter keywords or specify source='wpilib', 'ctre', or 'chiefdelphi'.",
        }

    return {
        "query": query,
        "source": source,
        "matches": unique_matches,
        "note": "Read full details of any page using read_frc_doc(url='...'). Cite the URL in your answer.",
    }


def _read_game_manual_url(parsed) -> dict:
    """Resolve a Game Manual PDF URL to indexed text, honoring #page=N / #rule=G401."""
    fragment = (parsed.fragment or "").strip()

    page_match = re.search(r"page=(\d+)", fragment, re.I)
    if page_match:
        return read_game_manual_page(int(page_match.group(1)))

    rule_match = re.search(r"(?:rule=)?([GRITCEQ]\d{3})", fragment, re.I)
    if rule_match:
        return read_game_manual_rule(rule_match.group(1))

    # No fragment: describe the manual rather than dumping 166 pages.
    MANUAL.ensure_loaded()
    return {
        "url": GAME_MANUAL_URL,
        "title": f"{MANUAL.season} FRC Game Manual",
        "manual": MANUAL.metadata,
        "note": (
            "This is the full Game Manual PDF. Call search_game_manual(query=...) to find "
            "relevant rules, read_game_manual_rule(rule_id=...) for one rule's full text, "
            "or read this URL with a '#page=N' fragment for a single page."
        ),
    }


def read_frc_doc(url: str) -> dict:
    """Read and extract clean text from an official FRC documentation page,
    CTRE API reference, Chief Delphi topic, or FIRST resource.

    Only approved FRC domains (docs.wpilib.org, v6.docs.ctr-electronics.com,
    api.ctr-electronics.com, chiefdelphi.com, firstinspires.org, docs.revrobotics.com,
    pathplanner.dev, docs.limelightvision.io, docs.photonvision.org, thebluealliance.com)
    can be accessed. Non-FRC domains are blocked.

    A Game Manual URL ending in '#page=N' returns that manual page's text; use
    read_game_manual_rule instead when you want one specific numbered rule.

    Args:
        url: The exact URL of the FRC documentation page or forum topic to read.

    Returns:
        Extracted article text, page title, URL, and any relevant headings or code snippets.
    """
    if not url or not url.strip():
        return {"error": "Provide a valid URL to read."}

    url = url.strip()

    if not is_approved_frc_domain(url):
        parsed = urlparse(url)
        domain = (parsed.netloc or "").split(":")[0]
        return {
            "error": f"Access denied: domain '{domain}' is not an approved FRC domain.",
            "note": "Lookup is strictly restricted to official FRC documentation (WPILib, CTRE, FIRST, Chief Delphi, REV, PathPlanner, Limelight, PhotonVision, TBA).",
        }

    parsed = urlparse(url)
    hostname = (parsed.netloc or "").split(":")[0].lower()

    # The Game Manual is a 5 MB PDF: serve it from the parsed rule index
    # rather than re-downloading and stripping raw bytes on every read.
    if hostname == GAME_MANUAL_HOST and parsed.path.lower().endswith(".pdf"):
        return _read_game_manual_url(parsed)

    cached = REGISTRY.get_cached_page(url)
    if cached:
        return cached

    # Special handling for Chief Delphi discourse topics: fetch JSON API for pristine text
    if "chiefdelphi.com" in hostname:
        topic_match = re.search(r"/t/(?:[^/]+/)?(\d+)", parsed.path)
        if topic_match:
            topic_id = topic_match.group(1)
            json_url = f"https://www.chiefdelphi.com/t/{topic_id}.json"
            try:
                resp = REGISTRY.session.get(json_url, timeout=READ_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                title = data.get("title", "Chief Delphi Topic")
                posts = data.get("post_stream", {}).get("posts", [])

                parts = [f"# {title}\n"]
                for p in posts[:6]:  # Include first post + top replies
                    author = p.get("username", "user")
                    post_body = _clean_html_to_markdown(p.get("cooked") or "")
                    parts.append(f"### Post by @{author}:\n{post_body}\n")

                content = "\n".join(parts)
                truncated = len(content) > MAX_DOC_CHARS
                content = content[:MAX_DOC_CHARS]

                result = {
                    "url": url,
                    "title": title,
                    "content": content,
                    "truncated": truncated,
                    "source": "Chief Delphi",
                }
                REGISTRY.cache_page(url, result)
                return result
            except Exception:
                logger.warning("Chief Delphi JSON fetch failed for %s; falling back to HTML", url)

    # Standard HTML fetching
    try:
        resp = REGISTRY.session.get(url, timeout=READ_TIMEOUT)
        resp.raise_for_status()

        # Extract title
        title_match = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.I | re.DOTALL)
        title = unescape(title_match.group(1)).strip() if title_match else "FRC Documentation"
        title = re.sub(r"\s+", " ", title)

        content = _clean_html_to_markdown(resp.text)
        truncated = len(content) > MAX_DOC_CHARS
        content = content[:MAX_DOC_CHARS]

        result = {
            "url": url,
            "title": title,
            "content": content,
            "truncated": truncated,
            "source": "FRC Documentation",
        }
        REGISTRY.cache_page(url, result)
        return result
    except Exception as error:
        logger.exception("Failed to read FRC doc URL: %s", url)
        return {
            "error": f"Failed to retrieve documentation page ({type(error).__name__}: {error})",
            "url": url,
        }
