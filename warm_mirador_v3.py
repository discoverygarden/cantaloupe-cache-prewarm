#!/usr/bin/env python3
"""
IIIF Presentation API v3 cache warmer.

Reads a list of paged-object page URLs, extracts the embedded Mirador
manifestId from each page's drupal-settings-json, fetches the v3 manifest,
and warms every image/thumbnail IIIF URL found in it so Cantaloupe's
derivative cache is populated before a real visitor pages through the book.

Usage:
    python3 warm_mirador_v3.py --url-file urls.txt [--workers 1] [--dry-run]
"""
import argparse
import concurrent.futures
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DRUPAL_SETTINGS_MARKER = 'data-drupal-selector="drupal-settings-json"'


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "mirador-cache-warmer/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def fetch_discard(url, timeout=60, chunk_size=64 * 1024):
    """Request a URL and drain the body without holding it in memory; the
    request is only made so Cantaloupe caches the derivative."""
    req = urllib.request.Request(url, headers={"User-Agent": "mirador-cache-warmer/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while resp.read(chunk_size):
            pass
        return resp.status


def extract_drupal_settings_json(page_html):
    """Locate the drupal-settings-json script tag and return its raw contents."""
    start = page_html.find(DRUPAL_SETTINGS_MARKER)
    if start == -1:
        return None
    open_idx = page_html.find(">", start)
    if open_idx == -1:
        return None
    open_idx += 1
    close_idx = page_html.lower().find("</script>", open_idx)
    if close_idx == -1:
        return None
    return page_html[open_idx:close_idx].strip()


def manifest_id_from_viewer(viewers, viewer_id):
    """Mirror sitectl-isle's viewer resolution: check windows[].manifestId,
    falling back to the manifests dict's keys."""
    for selector, viewer in viewers.items():
        if not isinstance(viewer, dict):
            continue
        candidate_id = viewer.get("id") or selector.lstrip("#")
        if candidate_id != viewer_id:
            continue
        for window in viewer.get("windows") or []:
            if isinstance(window, dict) and window.get("manifestId"):
                return window["manifestId"]
        manifests = viewer.get("manifests")
        if isinstance(manifests, dict):
            for manifest_id in manifests:
                if manifest_id:
                    return manifest_id
    return None


def find_manifest_id(settings):
    """Prefer settings['mirador_view_id'] if present, else the first viewer
    with a resolvable manifestId -- same precedence sitectl-isle uses."""
    mirador = settings.get("mirador")
    if not isinstance(mirador, dict):
        return None
    viewers = mirador.get("viewers")
    if not isinstance(viewers, dict) or not viewers:
        return None

    preferred = settings.get("mirador_view_id")
    if isinstance(preferred, str) and preferred:
        manifest_id = manifest_id_from_viewer(viewers, preferred)
        if manifest_id:
            return manifest_id

    for selector in viewers:
        manifest_id = manifest_id_from_viewer(viewers, selector.lstrip("#"))
        if manifest_id:
            return manifest_id
    return None


def extract_manifest_url(page_html):
    settings_json = extract_drupal_settings_json(page_html)
    if not settings_json:
        return None
    try:
        settings = json.loads(settings_json)
    except json.JSONDecodeError:
        return None
    return find_manifest_id(settings)


def extract_warm_urls(manifest):
    """Pull every image body id + thumbnail id out of a v3 manifest."""
    urls = []
    for item in manifest.get("items", []):
        for page in item.get("items", []):
            for anno in page.get("items", []):
                body = anno.get("body", {})
                if isinstance(body, dict) and body.get("id"):
                    urls.append(body["id"])
        for thumb in item.get("thumbnail", []):
            if thumb.get("id"):
                urls.append(thumb["id"])
    return urls


# HTTP statuses that mean "the service is unavailable", as opposed to a
# per-image problem (404/415/500 for a missing or corrupt source file).
OUTAGE_STATUSES = {429, 502, 503, 504}


class CircuitBreaker:
    """Trips after N consecutive outage-type failures across all workers
    (connection errors, timeouts, 429/502/503/504), so a dead or restarting
    Cantaloupe or Drupal doesn't make us wait out a timeout for every
    remaining URL. Any other HTTP response proves the service is up and
    resets the streak."""

    def __init__(self, limit):
        self.limit = limit
        self.consecutive = 0
        self.tripped = threading.Event()
        self._lock = threading.Lock()

    def record(self, outage):
        with self._lock:
            self.consecutive = self.consecutive + 1 if outage else 0
            if self.limit and self.consecutive >= self.limit:
                self.tripped.set()


def is_outage(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in OUTAGE_STATUSES
    return True


def fetch_guarded(url, breaker):
    """fetch() that feeds the breaker; used for page and manifest requests."""
    try:
        out = fetch(url)
    except Exception as e:
        breaker.record(is_outage(e))
        raise
    breaker.record(False)
    return out


def rewrite_base(url, image_base):
    """Point an image URL at image_base (scheme + host[:port]); path and query
    are kept byte-for-byte so the Cantaloupe identifier is unchanged."""
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.urlsplit(image_base)
    return urllib.parse.urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))


def warm_images(result, warm_urls, breaker):
    for u in warm_urls:
        if breaker.tripped.is_set():
            result["skipped"] = True
            result["errors"].append("aborted: too many consecutive outage-type failures")
            return
        outage = False
        try:
            status = fetch_discard(u)
            ok = 200 <= status < 300
            if not ok:
                result["errors"].append(f"{u} -> HTTP {status}")
        except urllib.error.HTTPError as e:
            ok = False
            outage = is_outage(e)
            result["errors"].append(f"{u} -> HTTP {e.code}")
        except Exception as e:
            ok = False
            outage = True
            result["errors"].append(f"{u} -> {e}")
        result["warmed" if ok else "failed"] += 1
        breaker.record(outage)


def warm_object(page_url, breaker, dry_run=False, image_base=None):
    """errors: every message for this object; fatal: page/manifest-level
    problems that stop work on it; skipped: not (fully) processed because the
    breaker tripped."""
    result = {"page": page_url, "warmed": 0, "failed": 0, "errors": [],
              "fatal": [], "skipped": False}
    try:
        if breaker.tripped.is_set():
            result["skipped"] = True
            result["errors"].append("skipped: aborted after too many consecutive outage-type failures")
            return result

        try:
            _, body = fetch_guarded(page_url, breaker)
            html = body.decode("utf-8", errors="replace")
        except Exception as e:
            result["fatal"].append(f"fetch page failed: {e}")
            return result

        manifest_url = extract_manifest_url(html)
        if not manifest_url:
            result["fatal"].append("no manifestId found in page")
            return result

        try:
            _, body = fetch_guarded(manifest_url, breaker)
            manifest = json.loads(body)
        except Exception as e:
            result["fatal"].append(f"fetch/parse manifest failed: {e}")
            return result

        warm_urls = extract_warm_urls(manifest)
        if image_base:
            warm_urls = [rewrite_base(u, image_base) for u in warm_urls]
        result["manifest_url"] = manifest_url
        result["total_urls"] = len(warm_urls)

        if dry_run:
            result["warmed"] = len(warm_urls)
            return result

        warm_images(result, warm_urls, breaker)
    except Exception as e:
        # One malformed object must not take down the whole run.
        result["fatal"].append(f"unexpected error: {type(e).__name__}: {e}")
    finally:
        result["errors"] = result["fatal"] + result["errors"]
    return result


def positive_int(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def non_negative_int(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def base_url(value):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise argparse.ArgumentTypeError("must look like http://host[:port]")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise argparse.ArgumentTypeError("must not include a path, query or fragment")
    return value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url-file", required=True, help="File with one paged-object page URL per line")
    ap.add_argument("--workers", type=positive_int, default=1,
                    help="Concurrent objects. Keep at 1 unless Cantaloupe has memory headroom: "
                         "opj_decompress subprocesses OOM-killed a 2Gi Cantaloupe at 2+ workers.")
    ap.add_argument("--max-consecutive-failures", type=non_negative_int, default=10,
                    help="Abort the run after this many outage-type failures in a row "
                         "(connection errors, timeouts, 429/502/503/504; 0 disables). "
                         "Guards against a dead or restarting Cantaloupe or Drupal.")
    ap.add_argument("--image-base-url", type=base_url, default=None, metavar="URL",
                    help="Send image requests to this scheme+host (e.g. http://svc-cantaloupe:8080) "
                         "instead of the host in the manifest. Needed in-cluster, where the public "
                         "hostname resolves to Drupal (no /iiif) and the ALB rejects cluster traffic. "
                         "Page and manifest requests still use the URLs given.")
    ap.add_argument("--dry-run", action="store_true", help="Only count URLs, don't fetch images")
    args = ap.parse_args()

    with open(args.url_file) as f:
        urls = [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]

    breaker = CircuitBreaker(args.max_consecutive_failures)
    start = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(warm_object, u, breaker, args.dry_run, args.image_base_url) for u in urls]
        for fut in concurrent.futures.as_completed(futures):
            r = fut.result()
            results.append(r)
            status = "OK" if not r["errors"] else "ERR"
            print(f"[{status}] {r['page']} warmed={r['warmed']} failed={r['failed']} total={r.get('total_urls', 0)}")
            for e in r["errors"]:
                print(f"    - {e}")

    elapsed = time.time() - start
    total_warmed = sum(r["warmed"] for r in results)
    total_failed = sum(r["failed"] for r in results)
    # Page/manifest-level problems never reach the per-image counters, so count
    # them separately: otherwise a run where every page failed would exit 0.
    pages_errored = sum(1 for r in results if r["fatal"])
    pages_skipped = sum(1 for r in results if r["skipped"])
    aborted = breaker.tripped.is_set()
    print(f"\nSummary: pages={len(urls)} processed={len(urls) - pages_skipped} warmed={total_warmed} "
          f"failed={total_failed} pages_errored={pages_errored} pages_skipped={pages_skipped} "
          f"elapsed={elapsed:.1f}s{' ABORTED' if aborted else ''}")

    if total_failed or pages_errored or aborted:
        sys.exit(1)


if __name__ == "__main__":
    main()
