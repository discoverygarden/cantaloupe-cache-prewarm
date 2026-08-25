# cantaloupe-cache-prewarm

Pre-warms Cantaloupe's IIIF derivative cache for paged/Mirador content ahead
of real visitors, so the first page-turn isn't slow.

Built for [UO-134](https://discoverygarden.atlassian.net/browse/UO-134). The
`sitectl isle cache mirador` tool (referenced in ISLE's docs) only supports
IIIF Presentation API v2 manifests; DGI's `islandora_iiif_presentation_api`
module serves v3, which `sitectl` silently fails to warm. This image wraps a
small script that parses the v3 manifest format instead.

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
  --url-file /urls.txt --workers 4
```

Run `--help` for the full flag list (`--dry-run`, `--workers`, etc).

Intended to run as a Kubernetes CronJob via the `cachePrewarm` values in the
[`cantaloupe` Helm chart](https://github.com/discoverygarden/helm-charts/tree/main/charts/cantaloupe).

## Known gaps

- No built-in discovery of paged objects yet — the URL list is currently
  expected to be supplied (e.g. via a mounted ConfigMap). A future version
  should query Drupal/Solr directly instead of requiring a static list.
