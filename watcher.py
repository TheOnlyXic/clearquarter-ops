#!/usr/bin/env python3
"""
ClearQuarter community watcher. v2

Watches Reddit for BAS/GST questions from Australian sole traders,
drafts a reply, sends it to Discord. You edit and post it yourself.

Modes:
  - Reddit OAuth API (recommended, reliable): set REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD secrets.
  - Public RSS (fallback): works, but cloud IPs get rate-limited often.

Usage:
  python watcher.py          # normal run
  python watcher.py --test   # offline self-test
"""
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree

STATE_FILE = "watcher_state.json"
USER_AGENT = "clearquarter-watcher/1.0 (personal project)"
DISCORD_HEADERS = {"Content-Type": "application/json", "User-Agent": "clearquarter-watcher/1.0"}

SUBREDDITS = ["SmallBusinessAU", "AusFinance", "auscorp"]

SEARCH_QUERIES = [
    '"BAS" sole trader',
    '"GST" spreadsheet OR template',
]

KEYWORD_CATEGORIES = {
    "bas_help": [
        "bas help", "bas overdue", "lodge bas", "lodging bas", "first bas",
        "bas due", "bas panic", "bas stressed", "bas nightmare", "bas audit",
    ],
    "gst_maths": [
        "work out gst", "gst calculator", "divide by 11", "gst on invoice",
        "gst backwards", "gst formula", "how much gst",
    ],
    "spreadsheet": [
        "bas spreadsheet", "gst spreadsheet", "bookkeeping spreadsheet",
        "bas template", "gst template", "sole trader bookkeeping",
    ],
    "dates": [
        "when is bas due", "bas due date", "bas deadline", "bas october",
        "bas february", "bas april", "bas july",
    ],
}

DRAFTS = {
    "bas_help": (
        "Quarterly BAS for a sole trader is less scary than it looks. The numbers "
        "you need: G1 is all your sales including GST, 1A is the GST on those sales "
        "(divide each GST-inclusive sale by 11), 1B is the GST you paid on business "
        "purchases. 1A minus 1B is what you pay or get back. Most sole traders are "
        "on the cash basis, so you count money when it actually moves. Happy to get "
        "more specific if you say what part is tripping you up."
    ),
    "gst_maths": (
        "GST is 10% of the ex-GST price, which works out to 1/11 of the price you "
        "actually charged. Invoiced $1,100 including GST? GST is $1,100 / 11 = $100. "
        "Dividing the inclusive amount by 10 instead overstates your GST by about "
        "10%, which is the most common mistake going."
    ),
    "spreadsheet": (
        "For a sole trader BAS you don't need much: date, amount including GST, GST "
        "(amount divided by 11), a category, and which quarter it belongs to. On the "
        "cash basis, the quarter is the quarter the money moved, not the invoice "
        "date. Bank fees and wages carry no GST, so nothing to claim there."
    ),
    "dates": (
        "Quarterly BAS dates are fixed: 28 October, 28 February, 28 April, 28 July. "
        "If the 28th lands on a weekend it moves to the next business day, so "
        "February is sometimes 1 March. Always lodge on time even if you can't pay; "
        "the penalty is for late lodgement, not for owing."
    ),
    "default": (
        "Happy to help if you share more detail. Short version for GST on a BAS: "
        "sales including GST go on G1, GST collected is 1A (divide inclusive sales "
        "by 11), GST paid on purchases is 1B, and you pay or refund the difference."
    ),
}

LINK_CATEGORIES = {"bas_help", "spreadsheet"}
DISCLOSURE_LINE = (
    "Full disclosure: I built a tool called ClearQuarter that does the GST "
    "splitting and quarter tagging for sole traders, A$19. Only mentioning it "
    "because it's exactly this problem."
)


def fetch(url, headers=None, retries=2):
    """GET with backoff. Raises the last error if all attempts fail."""
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    raise last_err


def build_feeds():
    feeds = []
    for sub in SUBREDDITS:
        feeds.append(f"https://www.reddit.com/r/{sub}/new/.rss?limit=50")
    for q in SEARCH_QUERIES:
        feeds.append(
            "https://www.reddit.com/search/feed.xml?"
            + urllib.parse.urlencode({"q": q, "sort": "new", "t": "week", "limit": 50})
        )
    return feeds


ATOM = "{http://www.w3.org/2005/Atom}"


