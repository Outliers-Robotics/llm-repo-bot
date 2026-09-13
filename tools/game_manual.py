"""Search and read the official FRC Game Manual PDF.

The manual is published as a single PDF that is revised through the season
(the "Version:" stamp in each page header, e.g. TU22). This module downloads
it once, extracts the text, and splits it into entries keyed by rule ID
(G401, R501, I101, ...) and by numbered subsection (5.9.2 OUTPOST), so rules
can be looked up exactly and the narrative sections stay searchable.
"""

from collections import OrderedDict
import hashlib
import io
import json
import logging
import math
import os
import re
import tempfile
from threading import Lock
from time import time

import requests

from pypdf import PdfReader


logger = logging.getLogger(__name__)

GAME_MANUAL_URL = "https://firstfrc.blob.core.windows.net/frc2026/Manual/2026GameManual.pdf"
GAME_MANUAL_SEASON = "2026"
GAME_MANUAL_HOST = "firstfrc.blob.core.windows.net"

DOWNLOAD_TIMEOUT = 60
MAX_PDF_BYTES = 64 * 1024 * 1024
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
# Bump when tokenization or entry parsing changes, to invalidate stale caches.
INDEX_SCHEMA_VERSION = 2
MAX_ENTRY_CHARS = 6000
SNIPPET_CHARS = 360

# Rule IDs are a single section letter plus three digits: G (game), R (ROBOT
# construction), I (inspection), T (tournament), C (Championship), E (event),
# Q (REFEREE interaction). Two-letter matches such as AM802 are part numbers.
RULE_ID_RE = re.compile(r"\b([GRITCEQ])(\d{3})\b")
RULE_START_RE = re.compile(r"^\s*([GRITCEQ]\d{3})\s+(\*?)\s*(\S.*)$")
SUBSECTION_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})\s+([A-Za-z][^\n]{2,70}?)\s*$")
PAGE_HEADER_RE = re.compile(
    r"^\s*Section\s+(\d+)\s+(.*?)\s+Version:\s*(\S+)\s+(\d+)\s+of\s+(\d+)\s*$"
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    """a an and are as at be by can could do does did for from get got had has have how i
    if in is it its may me must my need no not of on or our should so than that the their
    them then there they this to us use used using want was we what when where which who
    why will with would you your rule rules manual game about allowed allow""".split()
)


def _stem(token: str) -> str:
    """Fold the plural and participle forms the manual mixes freely."""
    if len(token) > 4:
        for suffix in ("ies", "es", "ing", "ed"):
            if token.endswith(suffix):
                base = token[: -len(suffix)]
                if len(base) >= 3:
                    return f"{base}y" if suffix == "ies" else base
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokenize(text: str) -> list[str]:
    return [
        _stem(t)
        for t in _TOKEN_RE.findall(text.lower())
        if t not in _STOPWORDS and len(t) > 1
    ]


def _cache_ttl_seconds() -> int:
    try:
        hours = int(os.getenv("FRC_MANUAL_CACHE_TTL_HOURS", "24"))
        return hours * 3600 if hours > 0 else DEFAULT_CACHE_TTL_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_CACHE_TTL_SECONDS


def _cache_dir() -> str:
    return os.getenv(
        "FRC_MANUAL_CACHE_DIR",
        os.path.join(tempfile.gettempdir(), "frc-game-manual-cache"),
    )


def _clean_line(line: str) -> str:
    """Normalize a single extracted PDF line."""
    # PDF extraction leaves non-breaking spaces and soft hyphens behind.
    line = line.replace("\xa0", " ").replace("­", "")
    return re.sub(r"[ \t]+", " ", line).strip()


