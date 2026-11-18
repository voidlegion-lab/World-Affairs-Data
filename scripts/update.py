#!/usr/bin/env python3
"""World Affairs data job.

1. Reads public RSS feeds (headlines + short excerpts only).
2. Groups reports of the same story from different sources.
3. Asks Google Gemini (free tier) to write careful summaries using ONLY the supplied text.
4. Writes data/stories.json: headlines, links, source names and AI notes. No article text is stored.

Run with:  python scripts/update.py            (real run, needs GEMINI_API_KEY for summaries)
           python scripts/update.py --mock --fixtures tests/fixtures --out /tmp/out.json   (offline test)
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "stories.json"

WINDOW_HOURS = 72            # stories older than this are dropped
MAX_EVENTS = 400             # size limit for the published file
BATCH_SIZE = 8               # stories per AI request
MAX_AI_REQUESTS = 12         # AI requests per run (keeps the free tier safe)
EXCERPT_FOR_AI = 600         # characters of each excerpt sent to the AI
USER_AGENT = "Mozilla/5.0 (compatible; WorldAffairsPersonalReader/1.0)"

RSS_NS = "http://purl.org/rss/1.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"

STOP = {
    "the", "and", "for", "with", "from", "that", "this", "after", "over", "into",
    "amid", "says", "will", "have", "has", "are", "was", "were", "been", "about",
    "than", "more", "new", "its", "their", "they", "what", "when", "who", "how",
    "why", "not", "but", "can", "could", "would", "may", "also", "said",
}

# ----------------------------------------------------------------- feed reading


def clean_text(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def fix_xml(raw):
    """Makes slightly broken feeds readable (bare & signs, control characters)."""
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text)
    # ElementTree rejects an encoding declaration on text that is already decoded.
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    return text.encode("utf-8")


def split_tag(tag):
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


def parse_date(value):
    v = (value or "").strip()
    if not v:
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(v)          # handles "03 Oct 26" and "03 Oct 2026"
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.year < 2000:
        return None
    return dt


def is_digest(snippet):
    head = snippet[:700]
    return bool(re.search(r"(?i)good (morning|afternoon|evening)", head)) and " Plus " in head


def parse_feed(raw, source):
    """Returns items using only the item's own direct title/link/description/date."""
    root = ET.fromstring(fix_xml(raw))
    out = []
    for item in root.iter():
        ns, local = split_tag(item.tag)
        if local != "item" or ns not in ("", RSS_NS):
            continue
        title = link = desc = date = ""
        for child in item:                     # direct children only (ignores image captions)
            cns, clocal = split_tag(child.tag)
            text = child.text or ""
            if cns in ("", RSS_NS):
                if clocal == "title":
                    title = text
                elif clocal == "link":
                    link = text
                elif clocal == "description":
                    desc = text
                elif clocal == "pubDate":
                    date = text
            elif cns == DC_NS and clocal == "date":
                date = text
        title = clean_text(title)
        link = link.strip()
        snippet = clean_text(desc)[:1500]
        if not title or not link.startswith("http") or is_digest(snippet):
            continue
        published = parse_date(date) or datetime.now(timezone.utc)
        out.append({
            "source": source, "title": title, "link": link,
            "snippet": snippet, "published": published,
        })
    return out