def parse_feed(xml_bytes):
    root = ElementTree.fromstring(xml_bytes)
    posts = []
    for entry in root.findall(f"{ATOM}entry"):
        post_id = entry.findtext(f"{ATOM}id") or ""
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        link_el = entry.find(f"{ATOM}link")
        link = link_el.get("href") if link_el is not None else ""
        content_el = entry.find(f"{ATOM}content")
        content = content_el.text or "" if content_el is not None else ""
        text = re.sub(r"<[^>]+>", " ", content)
        text = re.sub(r"\s+", " ", text).strip()
        posts.append({"id": post_id, "title": title, "link": link, "text": text})
    return posts


def fetch_rss_posts():
    posts, errors = [], []
    for i, feed_url in enumerate(build_feeds()):
        if i:
            time.sleep(2)
        try:
            posts.extend(parse_feed(fetch(feed_url)))
            print(f"  fetched: {feed_url[:80]}")
        except Exception as e:
            errors.append(f"{feed_url[:80]} -> {e}")
    return posts, errors


def reddit_token():
    cid = os.environ.get("REDDIT_CLIENT_ID", "")
    cs = os.environ.get("REDDIT_CLIENT_SECRET", "")
    user = os.environ.get("REDDIT_USERNAME", "")
    pw = os.environ.get("REDDIT_PASSWORD", "")
    if not (cid and cs and user and pw):
        return None
    body = urllib.parse.urlencode({
        "grant_type": "password", "username": user, "password": pw,
    }).encode()
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=body,
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{cid}:{cs}".encode()).decode(),
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"no token in response: {data}")
    return token


def fetch_api_posts(token):
    listings = [f"https://oauth.reddit.com/r/{s}/new?limit=50&raw_json=1" for s in SUBREDDITS]
    for q in SEARCH_QUERIES:
        listings.append(
            "https://oauth.reddit.com/search?"
            + urllib.parse.urlencode({"q": q, "sort": "new", "t": "week", "limit": 50, "raw_json": 1})
        )
    posts, errors = [], []
    for i, url in enumerate(listings):
        if i:
            time.sleep(1)
        try:
            data = json.loads(fetch(url, headers={"Authorization": f"Bearer {token}"}))
            for child in data.get("data", {}).get("children", []):
                d = child.get("data", {})
                pid = d.get("id", "")
                posts.append({
                    "id": f"t3_{pid}" if pid else "",
                    "title": d.get("title", ""),
                    "link": "https://www.reddit.com" + d.get("permalink", ""),
                    "text": d.get("selftext", "") or "",
                })
            print(f"  fetched: {url[:80]}")
        except Exception as e:
            errors.append(f"{url[:80]} -> {e}")
    return posts, errors


def categorize(post):
    haystack = (post["title"] + " " + post["text"]).lower()
    for category, keywords in KEYWORD_CATEGORIES.items():
        for kw in keywords:
            if kw in haystack:
                return category, kw
    return None, None


def draft_reply(category, post):
    base = DRAFTS.get(category, DRAFTS["default"])
    text = base
    if category in LINK_CATEGORIES:
        text = base + "\n\n" + DISCLOSURE_LINE

    api_key = os.environ.get("TOGETHER_API_KEY")
    if api_key:
        try:
            text = llm_polish(api_key, category, post, text)
        except Exception as e:
            print(f"  LLM polish failed, using template: {e}")
    return text


