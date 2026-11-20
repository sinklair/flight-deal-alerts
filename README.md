# Flight deal alerts

Checks NYC flight-deal RSS feeds every ~15 min via GitHub Actions and sends new deals to Pushover.

- Feeds are listed at the top of `check_feeds.py`
- Seen items are tracked in `state/seen.json` (first run seeds without alerting)
- Secrets needed: `PUSHOVER_TOKEN` (app token) and `PUSHOVER_USER` (user key)
- Manual run / test push: Actions tab > Check flight deals > Run workflow
