# ============================================================
#  TOOL  —  V4
# ============================================================
# Setup:
#   ANTHROPIC_API_KEY  — https://console.anthropic.com
#   SERPER_API_KEY     — https://serper.dev (2 500 free queries/month)
#   PROXY_LIST         — optional comma-separated proxy URLs
#                        e.g. PROXY_LIST=http://user:pass@host1:8080,...
# ============================================================

# ── stdlib ──────────────────────────────────────────────────
import os
import re
import json
import time
import random
import threading
from urllib.parse import urlparse
from typing import List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── third-party ─────────────────────────────────────────────
import streamlit as st
import pandas as pd
import tldextract
from dotenv import load_dotenv
import anthropic
from bs4 import BeautifulSoup

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import (
    ConnectionError  as CffiConnectionError,
    Timeout          as CffiTimeout,
    RequestException as CffiRequestException,
)

# ============================================================
# CONSTANTS & CONFIG
# ============================================================

BLOCKED = [
    "facebook.com", "instagram.com", "x.com",
    "twitter.com", "youtube.com", "ebay.", "dictionary.",
]

HAIKU_MODEL = "claude-haiku-4-5-20251001"

L1_TIMEOUT = 3    # fast HEAD check (seconds)
L2_TIMEOUT = 10   # retry GET with redirect tracking (seconds)

# Rule confidence gating.
# Claude returns a self-assessed confidence (0–100).
# Only entities with confidence above this threshold produce rules.
MIN_RULE_CONFIDENCE = 75

# Entity name words that signal a parent/holding company.
_DIVISION_RISK_WORDS = {
    "group", "groups", "holdings", "holding",
    "international", "global",
    "ventures", "venture",
    "partners", "partner",
    "industries", "industry",
    "services", "solutions",
    "enterprises", "enterprise",
}

# Root tokens known to be highly ambiguous (common words, shared brand names, etc.).
# Any entity whose clean name contains one of these is forced to Needs Manual Revision
# regardless of Claude's confidence score.
_AMBIGUOUS_ROOT_TOKENS = {
    "atlas", "apex", "neo", "vision", "prime",
    "nova", "core", "peak", "edge", "nexus",
    "orbit", "shift", "flux", "arc", "bridge",
    "alpha", "beta", "delta", "sigma", "omega",
    "pioneer", "catalyst", "spectrum", "horizon",
}

USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 Edg/118.0.0.0",
]

_LEADING_CODE_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"[A-Z0-9]{2,}[\s]*[-–][\s]*"
    r"|"
    r"\d+\s+"
    r")"
)

_SUFFIXES = [
    "inc", "llc", "ltd", "corp", "co", "company", "group", "plc",
    "sl", "sa", "sas", "srl", "spa", "bv", "nv", "ag", "gmbh",
    "ab", "oy", "as", "kft", "sro",
]
_SUFFIX_RE = re.compile(
    r"\b(?:" + "|".join(_SUFFIXES) + r")\b",
    re.IGNORECASE,
)

_DOTTED_ABBR_RE   = re.compile(r"\b((?:[A-Z]\.){2,})", re.IGNORECASE)
_GEO_QUALIFIER_RE = re.compile(r"\s*\([^)]+\)\s*$")
_LATIN_RE = re.compile(r"[a-zA-ZÀ-ɏḀ-ỿ]")
_ALPHA_RE  = re.compile(r"[^\W\d_]", re.UNICODE)

# ============================================================
# CACHES
# ============================================================

rules_cache     : dict = {}
base_name_cache : dict = {}
about_cache     : dict = {}
search_cache    : dict = {}

# ============================================================
# API CLIENTS & ENV
# ============================================================

load_dotenv(dotenv_path=".env")
_api_key        = os.getenv("ANTHROPIC_API_KEY")
_serper_api_key = os.getenv("SERPER_API_KEY")

claude_client = anthropic.Anthropic(api_key=_api_key)

# Proxy pool: set PROXY_LIST=url1,url2,... in .env to enable IP rotation.
_proxy_pool: List[str] = [
    p.strip()
    for p in os.getenv("PROXY_LIST", "").split(",")
    if p.strip()
]

# Caps simultaneous outbound HTTP connections across all worker threads.
_request_semaphore = threading.Semaphore(15)

# ============================================================
# ANTI-BLOCKING HELPERS
# ============================================================

def _random_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": random.choice([
            "en-US,en;q=0.9",
            "en-GB,en;q=0.8,en-US;q=0.7",
            "en-US,en;q=0.5",
        ]),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _pick_proxy() -> dict:
    if not _proxy_pool:
        return {}
    proxy_url = random.choice(_proxy_pool)
    return {"http": proxy_url, "https": proxy_url}