def llm_polish(api_key, category, post, draft):
    allow_link = category in LINK_CATEGORIES
    prompt = (
        "Rewrite this Reddit reply so it sounds like a real person, plain "
        "Australian English, under 120 words, no hashtags, no sign-off. "
        f"{'You may keep the disclosure sentence about the poster owning the tool.' if allow_link else 'Do not mention any product or tool.'} "
        "Keep every fact exactly as written.\n\n"
        f"Thread title: {post['title']}\n"
        f"Thread text: {post['text'][:500]}\n\n"
        f"Draft to rewrite:\n{draft}"
    )
    body = json.dumps({
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(
        "https://api.together.xyz/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"].strip()


def send_discord(webhook, title, url, keyword, draft):
    content = (
        f"**New match** | matched: `{keyword}`\n"
        f"**{title}**\n{url}\n\n"
        f"**Draft reply, edit before posting:**\n>>> {draft[:1500]}"
    )
    body = json.dumps({"content": content}).encode()
    req = urllib.request.Request(webhook, data=body, headers=DISCORD_HEADERS)
    urllib.request.urlopen(req, timeout=30)


def safe_send(webhook, title, url, keyword, draft):
    """Discord failures must never crash the run."""
    if not webhook:
        return False
    try:
        send_discord(webhook, title, url, keyword, draft)
        return True
    except Exception as e:
        print(f"  warning: Discord send failed: {e}")
        return False


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen": {}}


def save_state(state):
    cutoff = datetime.now(timezone.utc).timestamp() - 30 * 86400
    state["seen"] = {
        k: v for k, v in state["seen"].items()
        if datetime.fromisoformat(v).timestamp() > cutoff
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def notify_failure(webhook, message):
    if not webhook:
        return
    try:
        body = json.dumps({"content": f"⚠️ Watcher failed: {message[:1800]}"}).encode()
        req = urllib.request.Request(webhook, data=body, headers=DISCORD_HEADERS)
        urllib.request.urlopen(req, timeout=30)
    except Exception:
        pass


def run():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    state = load_state()
    first_run = not state["seen"]

    token = None
    try:
        token = reddit_token()
    except Exception as e:
        print(f"  warning: Reddit auth failed ({e}), falling back to RSS")

    if token:
        print("Mode: Reddit OAuth API")
        all_posts, errors = fetch_api_posts(token)
    else:
        print("Mode: public RSS")
        all_posts, errors = fetch_rss_posts()

    if not all_posts and errors:
        notify_failure(webhook, "All feeds failed. " + "; ".join(errors))
        sys.exit(1)
    for e in errors:
        print(f"  warning: {e}")

    unique = {p["id"]: p for p in all_posts if p["id"]}.values()

    if first_run:
        for p in unique:
            state["seen"][p["id"]] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        safe_send(
            webhook, "Watcher installed", "",
            "first-run",
            f"Seeded {len(state['seen'])} posts as seen so you don't get a flood. "
            "Real matches start arriving on the next daily run.",
        )
        print(f"First run: seeded {len(state['seen'])} posts. Done.")
        return

    sent = 0
    for p in unique:
        if p["id"] in state["seen"]:
            continue
        state["seen"][p["id"]] = datetime.now(timezone.utc).isoformat()
        category, kw = categorize(p)
        if not category:
            continue
        draft = draft_reply(category, p)
        if safe_send(webhook, p["title"], p["link"], kw, draft):
            sent += 1
        print(f"  match: {kw} | {p['title'][:60]}")

    save_state(state)
    print(f"Done. {sent} matches sent.")


def self_test():
    sample = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>t3_test1</id><title>First BAS panic, sole trader, help</title>
        <link href="https://reddit.com/r/SmallBusinessAU/1" />
        <content type="html">How do I even work out GST for my quarterly BAS?</content></entry>
      <entry><id>t3_test2</id><title>Anyone know a good GST spreadsheet</title>
        <link href="https://reddit.com/r/AusFinance/2" />
        <content type="html">Looking for a bas spreadsheet for my sole trader work</content></entry>
      <entry><id>t3_test3</id><title>Best coffee in Melbourne</title>
        <link href="https://reddit.com/r/other/3" />
        <content type="html">totally unrelated</content></entry>
    </feed>"""
    posts = parse_feed(sample.encode())
    assert len(posts) == 3, f"expected 3 posts, got {len(posts)}"
    cat1, kw1 = categorize(posts[0])
    assert cat1 == "bas_help" and kw1 == "bas panic", f"post 1: {cat1}, {kw1}"
    cat2, kw2 = categorize(posts[1])
    assert cat2 == "spreadsheet" and kw2 == "bas spreadsheet", f"post 2: {cat2}, {kw2}"
    assert categorize(posts[2]) == (None, None), "post 3 should not match"
    d1 = draft_reply(cat1, posts[0])
    assert "Full disclosure" in d1, "bas_help draft should include disclosure"
    d3 = draft_reply("gst_maths", posts[0])
    assert "Full disclosure" not in d3, "gst_maths draft must not include disclosure"
    assert safe_send("", "t", "", "k", "d") is False, "empty webhook should be a no-op"
    print("All self-tests passed.")


if __name__ == "__main__":
    if "--test" in sys.argv:
        self_test()
    else:
        run()