class GameManualIndex:
    """Thread-safe, lazily built search index over the Game Manual PDF."""

    def __init__(
        self,
        url: str = GAME_MANUAL_URL,
        season: str = GAME_MANUAL_SEASON,
        session: requests.Session | None = None,
    ):
        self.url = url
        self.season = season
        self.session = session
        self._lock = Lock()
        self._entries: list[dict] | None = None
        self._by_rule: dict[str, dict] = {}
        self._by_page: OrderedDict[int, list[dict]] = OrderedDict()
        self._version: str | None = None
        self._page_count: int = 0
        self._loaded_at: float = 0.0
        self._load_error: str | None = None

    # -- loading ---------------------------------------------------------

    @property
    def _cache_path(self) -> str:
        digest = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]
        return os.path.join(_cache_dir(), f"manual-{digest}.json")

    def _download(self) -> bytes:
        session = self.session or requests
        resp = session.get(self.url, timeout=DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()
        buffer = io.BytesIO()
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            buffer.write(chunk)
            if buffer.tell() > MAX_PDF_BYTES:
                raise ValueError(
                    f"Game Manual exceeded the {MAX_PDF_BYTES} byte download limit"
                )
        return buffer.getvalue()

    def _extract_pages(self, pdf_bytes: bytes) -> list[str]:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return [(page.extract_text() or "") for page in reader.pages]

    def _read_disk_cache(self) -> dict | None:
        path = self._cache_path
        try:
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if (
                payload.get("url") != self.url
                or payload.get("schema") != INDEX_SCHEMA_VERSION
                or not payload.get("entries")
            ):
                return None
            return payload
        except Exception:
            logger.warning("Ignoring unreadable Game Manual cache at %s", path)
            return None

    def _write_disk_cache(self, payload: dict):
        path = self._cache_path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Write to a sibling temp file so a crash cannot leave a partial cache.
            handle = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(path),
                prefix="manual-", suffix=".tmp", delete=False,
            )
            try:
                with handle:
                    json.dump(payload, handle)
                os.replace(handle.name, path)
            except Exception:
                try:
                    os.unlink(handle.name)
                except OSError:
                    pass
                raise
        except Exception:
            logger.warning("Could not persist Game Manual cache to %s", path, exc_info=True)

    def _install(self, payload: dict):
        entries = payload.get("entries") or []
        self._entries = entries
        self._version = payload.get("version")
        self._page_count = payload.get("page_count") or 0
        self._loaded_at = payload.get("fetched_at") or time()

        by_rule: dict[str, dict] = {}
        by_page: OrderedDict[int, list[dict]] = OrderedDict()
        for entry in entries:
            rule_id = entry.get("rule_id")
            # Keep the first occurrence: later hits are cross-references.
            if rule_id and rule_id not in by_rule:
                by_rule[rule_id] = entry
            by_page.setdefault(entry.get("page", 0), []).append(entry)
        self._by_rule = by_rule
        self._by_page = by_page

    def ensure_loaded(self, force: bool = False) -> bool:
        """Load the manual index, using the disk cache when it is still fresh."""
        with self._lock:
            fresh = (
                self._entries is not None
                and (time() - self._loaded_at) < _cache_ttl_seconds()
            )
            if fresh and not force:
                return True

            if not force:
                cached = self._read_disk_cache()
                if cached and (time() - (cached.get("fetched_at") or 0)) < _cache_ttl_seconds():
                    self._install(cached)
                    self._load_error = None
                    return True

            try:
                pdf_bytes = self._download()
                pages = self._extract_pages(pdf_bytes)
                payload = self._build_payload(pages)
                self._install(payload)
                self._write_disk_cache(payload)
                self._load_error = None
                return True
            except Exception as error:
                self._load_error = f"{type(error).__name__}: {error}"
                logger.exception("Could not load the FRC Game Manual from %s", self.url)
                # Fall back to any stale copy rather than losing the tool entirely.
                if self._entries is not None:
                    return True
                stale = self._read_disk_cache()
                if stale:
                    self._install(stale)
                    return True
                return False

    # -- parsing ---------------------------------------------------------

    def _build_payload(self, pages: list[str]) -> dict:
        entries: list[dict] = []
        version = None
        section_number = ""
        section_name = ""
        current: dict | None = None

        def close_current():
            nonlocal current
            if current is None:
                return
            body = "\n".join(current.pop("_lines")).strip()
            if body or current.get("rule_id"):
                current["text"] = body[:MAX_ENTRY_CHARS]
                entries.append(current)
            current = None

        for page_index, raw_text in enumerate(pages):
            page_number = page_index + 1
            for raw_line in raw_text.splitlines():
                line = _clean_line(raw_line)
                if not line:
                    continue

                header = PAGE_HEADER_RE.match(line)
                if header:
                    section_number = header.group(1)
                    section_name = header.group(2).strip()
                    version = version or header.group(3)
                    continue

                rule = RULE_START_RE.match(line)
                if rule:
                    close_current()
                    rule_id, asterisk, remainder = rule.group(1), rule.group(2), rule.group(3)
                    title = remainder.split(".")[0].strip()[:120] or rule_id
                    current = {
                        "kind": "rule",
                        "rule_id": rule_id,
                        "id": rule_id,
                        "title": title,
                        "section": section_name,
                        "section_number": section_number,
                        "page": page_number,
                        "headline_violation": bool(asterisk),
                        "_lines": [f"{rule_id} {remainder}"],
                    }
                    continue

                subsection = SUBSECTION_RE.match(line)
                if subsection and not line.lower().endswith(("of 166", "of the")):
                    close_current()
                    current = {
                        "kind": "section",
                        "rule_id": None,
                        "id": subsection.group(1),
                        "title": subsection.group(2).strip(),
                        "section": section_name,
                        "section_number": section_number,
                        "page": page_number,
                        "headline_violation": False,
                        "_lines": [line],
                    }
                    continue

                if current is None:
                    current = {
                        "kind": "page",
                        "rule_id": None,
                        "id": f"p{page_number}",
                        "title": section_name or f"Page {page_number}",
                        "section": section_name,
                        "section_number": section_number,
                        "page": page_number,
                        "headline_violation": False,
                        "_lines": [],
                    }
                current["_lines"].append(line)

        close_current()

        for entry in entries:
            entry["tokens"] = _tokenize(f"{entry['title']} {entry['text']}")

        return {
            "schema": INDEX_SCHEMA_VERSION,
            "url": self.url,
            "season": self.season,
            "version": version,
            "page_count": len(pages),
            "fetched_at": time(),
            "entries": entries,
        }

    # -- access ----------------------------------------------------------

    @property
    def metadata(self) -> dict:
        return {
            "season": self.season,
            "version": self._version,
            "page_count": self._page_count,
            "url": self.url,
        }

    def page_url(self, page: int) -> str:
        return f"{self.url}#page={page}" if page else self.url

    def get_rule(self, rule_id: str) -> dict | None:
        if not self.ensure_loaded():
            return None
        return self._by_rule.get(rule_id.strip().upper())

    def get_page(self, page: int) -> list[dict]:
        if not self.ensure_loaded():
            return []
        return self._by_page.get(page, [])

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        """Rank manual entries against a free-text query."""
        if not self.ensure_loaded() or not self._entries:
            return []

        explicit_rules = [
            f"{m.group(1)}{m.group(2)}" for m in RULE_ID_RE.finditer(query.upper())
        ]
        terms = _tokenize(query)

        results: list[dict] = []
        seen: set[tuple] = set()

        # An explicit rule ID in the question is an exact lookup, not a guess.
        for rule_id in explicit_rules:
            entry = self._by_rule.get(rule_id)
            if entry:
                key = (entry["page"], entry["id"])
                if key not in seen:
                    seen.add(key)
                    results.append(self._format(entry, terms))

        if terms:
            scored: list[tuple[int, int, dict]] = []
            for entry in self._entries:
                title_tokens = set(_tokenize(entry["title"]))
                body_tokens = entry.get("tokens") or []
                body_counts: dict[str, int] = {}
                for token in body_tokens:
                    body_counts[token] = body_counts.get(token, 0) + 1

                score = 0
                matched = 0
                for term in terms:
                    hit = False
                    if term in title_tokens:
                        score += 12
                        hit = True
                    count = body_counts.get(term, 0)
                    if count:
                        score += min(count, 5) * 3
                        hit = True
                    elif not hit:
                        partial = sum(
                            1 for token in body_counts if len(term) > 3 and term in token
                        )
                        if partial:
                            score += 1
                            hit = True
                    if hit:
                        matched += 1

                if not score:
                    continue
                # Reward entries covering more of the question, and prefer rules.
                score += matched * matched * 4
                if entry["kind"] == "rule":
                    score += 6
                # Long entries (the Glossary especially) accumulate incidental
                # term hits, so normalize them against focused ones.
                length = len(entry.get("text", ""))
                if length > 2000:
                    score = int(score * math.sqrt(2000 / length))
                scored.append((score, -entry["page"], entry))

            scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
            for score, _, entry in scored:
                key = (entry["page"], entry["id"])
                if key in seen:
                    continue
                seen.add(key)
                results.append(self._format(entry, terms))
                if len(results) >= max_results:
                    break

        return results[:max_results]

    def _snippet(self, text: str, terms: list[str]) -> str:
        """Pick the window of text that covers the most query terms."""
        if not text:
            return ""
        if len(text) <= SNIPPET_CHARS or not terms:
            return text[:SNIPPET_CHARS].strip()

        lowered = text.lower()
        best_pos, best_hits = 0, -1
        step = 80
        for start in range(0, max(len(text) - SNIPPET_CHARS, 1), step):
            window = lowered[start : start + SNIPPET_CHARS]
            hits = sum(window.count(term) for term in terms)
            if hits > best_hits:
                best_hits, best_pos = hits, start

        snippet = text[best_pos : best_pos + SNIPPET_CHARS].strip()
        if best_pos:
            snippet = f"…{snippet}"
        if best_pos + SNIPPET_CHARS < len(text):
            snippet = f"{snippet}…"
        return snippet

    def _format(self, entry: dict, terms: list[str]) -> dict:
        label = entry["rule_id"] or entry["id"]
        title = f"{label} {entry['title']}".strip()
        return {
            "title": title,
            "url": self.page_url(entry["page"]),
            "snippet": self._snippet(entry.get("text", ""), terms),
            "rule_id": entry["rule_id"],
            "section": entry["section"],
            "page": entry["page"],
            "source": f"{self.season} FRC Game Manual",
        }


