# cantaloupe-cache-prewarm

Pre-warms Cantaloupe's IIIF derivative cache for paged/Mirador content ahead
of real visitors, so the first page-turn isn't slow.

Built for [UO-134](https://discoverygarden.atlassian.net/browse/UO-134). The
`sitectl isle cache mirador` tool (referenced in ISLE's docs) was evaluated
first, but its manifest parser hardcodes a filter requiring image URLs to
contain `/iiif/3/` (IIIF Image API v3) — see
[`collectManifestWarmURLsForMode` in `sitectl-isle`'s `cache.go`](https://github.com/libops/sitectl-isle/blob/main/cmd/cache.go).
It correctly walks IIIF Presentation API v3 manifest structure (the format
DGI's `islandora_iiif_presentation_api` module serves), but our Cantaloupe
deployment serves IIIF **Image API v2** endpoints (`/iiif/2/...`) from
within that v3 Presentation manifest, so every image/thumbnail URL gets
silently filtered out and `sitectl` warms nothing, with no error. This image
wraps a small script that has no such version filter.

## What it does

Given a list of paged-object page URLs, for each one:
1. Fetches the page and extracts the Mirador `manifestId` from the embedded
   `drupal-settings-json`.
2. Fetches that IIIF Presentation API v3 manifest.
3. Extracts every image and thumbnail IIIF URL from the manifest's canvases.
4. Requests each one, so Cantaloupe generates and caches the derivative.

## Usage

```
docker run --rm -v $(pwd)/urls.txt:/urls.txt cantaloupe-cache-prewarm:latest \
  --url-file /urls.txt --workers 1
```

Run `--help` for the full flag list (`--dry-run`, `--workers`, etc). `--workers` defaults to 1: higher values OOM-killed a 2Gi Cantaloupe in testing. The exit code is non-zero if any image or page failed. The run aborts after `--max-consecutive-failures` (default 10) outage-type failures in a row (connection errors, timeouts, 429/502/503/504), e.g. if Cantaloupe or Drupal is down. A 404 or 500 for one bad image doesn't count. Blank lines and `#` comments in the URL file are ignored.

`--image-base-url http://svc-cantaloupe:8080` sends image requests straight to the in-cluster Cantaloupe Service (only scheme and host are replaced; the IIIF path is untouched). Use it when running in the cluster: there, the site's public hostname resolves to Drupal, which 404s `/iiif/...`, and the ALB rejects cluster-originated traffic. Page and manifest requests still go to the URLs in the URL file.

Intended to run as a Kubernetes CronJob via the `cachePrewarm` values in the
[`cantaloupe` Helm chart](https://github.com/discoverygarden/helm-charts/tree/main/charts/cantaloupe).

## Testing

`testing/create_test_book.php` is a drush script that seeds a synthetic
Paged Content object (a parent + 5 pages, each with a Service File image
media) on any Islandora site, for validating this warmer against a real
IIIF manifest without needing pre-existing content. It assumes a managed
file with fid 4 already exists on the target site to use as source image
data — point it at a different fid if that's not the case.

Run it against a target site with:
```
drush php:script testing/create_test_book.php
```

## Known gaps

- No built-in discovery of paged objects yet — the URL list is currently
  expected to be supplied (e.g. via a mounted ConfigMap). A future version
  should query Drupal/Solr directly instead of requiring a static list.
