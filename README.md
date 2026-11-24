# Infrawatch blog RSS

RSS for the public [Infrawatch research blog](https://infrawatch.com/blog).

Subscribe at **https://infrawatch.com/rss**. The feed runs on existing
Infrawatch hosting at https://app.infrawatch.com/blog-rss.xml.
Framer redirects the subscription URL and includes RSS autodiscovery in the
site's HTML head. No Framer layout change is needed.

## How it updates

The feed server refreshes the published blog every 30 minutes when requested.
Only links on the public blog index are eligible. Drafts, unlisted items and
pages marked `noindex` are excluded. The generator uses each article's
published structured metadata and does not require a Framer plugin or API key.
Publication dates are included when the article supplies one; missing dates
are omitted rather than fabricated.

The generated feed is validated before replacing the cached copy. A failed
refresh leaves the last successfully generated feed available. The repository
includes an initial public snapshot so restarts can serve a valid feed while
refreshing. HTTP caching and conditional requests reduce unnecessary transfers.

The container uses an existing Infrawatch server. No new subscription, server,
third-party Python package or stored secret is needed. GitHub Actions is not
required.

## Run locally

Python 3.13 or later:

```sh
python3 -m unittest discover -v
python3 build_feed.py --output-dir published
python3 serve_feed.py
```

The server listens on port 8080 and serves `/`, `/blog-rss.xml` and `/healthz`.
Use the Dockerfile for deployment. Keep the public subscription URL stable if
hosting changes.

The approach was inspired by [clearlysid/framer-rss](https://github.com/clearlysid/framer-rss).
This generator is a separate implementation tailored to the published
Infrawatch blog; it does not use that project's markup and date assumptions.