MANUAL = GameManualIndex()


def _manual_unavailable() -> dict:
    return {
        "error": "The FRC Game Manual PDF could not be downloaded or parsed.",
        "url": GAME_MANUAL_URL,
        "note": "Answer from other sources and say the manual itself was unavailable.",
    }


def search_game_manual(query: str, max_results: int = 5) -> dict:
    """Search the official FIRST Robotics Competition Game Manual PDF for rules,
    penalties, field elements, scoring, and game definitions.

    Use this for any question about what is legal, how scoring works, penalty
    amounts, ROBOT size or weight limits, BUMPER rules, inspection requirements,
    or tournament procedure. Rule IDs mentioned in the query (such as G401 or
    R501) are looked up exactly.

    Args:
        query: Rule ID or search terms (e.g. 'G401', 'BUMPER height',
            'ROBOT weight limit', 'AUTO scoring', 'extension beyond frame perimeter').
        max_results: Maximum number of manual entries to return (1-10).

    Returns:
        Matching manual entries with rule ID, section, page number, a text
        snippet, and a citable URL. Call read_game_manual_rule for a rule's
        full text, or read_frc_doc with a returned URL to read the whole page.
    """
    if not query or not query.strip():
        return {"error": "Provide a non-empty search query."}

    try:
        max_results = max(1, min(int(max_results), 10))
    except (TypeError, ValueError):
        max_results = 5

    matches = MANUAL.search(query.strip(), max_results=max_results)
    if not matches and not MANUAL.ensure_loaded():
        return _manual_unavailable()

    result = {
        "query": query.strip(),
        "manual": MANUAL.metadata,
        "matches": matches,
    }
    if not matches:
        result["note"] = (
            "No manual entries matched. Try the rule ID, a defined term in CAPS "
            "(such as BUMPER, FUEL, or AUTO), or fewer keywords."
        )
    else:
        result["note"] = (
            "Quote rule text exactly and cite the rule ID plus the manual version. "
            "Use read_game_manual_rule for a rule's complete text."
        )
    return result


