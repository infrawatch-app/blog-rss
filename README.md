# Infrawatch blog RSS

RSS for the public [Infrawatch research blog](https://infrawatch.com/blog).

Subscribe at **https://infrawatch.com/rss**. The feed is hosted at
https://infrawatch-app.github.io/blog-rss/feed.xml through GitHub Pages.
Framer redirects the subscription URL and includes RSS autodiscovery in the
site's HTML head.

## How it updates

GitHub Actions checks the published blog at 13 and 43 minutes past each hour,
and whenever the generator changes. GitHub may delay scheduled runs.
Only links on the public blog index are eligible. Drafts, unlisted items and
pages marked `noindex` are excluded. The generator uses each article's
published structured metadata and does not require a Framer plugin or API key.
Publication dates are included when the article supplies one; missing dates
are omitted rather than fabricated.

The generated feed is validated before deployment. A failed refresh leaves
the last successfully published feed available. Successful refreshes commit
the generated public snapshot, including its check timestamp, so the public
repository remains active and its scheduled workflow does not expire after
60 days without source changes.

GitHub Pages and standard Actions runners are free for this public repository.
No subscription, server, third-party Python package or stored secret is needed.
The workflow uses only its short-lived GitHub Actions token.

## Run locally

Python 3.13 or later:

```sh
python3 -m unittest discover -v
python3 build_feed.py --output-dir published
```

Use **Actions → Refresh blog RSS → Run workflow** to refresh immediately after
publishing an article. Change the schedule in `.github/workflows/publish.yml`.
Keep the public subscription URL stable if hosting changes.

## Sources

- [GitHub Pages on free plans](https://docs.github.com/en/pages/getting-started-with-github-pages)
- [Free standard runners for public repositories](https://docs.github.com/en/actions/concepts/billing-and-usage)

The approach was inspired by [clearlysid/framer-rss](https://github.com/clearlysid/framer-rss).
This generator is a separate implementation tailored to the published
Infrawatch blog; it does not use that project's markup and date assumptions.