# ============================================================
# PURE HELPER FUNCTIONS
# ============================================================

def is_latin_script(text: str) -> bool:
    if not text or not isinstance(text, str):
        return False
    alpha_chars = _ALPHA_RE.findall(text)
    if not alpha_chars:
        return False
    latin_chars = [c for c in alpha_chars if _LATIN_RE.match(c)]
    return (len(latin_chars) / len(alpha_chars)) >= 0.80


def clean_name(name) -> str:
    if pd.isna(name):
        return ""
    text = str(name).strip()
    text = _LEADING_CODE_RE.sub("", text).strip()
    text = _DOTTED_ABBR_RE.sub(lambda m: m.group(1).replace(".", ""), text)
    text = _SUFFIX_RE.sub("", text)
    text = re.sub(r"[^\w\s()]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.title()


def extract_base_name(normalized_name: str) -> str:
    """Strip trailing geographic/branch qualifiers: "Chemieuro (Francia)" → "Chemieuro"."""
    if not normalized_name:
        return normalized_name
    base = _GEO_QUALIFIER_RE.sub("", normalized_name).strip()
    return base if base else normalized_name


def extract_domain(url) -> str:
    if pd.isna(url) or not str(url).strip():
        return ""
    raw = str(url)
    if not raw.startswith("http"):
        raw = "https://" + raw
    ext = tldextract.extract(raw)
    return f"{ext.domain}.{ext.suffix}"


def normalize_url(url) -> str:
    raw = str(url).strip()
    if not raw.startswith("http"):
        raw = "https://" + raw
    return raw


def is_blocked(url: str) -> bool:
    return any(kw in url.lower() for kw in BLOCKED)


def is_division_risk(normalized_name: str) -> bool:
    words = set(normalized_name.lower().split())
    return bool(words & _DIVISION_RISK_WORDS)


def _has_broad_rule(normalized_name: str, rules: list) -> bool:
    """
    Safety net: flags a rule that is only the first word of a multi-word name.
    "Kantar Group" + rule "Kantar" → True (too broad; would match all Kantar sub-divisions).
    """
    name_parts = normalized_name.lower().split()
    if len(name_parts) < 2:
        return False
    first_word = name_parts[0]
    return any(rule.strip().lower() == first_word for rule in rules)


def _has_ambiguous_token(name: str) -> bool:
    words = set(name.lower().split())
    return bool(words & _AMBIGUOUS_ROOT_TOKENS)


def _empty_haiku_result() -> dict:
    return {
        "rules":            [],
        "rule_types":       [],
        "notes":            "",
        "description":      "",
        "confidence":       0,
        "has_divisions":    False,
        "same_name_risk":   False,
        "ambiguity_flag":   False,
        "ambiguity_reason": "",
        "evidence":         [],
    }


# ============================================================
# RESPONSE PARSING
# ============================================================

def parse_rules_response(text: str) -> dict:
    result = _empty_haiku_result()
    try:
        start = text.find("{")
        end   = text.rfind("}")
        if start == -1 or end == -1:
            return result

        payload = json.loads(text[start : end + 1])

        raw_rules = payload.get("rules", [])
        cleaned_texts: list[str] = []
        cleaned_types: list[str] = []
        seen: set[str] = set()
        for r in raw_rules:
            # Accept both new {"text": ..., "rule_type": ...} objects and legacy plain strings.
            if isinstance(r, dict):
                r_text = re.sub(r"\s+", " ", str(r.get("text", ""))).strip()
                r_type = str(r.get("rule_type", "rule")).strip().lower()
                if r_type not in ("rule", "alias"):
                    r_type = "rule"
            else:
                r_text = re.sub(r"\s+", " ", str(r)).strip()
                r_type = "rule"
            if r_text and r_text not in seen:
                seen.add(r_text)
                cleaned_texts.append(r_text)
                cleaned_types.append(r_type)

        _title_re = re.compile(
            r"\b(ceo|chief|president|founder|chairman|chairwoman|mr|mrs|ms|dr)\b",
            re.IGNORECASE,
        )
        final_texts: list[str] = []
        final_types: list[str] = []
        for r_text, r_type in zip(cleaned_texts, cleaned_types):
            if _title_re.search(r_text):
                continue
            redundant = any(
                other != r_text and other.lower() in r_text.lower() and len(other) < len(r_text)
                for other in cleaned_texts
            )
            if not redundant:
                final_texts.append(r_text)
                final_types.append(r_type)

        raw_conf = payload.get("confidence", 0)
        try:
            confidence = max(0, min(100, int(raw_conf)))
        except (TypeError, ValueError):
            confidence = 0

        raw_evidence = payload.get("evidence", [])
        evidence = [str(e).strip()[:200] for e in raw_evidence if str(e).strip()][:3]

        result.update({
            "rules":            final_texts[:5],
            "rule_types":       final_types[:5],
            "notes":            str(payload.get("notes", "")).strip()[:500],
            "description":      str(payload.get("description", "")).strip()[:600],
            "confidence":       confidence,
            "has_divisions":    bool(payload.get("has_divisions", False)),
            "same_name_risk":   bool(payload.get("same_name_risk", False)),
            "ambiguity_flag":   bool(payload.get("ambiguity_flag", False)),
            "ambiguity_reason": str(payload.get("ambiguity_reason", "")).strip()[:300],
            "evidence":         evidence,
        })
        return result

    except Exception:
        return result


# ============================================================
# NETWORK / IO FUNCTIONS
# ============================================================

def _http_status_label(status: int) -> str:
    if 200 <= status < 400 or status == 403:
        return "OK"
    if status == 404:
        return "Broken"
    return "Need revision"


def check_url_layered(url) -> dict:
    """
    Two-layer URL validation.
    Layer 1: fast HEAD (L1_TIMEOUT). Falls through on 405 or timeout.
    Layer 2: full GET with redirect following (L2_TIMEOUT).
    Returns {"status", "final_url"}.
    """
    def _fail(s):
        return {"status": s, "final_url": str(url)}

    if pd.isna(url) or not str(url).strip():
        return _fail("Need revision")

    url_norm = normalize_url(url)
    if is_blocked(url_norm):
        return _fail("Social media")

    proxy    = _pick_proxy()
    proxy_kw = {"proxies": proxy} if proxy else {}

    # Layer 1: HEAD
    with _request_semaphore:
        try:
            r1 = cffi_requests.head(
                url_norm, impersonate="chrome120",
                timeout=L1_TIMEOUT, allow_redirects=True,
                headers=_random_headers(), **proxy_kw,
            )
            if r1.status_code != 405:
                label     = _http_status_label(r1.status_code)
                final_url = str(r1.url) if hasattr(r1, "url") else url_norm
                if label == "OK":
                    return {"status": label, "final_url": final_url}
        except CffiTimeout:
            pass
        except (CffiConnectionError, CffiRequestException):
            return _fail("Broken")
        except Exception:
            pass

    time.sleep(random.uniform(0.2, 0.5))

    # Layer 2: GET
    with _request_semaphore:
        try:
            r2 = cffi_requests.get(
                url_norm, impersonate="chrome120",
                timeout=L2_TIMEOUT, allow_redirects=True,
                headers=_random_headers(), **proxy_kw,
            )
            label     = _http_status_label(r2.status_code)
            final_url = str(r2.url) if hasattr(r2, "url") else url_norm
            return {"status": label, "final_url": final_url}
        except CffiTimeout:
            return {"status": "OK", "final_url": url_norm}
        except (CffiConnectionError, CffiRequestException):
            return _fail("Broken")
        except Exception:
            return _fail("Need revision")


def check_urls_concurrent(urls: List[str], max_workers: int = 20) -> Dict[str, dict]:
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(check_url_layered, url): url for url in urls}
        for future in as_completed(future_to_url):
            raw = future_to_url[future]
            try:
                results[raw] = future.result()
            except Exception:
                results[raw] = {"status": "Need revision", "final_url": str(raw)}
    return results