def read_game_manual_rule(rule_id: str) -> dict:
    """Read the complete text of one numbered rule from the FRC Game Manual.

    Args:
        rule_id: The exact rule ID, such as 'G401', 'R501', 'I101', 'T201',
            'E101', or 'C301'. Game rules start with G, ROBOT construction
            rules with R, inspection with I, and tournament rules with T.

    Returns:
        The rule's full text, its section, page number, and a citable URL.
    """
    if not rule_id or not rule_id.strip():
        return {"error": "Provide a rule ID such as 'G401'."}

    # Match the text as written first, so "rule R103" does not collapse into
    # "RULER103" and lose its word boundary; fall back for spellings like "R 103".
    upper = rule_id.strip().upper()
    match = RULE_ID_RE.search(upper) or RULE_ID_RE.search(upper.replace(" ", ""))
    if not match:
        return {
            "error": f"'{rule_id}' is not a valid FRC rule ID.",
            "note": "Rule IDs are one letter (G, R, I, T, C, E, Q) plus three digits, e.g. G401.",
        }
    normalized = f"{match.group(1)}{match.group(2)}"

    if not MANUAL.ensure_loaded():
        return _manual_unavailable()

    entry = MANUAL.get_rule(normalized)
    if not entry:
        return {
            "error": f"Rule {normalized} was not found in the {MANUAL.season} Game Manual.",
            "manual": MANUAL.metadata,
            "note": "The rule may not exist this season. Use search_game_manual to find the current rule.",
        }

    return {
        "rule_id": entry["rule_id"],
        "title": entry["title"],
        "section": entry["section"],
        "page": entry["page"],
        "url": MANUAL.page_url(entry["page"]),
        "content": entry["text"],
        "manual": MANUAL.metadata,
        "source": f"{MANUAL.season} FRC Game Manual",
    }


