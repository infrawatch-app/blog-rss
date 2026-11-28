# Infrawatch blog RSS

> **2026-11-28: the Coolify app `blog-rss` (platform-ui-prod-01) is retired.** It had been stopped since
> 2026-11-24, when Framer Hosting started serving `https://infrawatch.com/rss`. This repo stays active as the
> manual generator for that file. `serve_feed.py` and the Dockerfile are kept for future automated hosting.

RSS for the public [Infrawatch research blog](https://infrawatch.com/blog).

Subscribe at **https://infrawatch.com/rss**. Framer serves the RSS XML directly
from Hosting → Files on the main domain, with RSS autodiscovery in the site's
HTML head. `/rss.xml` is a same-domain alias. There is no RSS route on the
customer application.

## How it updates

The public feed is currently a static snapshot. Regenerate it after publishing
or editing blog posts, then replace the `rss` file in Framer Hosting → Files
(path `/`) and publish the main site:

```sh
python3 build_feed.py --output-dir published
cp published/feed.xml /path/to/upload/rss
```

Only links on the public blog index are eligible. Drafts, unlisted items and
pages marked `noindex` are excluded. The generator uses each article's
published structured metadata and does not require a Framer plugin or API key.
Publication dates are included when the article supplies one; missing dates
are omitted rather than fabricated. XML is validated before writing.

Framer's documented API does not expose Hosting Files replacement, so this
native route does not refresh itself. No paid add-on was purchased. The
optional server implementation is retained for future automated hosting;
its temporary public app-domain route has been removed and the server stopped.

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