def fetch_url(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def collect(sources, fixtures_dir=None):
    items, report = [], []
    for s in sources:
        try:
            if fixtures_dir:
                path = Path(fixtures_dir) / f"{slug(s['name'])}.xml"
                if not path.exists():
                    raise FileNotFoundError("no fixture")
                raw = path.read_bytes()
            else:
                raw = fetch_url(s["url"])
            got = parse_feed(raw, s["name"])
            items.extend(got)
            if got:
                report.append({"source": s["name"], "ok": True, "count": len(got)})
            else:
                report.append({"source": s["name"], "ok": False, "error": "connected but found 0 headlines"})
        except Exception as e:  # keep going: one broken feed must not stop the job
            report.append({"source": s["name"], "ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
    return items, report

# ---------------------------------------------------------------------- grouping


def tokens(title):
    words = re.split(r"[^a-z0-9]+", title.lower())
    return {w for w in words if len(w) >= 4 and w not in STOP}


def similar(a, b):
    if not a or not b:
        return False
    shared = len(a & b)
    return shared >= 3 and shared / min(len(a), len(b)) >= 0.5


def group(items):
    """items newest first. Returns list of events (lists of items from different sources)."""
    clusters, index = [], {}
    for it in items:
        t = tokens(it["title"])
        match = None
        cands = []
        for w in t:
            for cid in index.get(w, []):
                if cid not in cands:
                    cands.append(cid)
        for cid in cands:
            c = clusters[cid]
            if all(x["source"] != it["source"] for x in c["items"]) and similar(t, c["tokens"]):
                match = c
                break
        if match:
            match["items"].append(it)
        else:
            clusters.append({"tokens": t, "items": [it]})
            for w in t:
                index.setdefault(w, []).append(len(clusters) - 1)
    return [c["items"] for c in clusters]


def event_id(items):
    return hashlib.sha1(items[0]["link"].encode()).hexdigest()[:12]


def links_hash(items):
    return hashlib.sha1("|".join(sorted(i["link"] for i in items)).encode()).hexdigest()[:12]

# --------------------------------------------------------------------------- AI

SYSTEM = """You are a careful news summariser for a personal current-affairs study app.
Rules:
1. Use ONLY the text supplied for each story (headlines and excerpts). Never add facts, numbers, names, dates, quotes or causes that are not in the supplied text.
2. If the supplied text is too thin for a field, write "Not stated in the sources." for text fields, or [] for lists.
3. Stay neutral. Separate confirmed facts from claims, allegations, opinions and predictions; put the latter in "unconfirmed".
4. If sources give different figures or accounts, describe the difference in "discrepancies"; otherwise use [].
5. Write in your own words. Do not copy sentences and do not use direct quotations.
6. "background_ai" may use well-established general knowledge to give context (2-3 sentences). If you are not sure, return "". It is unverified.
7. "exam_points": up to 5 short facts (people, organisations, agreements, dates, statistics) taken from the supplied text that could be asked in a competitive exam. No outside facts.
Return ONLY valid JSON, with no commentary."""

SCHEMA_HINT = """Return JSON in exactly this shape:
{"stories": [{"id": "<same id as given>",
 "short_summary": "1-2 sentences",
 "detailed_summary": "3-6 sentences using only the supplied text",
 "key_facts": ["..."],
 "unconfirmed": ["..."],
 "discrepancies": ["..."],
 "significance": "1-2 sentences that follow only from the supplied text",
 "background_ai": "",
 "exam_points": ["..."]}]}"""

AI_TEXT_FIELDS = ["short_summary", "detailed_summary", "significance", "background_ai"]
AI_LIST_FIELDS = ["key_facts", "unconfirmed", "discrepancies", "exam_points"]


def http_json(url, key, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"x-goog-api-key": key}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pick_model(key):
    """Chooses the newest stable 'flash-lite' (else 'flash') Gemini model the key can use."""
    override = os.environ.get("GEMINI_MODEL", "").strip()
    if override:
        return override
    bad = ("image", "tts", "audio", "live", "embedding", "robotics", "computer",
           "native", "imagen", "veo", "lyria", "learnlm", "gemma", "aqa", "vision", "pro")
    try:
        data = http_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", key)
        found = []
        for m in data.get("models", []):
            name = m.get("name", "").replace("models/", "")
            if not name.startswith("gemini"):
                continue
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if any(b in name for b in bad):
                continue
            tier = 0 if "flash-lite" in name else (1 if "flash" in name else 2)
            if tier == 2:
                continue
            preview = 1 if ("preview" in name or "exp" in name) else 0
            nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", name)]
            found.append((tier, preview, [-n for n in nums], name))
        if found:
            found.sort()
            return found[0][3]
    except Exception as e:
        print(f"Could not list Gemini models ({e}); using default.", file=sys.stderr)
    return "gemini-2.5-flash-lite"


def call_gemini(model, key, user_text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                             "maxOutputTokens": 8192},
    }
    last = None
    for attempt in range(4):
        try:
            data = http_json(url, key, body)
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 500, 503):
                wait = 20 * (attempt + 1)
                retry_after = e.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = max(wait, int(retry_after))
                print(f"Gemini said {e.code}; waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError) as e:
            last = str(e)
            time.sleep(10)
    raise RuntimeError(f"Gemini failed: {last}")


def mock_gemini(batch):
    return json.dumps({"stories": [{
        "id": b["id"],
        "short_summary": "MOCK short summary.",
        "detailed_summary": "MOCK detailed summary.",
        "key_facts": ["MOCK fact"],
        "unconfirmed": [], "discrepancies": [],
        "significance": "MOCK significance.",
        "background_ai": "", "exam_points": ["MOCK exam point"],
    } for b in batch]})


def parse_ai(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    data = json.loads(text)
    stories = data.get("stories", []) if isinstance(data, dict) else data
    out = {}
    for s in stories:
        if not isinstance(s, dict) or "id" not in s:
            continue
        clean = {}
        for f in AI_TEXT_FIELDS:
            v = s.get(f, "")
            clean[f] = v.strip() if isinstance(v, str) else ""
        for f in AI_LIST_FIELDS:
            v = s.get(f, [])
            clean[f] = [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []
        out[str(s["id"])] = clean
    return out


def ask_ai(batch, model, key, mock):
    if mock:
        return mock_gemini(batch)
    prompt = SCHEMA_HINT + "\n\nStories:\n" + json.dumps(batch, ensure_ascii=False)
    text = call_gemini(model, key, prompt)
    time.sleep(5)
    return text


def make_batch(events):
    return [{
        "id": event_id(ev),
        "sources": [{"source": i["source"], "headline": i["title"],
                     "excerpt": i["snippet"][:EXCERPT_FOR_AI]} for i in ev],
    } for ev in events]


def summarise(pending, key, mock=False):
    """pending: list of events. Returns (results dict id -> ai, stats)."""
    results, requests_made, errors, skipped = {}, 0, [], 0
    model = "mock" if mock else pick_model(key)
    print(f"Using model: {model}")
    for start in range(0, len(pending), BATCH_SIZE):
        if requests_made >= MAX_AI_REQUESTS:
            break
        chunk = pending[start:start + BATCH_SIZE]
        batch = make_batch(chunk)
        wanted = {b["id"] for b in batch}
        try:
            requests_made += 1
            got = parse_ai(ask_ai(batch, model, key, mock))
            got = {k: v for k, v in got.items() if k in wanted}
            results.update(got)
            missing = [ev for ev in chunk if event_id(ev) not in got]
            print(f"Batch {requests_made}: asked {len(chunk)}, received {len(got)}")
            # The AI sometimes leaves stories out: ask once more for just those.
            if missing and requests_made < MAX_AI_REQUESTS:
                requests_made += 1
                retry = make_batch(missing)
                got2 = parse_ai(ask_ai(retry, model, key, mock))
                got2 = {k: v for k, v in got2.items() if k in {r["id"] for r in retry}}
                results.update(got2)
                print(f"Batch {requests_made} (retry): asked {len(missing)}, received {len(got2)}")
                skipped += len(missing) - len(got2)
            else:
                skipped += len(missing)
        except Exception as e:
            errors.append(str(e)[:200])
            print(f"AI batch failed: {e}", file=sys.stderr)
    return results, {"model": model, "requests": requests_made, "errors": errors, "skipped": skipped}

# -------------------------------------------------------------------------- main


def load_previous(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"events": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="fake AI answers (testing)")
    ap.add_argument("--fixtures", help="read feeds from this folder instead of the internet")
    ap.add_argument("--out", help="write to this file instead of data/stories.json")
    args = ap.parse_args()

    target = Path(args.out) if args.out else DATA_FILE
    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    items, report = collect(sources, args.fixtures)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    items = [i for i in items if i["published"] >= cutoff]
    items.sort(key=lambda i: i["published"], reverse=True)
    events = group(items)[:MAX_EVENTS]

    previous = {e["id"]: e for e in load_previous(target).get("events", [])}
    key = os.environ.get("GEMINI_API_KEY", "").strip()

    pending = []
    for ev in events:
        old = previous.get(event_id(ev))
        if old and old.get("ai") and old.get("links_hash") == links_hash(ev):
            continue                               # unchanged story: reuse its summary
        pending.append(ev)
    # multi-source stories first, then newest
    pending.sort(key=lambda ev: (-len(ev), -max(i["published"] for i in ev).timestamp()))

    ai_stats = {"model": None, "requests": 0, "errors": [], "skipped": 0}
    results = {}
    if pending and (key or args.mock):
        results, ai_stats = summarise(pending, key, mock=args.mock)
    elif pending:
        ai_stats["errors"].append("GEMINI_API_KEY is not set; no summaries written.")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out_events = []
    for ev in events:
        eid = event_id(ev)
        old = previous.get(eid)
        ai, ai_at, ai_model = None, None, None
        if eid in results:
            ai, ai_at, ai_model = results[eid], now, ai_stats["model"]
        elif old and old.get("ai") and old.get("links_hash") == links_hash(ev):
            ai, ai_at, ai_model = old["ai"], old.get("ai_at"), old.get("ai_model")
        out_events.append({
            "id": eid,
            "links_hash": links_hash(ev),
            "published": max(i["published"] for i in ev).isoformat(timespec="seconds"),
            "title": ev[0]["title"],
            "sources": [{"name": i["source"], "title": i["title"], "link": i["link"],
                         "published": i["published"].isoformat(timespec="seconds")} for i in ev],
            "ai": ai, "ai_at": ai_at, "ai_model": ai_model,
        })

    summarised = sum(1 for e in out_events if e["ai"])
    output = {
        "generated_at": now,
        "events": out_events,
        "report": report,
        "ai": {**ai_stats, "summarised": summarised, "total": len(out_events)},
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {len(out_events)} stories ({summarised} with AI notes) to {target}")
    for r in report:
        print(("OK   " if r["ok"] else "FAIL ") + r["source"], r.get("count", r.get("error", "")))


if __name__ == "__main__":
    main()