def read_game_manual_page(page: int) -> dict:
    """Read the full extracted text of one page of the FRC Game Manual PDF.

    Args:
        page: The manual page number, as shown in search results.

    Returns:
        The page's text, the rules it contains, and a citable URL.
    """
    try:
        page = int(page)
    except (TypeError, ValueError):
        return {"error": "Provide a numeric manual page number."}

    if not MANUAL.ensure_loaded():
        return _manual_unavailable()

    entries = MANUAL.get_page(page)
    if not entries:
        return {
            "error": f"Page {page} has no extractable text in the {MANUAL.season} Game Manual.",
            "manual": MANUAL.metadata,
        }

    parts = []
    for entry in entries:
        label = entry["rule_id"] or entry["id"]
        parts.append(f"## {label} {entry['title']}\n{entry['text']}")
    content = "\n\n".join(parts)

    return {
        "page": page,
        "url": MANUAL.page_url(page),
        "section": entries[0]["section"],
        "rules": [e["rule_id"] for e in entries if e["rule_id"]],
        "content": content[:MAX_ENTRY_CHARS * 2],
        "truncated": len(content) > MAX_ENTRY_CHARS * 2,
        "manual": MANUAL.metadata,
        "source": f"{MANUAL.season} FRC Game Manual",
    }