def fetch_about_page(url) -> dict:
    """Try /about then /about-us on the entity root domain. Cached per root URL."""
    empty = {"title": "", "meta_description": "", "h1": "", "text": ""}
    if pd.isna(url) or not str(url).strip():
        return empty

    raw    = normalize_url(str(url))
    parsed = urlparse(raw)
    root   = f"{parsed.scheme}://{parsed.netloc}"

    if root in about_cache:
        return about_cache[root]

    proxy    = _pick_proxy()
    proxy_kw = {"proxies": proxy} if proxy else {}

    for path in ("/about", "/about-us"):
        about_url = root + path
        try:
            response = cffi_requests.get(
                about_url, impersonate="chrome120", timeout=15,
                allow_redirects=True, headers=_random_headers(), **proxy_kw,
            )
            if response.status_code >= 400:
                continue

            soup   = BeautifulSoup(response.text, "html.parser")
            result = {"title": "", "meta_description": "", "h1": "", "text": ""}

            if soup.title and soup.title.string:
                result["title"] = soup.title.string.strip()
            meta = soup.find("meta", attrs={"name": "description"})
            if meta and meta.get("content"):
                result["meta_description"] = meta.get("content").strip()[:500]
            h1 = soup.find("h1")
            if h1:
                result["h1"] = h1.get_text(" ", strip=True)[:300]
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = " ".join(soup.stripped_strings)
            result["text"] = re.sub(r"\s+", " ", text).strip()[:1200]

            about_cache[root] = result
            return result

        except Exception:
            continue

    about_cache[root] = empty
    return empty


