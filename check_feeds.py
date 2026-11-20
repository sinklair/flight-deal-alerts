"""Check flight-deal RSS feeds and send new items to Pushover.

Env vars (set as GitHub repo secrets):
  PUSHOVER_TOKEN  - Pushover application API token
  PUSHOVER_USER   - Pushover user key
Optional:
  STATE_FILE      - path to seen-items JSON (default: state/seen.json)
  DRY_RUN=1       - print instead of sending
"""

import json
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FEEDS = {
    "The Flight Deal": "https://www.theflightdeal.com/category/flight-deals/nyc/feed/",
    "Fly4free": "https://www.fly4free.com/flight-deals/new-york/feed/",
}

STATE_FILE = os.environ.get("STATE_FILE", "state/seen.json")
MAX_SEEN_PER_FEED = 300
MAX_ALERTS_PER_RUN = 10  # guard against floods if a feed resets
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
ATOM = "{http://www.w3.org/2005/Atom}"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_items(xml_bytes):
    """Return list of (id, title, link) in feed order (newest first usually)."""
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):  # RSS 2.0
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        items.append((guid, title, link))
    for it in root.iter(f"{ATOM}entry"):  # Atom fallback
        title = (it.findtext(f"{ATOM}title") or "").strip()
        link_el = it.find(f"{ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        guid = (it.findtext(f"{ATOM}id") or link or title).strip()
        items.append((guid, title, link))
    return items


def send_pushover(source, title, link):
    if os.environ.get("DRY_RUN") == "1":
        print(f"[dry-run] {source}: {title} -> {link}")
        return
    data = urllib.parse.urlencode({
        "token": os.environ["PUSHOVER_TOKEN"],
        "user": os.environ["PUSHOVER_USER"],
        "title": f"✈️ {source}",
        "message": title or "New deal",
        "url": link,
        "url_title": "View deal",
    }).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def main():
    state = load_state()
    failures = 0

    for source, url in FEEDS.items():
        try:
            items = parse_items(fetch(url))
        except Exception as e:  # keep going if one feed is down
            print(f"ERROR {source}: {e}", file=sys.stderr)
            failures += 1
            continue

        first_run = source not in state
        seen = state.get(source, [])
        seen_set = set(seen)
        new = [i for i in items if i[0] not in seen_set]

        if first_run:
            print(f"{source}: first run, seeding {len(items)} items without alerts")
        else:
            # oldest first so notifications arrive in posting order
            to_send = list(reversed(new))[-MAX_ALERTS_PER_RUN:]
            for guid, title, link in to_send:
                send_pushover(source, title, link)
            print(f"{source}: {len(items)} items, {len(new)} new, {len(to_send)} sent")

        state[source] = ([i[0] for i in new] + seen)[:MAX_SEEN_PER_FEED]

    save_state(state)

    if failures == len(FEEDS):
        sys.exit(1)  # every feed failed - surface it in GitHub


if __name__ == "__main__":
    main()
