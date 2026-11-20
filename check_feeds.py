"""Check flight-deal RSS feeds and send new items to Pushover.

Env vars (set as GitHub repo secrets):
  PUSHOVER_TOKEN  - Pushover application API token
  PUSHOVER_USER   - Pushover user key
Optional:
  STATE_FILE      - path to seen-items JSON (default: state/seen.json)
  DRY_RUN=1       - print instead of sending
  TEST_LATEST=1   - also send the newest item from each feed (format check)
"""

import json
import mimetypes
import os
import re
import sys
import uuid
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
MEDIA = "{http://search.yahoo.com/mrss/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Pushover attachment limit
IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def find_image(it, html_tags):
    """Best-effort image URL: enclosure, media:*, then first <img> in HTML."""
    enc = it.find("enclosure")
    if enc is not None and enc.get("type", "").startswith("image"):
        return enc.get("url")
    for tag in (f"{MEDIA}content", f"{MEDIA}thumbnail"):
        el = it.find(tag)
        if el is None:
            el = it.find(f"{MEDIA}group/{tag}")
        if el is not None and el.get("url"):
            return el.get("url")
    for tag in html_tags:
        m = IMG_RE.search(it.findtext(tag) or "")
        if m:
            return m.group(1)
    return None


def parse_items(xml_bytes):
    """Return list of (id, title, link, image) in feed order (newest first usually)."""
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):  # RSS 2.0
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        image = find_image(it, (f"{CONTENT}encoded", "description"))
        items.append((guid, title, link, image))
    for it in root.iter(f"{ATOM}entry"):  # Atom fallback
        title = (it.findtext(f"{ATOM}title") or "").strip()
        link_el = it.find(f"{ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        guid = (it.findtext(f"{ATOM}id") or link or title).strip()
        image = find_image(it, (f"{ATOM}content", f"{ATOM}summary"))
        items.append((guid, title, link, image))
    return items


def get_image(url):
    """Download image bytes for attachment; None on any problem."""
    if not url:
        return None
    try:
        raw = fetch(url)
    except Exception as e:
        print(f"  image fetch failed ({e}), sending without image")
        return None
    if len(raw) > MAX_IMAGE_BYTES:
        print("  image too large, sending without image")
        return None
    ctype = mimetypes.guess_type(urllib.parse.urlparse(url).path)[0] or "image/jpeg"
    return raw, ctype


def multipart(fields, file_field=None):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        )
    if file_field:
        name, (raw, ctype) = file_field
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="image"\r\nContent-Type: {ctype}\r\n\r\n'.encode() + raw + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_pushover(source, title, link, image_url=None):
    if os.environ.get("DRY_RUN") == "1":
        print(f"[dry-run] {source}: {title} -> {link} [img: {image_url}]")
        return
    fields = {
        "token": os.environ["PUSHOVER_TOKEN"],
        "user": os.environ["PUSHOVER_USER"],
        "title": f"✈️ {source}",
        "message": title or "New deal",
        "url": link,
        "url_title": "View deal",
    }
    image = get_image(image_url)
    body, ctype = multipart(fields, ("attachment", image) if image else None)
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json", data=body, headers={"Content-Type": ctype}
    )
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
    test_latest = os.environ.get("TEST_LATEST") == "1"

    for source, url in FEEDS.items():
        try:
            items = parse_items(fetch(url))
        except Exception as e:  # keep going if one feed is down
            print(f"ERROR {source}: {e}", file=sys.stderr)
            failures += 1
            continue

        if test_latest and items:
            print(f"{source}: test - sending latest item (image: {items[0][3]})")
            send_pushover(source, *items[0][1:])

        first_run = source not in state
        seen = state.get(source, [])
        seen_set = set(seen)
        new = [i for i in items if i[0] not in seen_set]

        if first_run:
            print(f"{source}: first run, seeding {len(items)} items without alerts")
        else:
            # oldest first so notifications arrive in posting order
            to_send = list(reversed(new))[-MAX_ALERTS_PER_RUN:]
            for guid, title, link, image in to_send:
                send_pushover(source, title, link, image)
            print(f"{source}: {len(items)} items, {len(new)} new, {len(to_send)} sent")

        state[source] = ([i[0] for i in new] + seen)[:MAX_SEEN_PER_FEED]

    save_state(state)

    if failures == len(FEEDS):
        sys.exit(1)  # every feed failed - surface it in GitHub


if __name__ == "__main__":
    main()