def serper_search(query: str, count: int = 5) -> list:
    """Query Serper.dev (Google Search API). Cached per query string."""
    if not _serper_api_key:
        return []

    cache_key = f"{query}|{count}"
    if cache_key in search_cache:
        return search_cache[cache_key]

    headers = {"X-API-KEY": _serper_api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": count}
    results: list = []

    try:
        resp = cffi_requests.post(
            "https://google.serper.dev/search",
            headers=headers, json=payload, timeout=20,
        )
        data = resp.json()

        for r in data.get("organic", []):
            results.append({
                "title":          r.get("title", ""),
                "description":    r.get("snippet", ""),
                "url":            r.get("link", ""),
                "extra_snippets": [],
            })

        kg = data.get("knowledgeGraph", {})
        if kg:
            kg_text = " | ".join(filter(None, [
                kg.get("title", ""), kg.get("type", ""), kg.get("description", ""),
            ]))
            if kg_text:
                results.insert(0, {
                    "title":          kg.get("title", "Knowledge Graph"),
                    "description":    kg_text,
                    "url":            kg.get("website", ""),
                    "extra_snippets": [],
                })
    except Exception:
        results = []

    search_cache[cache_key] = results
    return results[:count]


# ============================================================
# AI / LLM FUNCTIONS  (Haiku)
# ============================================================

_CONFIDENCE_INSTRUCTIONS = """
Confidence and ambiguity assessment (ALL fields required):

"confidence" — integer 0–100 reflecting how certain you are that:
  (a) the rules are factually grounded in the evidence provided
  (b) each rule matches ONLY this specific entity, not a broader category
  (c) the rules will not produce false positives in news text
  Scoring guide:
    0–39  : little/no usable evidence, or entity is fundamentally ambiguous
    40–69 : partial evidence or notable ambiguity risk present
    70–84 : solid evidence, reasonably specific rules, low ambiguity
    85–100: strong evidence, highly specific rules, minimal false-positive risk
  When in doubt about specificity, lower the confidence score rather than
  broadening rules.

"has_divisions" — true if this entity is a PARENT or HOLDING company with
  separately named operational sub-divisions that could appear independently
  in news (e.g. "Kantar Group" has "Kantar Media", "Kantar Profiles", etc.).
  A rule like "Kantar" would incorrectly match content about any sub-division.

"same_name_risk" — true if the entity's core name is shared by multiple
  UNRELATED companies (e.g. "Apple" → Apple Inc. + Apple Corps Ltd.).
  Every rule must then include a disambiguating qualifier.

"ambiguity_flag" — true if EITHER has_divisions OR same_name_risk is true,
  OR if you cannot confidently identify which specific entity this is from
  the evidence alone.

"ambiguity_reason" — one concise sentence explaining the main risk, or "" if
  there is no ambiguity concern.

Each rule object has a "rule_type" field:
  "rule"  — high-confidence, direct name match safe for automatic publishing.
  "alias" — plausible but context-dependent surface form that requires manual
            verification before use. When ambiguity_flag is true, classify
            borderline forms as "alias" rather than omitting them entirely.

"evidence" — array of 2–3 short verbatim snippets (from the search results or
  website text above) that most directly support your confidence score and
  ambiguity assessment. Each snippet must come from the provided evidence, not
  from memory. Omit this field (empty array) only when no relevant snippets exist.

CRITICAL SPECIFICITY CONSTRAINTS:
  • If has_divisions is true: NEVER use the parent name alone as a rule.
    Wrong: "Kantar"  for entity "Kantar Group".
    Correct: "Kantar Group".
  • If same_name_risk is true: ALWAYS include a disambiguating qualifier in
    every rule.
    Wrong: "Apple"  for entity "Apple Inc.".
    Correct: "Apple Inc.", "Apple Computer".
  • If ambiguity_flag is true and you cannot produce unambiguous rules,
    return an empty rules list and set confidence below 50.
"""

_JSON_SCHEMA = """{
  "rules": [
    {"text": "Entity Name Inc.", "rule_type": "rule"},
    {"text": "ENI", "rule_type": "alias"}
  ],
  "notes": "Very short note",
  "description": "2-3 sentence factual description of the entity",
  "confidence": 80,
  "has_divisions": false,
  "same_name_risk": false,
  "ambiguity_flag": false,
  "ambiguity_reason": "",
  "evidence": ["snippet from search result 1", "snippet from website text", "snippet 3"]
}"""


def get_rules_from_haiku(
    name: str,
    domain: str,
    website_info: dict,
    search_results: list = None,
) -> dict:
    """
    Single Haiku call that uses about-page content as primary evidence
    and appends search results when available.
    """
    search_text_parts = []
    if search_results:
        for i, r in enumerate(search_results[:5], start=1):
            search_text_parts.append(f"{i}. {r['title']}\n{r['description']}\n{r['url']}")
            for extra in r.get("extra_snippets", [])[:2]:
                search_text_parts.append(f"Extra: {extra}")

    search_section = (
        f"\nSearch results:\n{chr(10).join(search_text_parts)}"
        if search_text_parts
        else ""
    )

    prompt = f"""You are helping build entity mention rules for English-language news matching.

Target entity:
Name: {name}
Domain: {domain}

Tasks:
1. Extract mention rules (surface forms used to identify this entity in news text).
2. Write a concise entity description: 2-3 sentences covering what the entity is,
   what it does, and where it operates. Base it only on the evidence — do not speculate.

Rules constraints:
- Return 0 to 5 rules only
- No generic phrases, no context-dependent phrases
- No longer wrappers around a shorter included rule
- No speculative aliases
- Surname-only for people only if clearly distinctive and evidenced
- Short name or acronym for organizations only if clearly evidenced

{_CONFIDENCE_INSTRUCTIONS}

EVIDENCE CONSTRAINT: Base all confidence scores, ambiguity assessments, rule decisions,
and evidence snippets ONLY on the website content and search results provided below.
Do not assert facts from training knowledge that are absent from the provided evidence.
If the evidence is insufficient to identify the entity, set confidence below 50.

Website title: {website_info.get("title", "")}
Website meta description: {website_info.get("meta_description", "")}
Website H1: {website_info.get("h1", "")}
Website text excerpt: {website_info.get("text", "")}{search_section}

Return strict JSON only:
{_JSON_SCHEMA}"""

    retries = 0
    while retries < 3:
        try:
            response = claude_client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            return parse_rules_response(response.content[0].text.strip())
        except anthropic.RateLimitError:
            retries += 1
            wait = 60 * retries
            print(f"Rate limit hit for '{name}'. Waiting {wait}s …")
            time.sleep(wait)
        except Exception as e:
            print(f"Haiku API error for '{name}': {e}")
            return _empty_haiku_result()
    return _empty_haiku_result()


# ============================================================
# ORCHESTRATION FUNCTIONS
# ============================================================

def build_search_queries(
    name: str,
    domain: str,
    website_info: Dict[str, str],
    entity_type: str = "org",
    role_or_company: str = "",
) -> List[str]:
    queries = []
    # Confirmation queries — establish the entity's identity
    if name and domain:
        queries.append(f'"{name}" "{domain}"')
    if name:
        queries.append(f'"{name}" company')
    if name and website_info.get("title"):
        queries.append(f'"{name}" "{website_info["title"][:80]}"')
    # Disambiguation queries — detect common-word overlap and off-domain mentions
    if name:
        queries.append(f'"{name}" meaning OR word')
    if name and domain:
        queries.append(f'"{name}" -site:{domain}')
    # Person-specific identity anchoring
    if entity_type == "person" and name and role_or_company:
        queries.append(f'"{name}" "{role_or_company[:60]}"')
    seen: set = set()
    deduped = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped[:5]


def _review_tier(ambiguity_flag: bool, has_rules: bool) -> str:
    if has_rules and not ambiguity_flag:
        return "A — Spot-check"
    if has_rules:
        return "B — Review reasoning"
    return "C — Full manual"


def generate_rules_for_row(row) -> dict:
    """
    Per-row enrichment pipeline:
      1. Cache hit on URL   → return cached result
      2. Cache hit on base name → return cached result (shared across regional variants)
      3. Fetch about page (/about, /about-us)
      4. Serper search — confirmation + disambiguation queries
      5. No-evidence short-circuit → Needs Manual Revision = True
      6. Call Haiku with combined evidence
      7. Confidence gate: suppress rules and flag if confidence <= 75 or ambiguity detected
      8. Ambiguous token blocklist: force manual regardless of score
      9. Return output dict
    """
    raw_url          = str(row["URL"])
    name             = row["Clean Name"]
    base_name        = extract_base_name(name)
    domain           = row["Normalized URL"]
    entity_type      = str(row.get("Entity Type", "org") or "org").lower().strip()
    role_or_company  = str(row.get("Role", "") or "").strip()

    # 1. URL cache hit
    if raw_url in rules_cache:
        return rules_cache[raw_url]

    # 2. Base-name cache hit
    if base_name and base_name in base_name_cache:
        cached = base_name_cache[base_name].copy()
        prior  = cached.get("Rules Evidence") or ""
        cached["Rules Evidence"] = (prior + " [shared from base entity]").strip()
        rules_cache[raw_url] = cached
        return cached

    # 3. Fixed rule creation threshold.
    threshold = MIN_RULE_CONFIDENCE

    def _no_rule_output(notes: str) -> dict:
        tier = "C — Full manual"
        return {
            "Rule 1": "", "Rule 2": "", "Rule 3": "",
            "Rule 4": "", "Rule 5": "",
            "Rule Type 1": "", "Rule Type 2": "", "Rule Type 3": "",
            "Rule Type 4": "", "Rule Type 5": "",
            "Entity Description":       "",
            "Rules Evidence":           notes,
            "Needs Manual Revision":    True,
            "Ambiguity Flag":           False,
            "Review Tier":              tier,
        }

    # 4. Fetch about page
    about_info = fetch_about_page(raw_url)
    has_about  = any(about_info.values())

    # 5. Serper search — run all queries to maximise disambiguation evidence
    queries        = build_search_queries(name, domain, about_info, entity_type, role_or_company)
    search_results: list = []
    for q in queries:
        search_results.extend(serper_search(q, count=5))
        if len(search_results) >= 10:
            break

    # 6. No-evidence short-circuit
    if not has_about and not search_results:
        output = _no_rule_output("No about page and no search results — insufficient evidence")
        rules_cache[raw_url]       = output
        base_name_cache[base_name] = output
        return output

    # 7. Call Haiku
    haiku_result     = get_rules_from_haiku(name, domain, about_info, search_results[:10])
    rules            = haiku_result["rules"]
    rule_types       = haiku_result["rule_types"]
    notes            = haiku_result["notes"]
    description      = haiku_result["description"]
    confidence       = haiku_result["confidence"]
    has_divisions    = haiku_result["has_divisions"]
    ambiguity_flag   = bool(haiku_result["ambiguity_flag"] or has_divisions)
    ambiguity_reason = haiku_result["ambiguity_reason"]
    evidence         = haiku_result["evidence"]

    # 8. Confidence gate → Needs Manual Revision
    needs_manual = False

    if confidence <= threshold:
        needs_manual = True
        notes = (
            f"[Auto-suppressed] Confidence {confidence} below threshold {threshold}. "
            f"{ambiguity_reason or 'Insufficient evidence for reliable rules.'}"
        )
    elif ambiguity_flag:
        needs_manual = True
        notes = f"[Auto-suppressed] Ambiguity detected. {ambiguity_reason}"
    elif _has_broad_rule(name, rules):
        needs_manual = True
        notes = (
            f"[Auto-suppressed] Rule too broad for multi-word entity '{name}'. "
            f"{ambiguity_reason or 'Manual review needed.'}"
        )

    # 9. Ambiguous token blocklist — overrides confidence regardless of score
    if _has_ambiguous_token(name):
        needs_manual = True
        notes = (f"[Blocklist] Name contains known ambiguous root token. " + notes).strip()

    if needs_manual:
        rules      = []
        rule_types = []
        description = ""

    padded       = (rules      + ["", "", "", "", ""])[:5]
    padded_types = (rule_types + ["", "", "", "", ""])[:5]
    tier = _review_tier(ambiguity_flag, bool(rules))

    evidence_text = " | ".join(evidence)
    if notes and evidence_text:
        combined_evidence = f"{notes} | Evidence: {evidence_text}"
    else:
        combined_evidence = notes or evidence_text

    output = {
        "Rule 1":              padded[0],
        "Rule 2":              padded[1],
        "Rule 3":              padded[2],
        "Rule 4":              padded[3],
        "Rule 5":              padded[4],
        "Rule Type 1":         padded_types[0],
        "Rule Type 2":         padded_types[1],
        "Rule Type 3":         padded_types[2],
        "Rule Type 4":         padded_types[3],
        "Rule Type 5":         padded_types[4],
        "Rules Evidence":      combined_evidence,
        "Entity Description":  description,
        "Needs Manual Revision":    needs_manual,
        "Ambiguity Flag":      ambiguity_flag,
        "Review Tier":         tier,
    }

    rules_cache[raw_url]       = output
    base_name_cache[base_name] = output
    return output


# ============================================================
# STREAMLIT UI HELPERS
# ============================================================

_OK_STATUS     = {"OK"}
_BROKEN_STATUS = {"Broken", "Social media"}

_TAB1_COLS = [
    "Name", "URL",
    "Clean Name", "Base Name", "Normalized URL",
    "Duplicate Flag", "Website Status",
    "Entity Description",
    "Rules",
    "Rule 1", "Rule Type 1",
    "Rule 2", "Rule Type 2",
    "Rule 3", "Rule Type 3",
    "Rule 4", "Rule Type 4",
    "Rule 5", "Rule Type 5",
    "Rules Evidence",
]

_TAB2_COLS = [
    "Name", "URL",
    "Clean Name", "Base Name", "Normalized URL",
    "Website Status",
    "Needs Manual Revision",
    "Review Tier",
    "Ambiguity Flag",
    "Duplicate Flag",
    "Rules Evidence",
    "Review Reason",
]

_TAB3_COLS = [
    "Name", "URL",
    "Clean Name", "Normalized URL",
    "Latin Script", "Website Status",
    "Review Reason",
]

_ALL_EXPORT_COLS = [
    "Tab",
    "Name", "URL",
    "Clean Name", "Base Name", "Normalized URL",
    "Website Status", "Duplicate Flag", "Latin Script",
    "Entity Description",
    "Rules",
    "Rule 1", "Rule Type 1",
    "Rule 2", "Rule Type 2",
    "Rule 3", "Rule Type 3",
    "Rule 4", "Rule Type 4",
    "Rule 5", "Rule Type 5",
    "Rules Evidence",
    "Needs Manual Revision",
    "Ambiguity Flag",
    "Review Tier",
    "Review Reason",
]


def _make_review_reason(row) -> str:
    reasons = []
    if not row.get("Latin Script", True):
        reasons.append("Non-Latin characters in entity name")
    status = row.get("Website Status", "")
    if status == "Broken":
        reasons.append("Broken link (404 or connection failed)")
    elif status == "Social media":
        reasons.append("Social media / blocked domain")
    elif status == "Need revision":
        reasons.append("Link needs revision (unexpected HTTP response)")
    if row.get("Needs Manual Revision", False):
        reasons.append("Low confidence or ambiguous entity — manual review required")
    if row.get("Duplicate Flag", False):
        reasons.append("Duplicate URL in batch")
    return "; ".join(reasons) if reasons else "Unknown"


def _render_results(df: pd.DataFrame) -> None:
    total   = len(df)
    ok_mask = (
        df["Website Status"].isin(_OK_STATUS)
        & df["Latin Script"]
        & (~df["Needs Manual Revision"])
        & (df["Rules"] != "")
    )
    rev_mask = (
        ~ok_mask
        & ~(
            (~df["Latin Script"])
            | df["Website Status"].isin(_BROKEN_STATUS)
        )
    )
    inc_mask = (~df["Latin Script"]) | df["Website Status"].isin(_BROKEN_STATUS)

    n_ok  = int(ok_mask.sum())
    n_rev = int(rev_mask.sum())
    n_inc = int(inc_mask.sum())

    ok_rows = df[df["Website Status"].isin(_OK_STATUS) & df["Latin Script"]]
    if len(ok_rows):
        flagged = int(ok_rows["Needs Manual Revision"].sum())
        st.info(
            f"**{total} total rows** — "
            f"**{n_ok} OK** | "
            f"**{n_rev} Needs Revision** ({flagged} flagged for manual review) | "
            f"**{n_inc} Incorrect**"
        )

    df_ok  = df[ok_mask].copy()
    df_rev = df[rev_mask].copy()
    df_inc = df[inc_mask].copy()

    df_rev["Review Reason"] = df_rev.apply(_make_review_reason, axis=1)
    df_inc["Review Reason"] = df_inc.apply(_make_review_reason, axis=1)

    if not _proxy_pool:
        st.warning(
            "No proxies configured (`PROXY_LIST` not set in `.env`). "
            "Sending many requests from one IP may cause valid sites to be "
            "falsely marked Broken or Need revision."
        )

    tab1, tab2, tab3 = st.tabs([
        f"OK  ({n_ok})",
        f"Needs Revision  ({n_rev})",
        f"Incorrect Requests  ({n_inc})",
    ])

    with tab1:
        st.caption("Clean links + valid names + high-confidence rules. Ready for training.")
        st.dataframe(
            df_ok[[c for c in _TAB1_COLS if c in df_ok.columns]],
            use_container_width=True,
        )

    with tab2:
        st.caption(
            "Entities that need manual review: low confidence, ambiguous names, "
            "or links returning unexpected HTTP responses."
        )
        st.dataframe(
            df_rev[[c for c in _TAB2_COLS if c in df_rev.columns]],
            use_container_width=True,
        )

    with tab3:
        st.caption(
            "Non-Latin names, broken links (404 / connection failed), "
            "and social media / blocked domains."
        )
        st.dataframe(
            df_inc[[c for c in _TAB3_COLS if c in df_inc.columns]],
            use_container_width=True,
        )

    st.divider()
    df_export = df.copy()
    df_export["Tab"] = "Incorrect Requests"
    df_export.loc[rev_mask, "Tab"] = "Needs Revision"
    df_export.loc[ok_mask,  "Tab"] = "OK"
    if "Review Reason" not in df_export.columns:
        df_export["Review Reason"] = df_export.apply(_make_review_reason, axis=1)
    df_export = df_export[[c for c in _ALL_EXPORT_COLS if c in df_export.columns]]
    st.download_button(
        "Download Full CSV  (all rows, all tabs, Tab column shows category)",
        df_export.to_csv(index=False).encode("utf-8"),
        "cleaned_data_v3.csv",
        "text/csv",
    )


# ============================================================
# STREAMLIT UI
# ============================================================

st.title("Data ConceptOps Tool  —  V3")
st.caption(
    "Haiku + Serper.dev  |  Layered URL validation (HEAD → GET)  "
    "|  Confidence gating  |  Ambiguity detection"
)

if "df_result" not in st.session_state:
    st.session_state.df_result = None
if "_file_id" not in st.session_state:
    st.session_state._file_id  = None

uploaded_file = st.file_uploader("Upload CSV", type=["csv"])

if uploaded_file:
    file_id = (uploaded_file.name, uploaded_file.size)
    if st.session_state._file_id != file_id:
        st.session_state.df_result = None
        st.session_state._file_id  = file_id

    df_raw = pd.read_csv(uploaded_file)
    st.write("Preview:", df_raw.head())

    if st.button("Run Cleaning"):
        df = df_raw.copy()

        rules_cache.clear()
        base_name_cache.clear()
        about_cache.clear()
        search_cache.clear()

        progress_bar = st.progress(0)
        status_text  = st.empty()

        # 1. Latin-script check
        status_text.text("Checking script / language…")
        df["Latin Script"] = df["Name"].apply(
            lambda n: is_latin_script(str(n)) if pd.notna(n) else False
        )
        progress_bar.progress(5)

        # 2. URL normalization & duplicate detection
        status_text.text("Normalizing URLs and checking duplicates…")
        df["Normalized URL"] = df["URL"].apply(extract_domain)
        df["Duplicate Flag"] = df["URL"].duplicated(keep=False)
        progress_bar.progress(12)

        # 3. Link validation (HEAD → GET)
        status_text.text("Validating URLs…")
        url_results = check_urls_concurrent(df["URL"].tolist(), max_workers=20)
        df["Website Status"] = df["URL"].map(
            lambda u: url_results.get(u, {}).get("status", "Need revision")
        )
        progress_bar.progress(42)

        # 4. Name cleaning
        status_text.text("Cleaning names…")
        df["Clean Name"] = df["Name"].apply(clean_name)
        df["Base Name"]  = df["Clean Name"].apply(extract_base_name)
        progress_bar.progress(55)

        # 5. Rule generation — Serper + Claude (eligible rows only)
        status_text.text("Generating rules and descriptions…")

        for col in [
            "Rule 1", "Rule 2", "Rule 3", "Rule 4", "Rule 5",
            "Rule Type 1", "Rule Type 2", "Rule Type 3", "Rule Type 4", "Rule Type 5",
            "Rules Evidence", "Entity Description",
            "Review Tier",
        ]:
            df[col] = ""
        df["Needs Manual Revision"] = False
        df["Ambiguity Flag"]        = False

        eligible_indices = [
            idx for idx, row in df.iterrows()
            if row["Website Status"] == "OK"
            and df.at[idx, "Latin Script"]
            and row["Normalized URL"]
        ]
        total_eligible = len(eligible_indices)

        for processed, idx in enumerate(eligible_indices, start=1):
            row        = df.loc[idx]
            rules_data = generate_rules_for_row(row)
            for col, value in rules_data.items():
                df.at[idx, col] = value
            pct = 55 + int((processed / max(total_eligible, 1)) * 43)
            progress_bar.progress(min(pct, 98))
            status_text.text(
                f"Processing {processed}/{total_eligible}: {row.get('Clean Name', '')} …"
            )

        # 6. Combined Rules column
        df["Rules"] = df[["Rule 1", "Rule 2", "Rule 3", "Rule 4", "Rule 5"]].apply(
            lambda r: " | ".join(x for x in r if x), axis=1
        )

        progress_bar.progress(100)
        status_text.text("")
        st.success("Processing completed!")

        st.session_state.df_result = df

    if st.session_state.df_result is not None:
        _render_results(st.session_state.df_result)
