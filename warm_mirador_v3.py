#!/usr/bin/env python3
"""
IIIF Presentation API v3 cache warmer.

Reads a list of paged-object page URLs, extracts the embedded Mirador
manifestId from each page's drupal-settings-json, fetches the v3 manifest,
and warms every image/thumbnail IIIF URL found in it so Cantaloupe's
derivative cache is populated before a real visitor pages through the book.

Usage:
    python3 warm_mirador_v3.py --url-file urls.txt [--workers 4] [--dry-run]
"""
import argparse
import concurrent.futures
import json
import sys
import time
import urllib.request

DRUPAL_SETTINGS_MARKER = 'data-drupal-selector="drupal-settings-json"'


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "mirador-cache-warmer/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


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


def warm_object(page_url, dry_run=False):
    result = {"page": page_url, "warmed": 0, "failed": 0, "errors": []}
    try:
        _, body = fetch(page_url)
        html = body.decode("utf-8", errors="replace")
    except Exception as e:
        result["errors"].append(f"fetch page failed: {e}")
        return result

    manifest_url = extract_manifest_url(html)
    if not manifest_url:
        result["errors"].append("no manifestId found in page")
        return result

    try:
        _, body = fetch(manifest_url)
        manifest = json.loads(body)
    except Exception as e:
        result["errors"].append(f"fetch/parse manifest failed: {e}")
        return result

    warm_urls = extract_warm_urls(manifest)
    result["manifest_url"] = manifest_url
    result["total_urls"] = len(warm_urls)

    if dry_run:
        result["warmed"] = len(warm_urls)
        return result

    for u in warm_urls:
        try:
            status, _ = fetch(u, timeout=60)
            if status == 200:
                result["warmed"] += 1
            else:
                result["failed"] += 1
                result["errors"].append(f"{u} -> HTTP {status}")
        except Exception as e:
            result["failed"] += 1
            result["errors"].append(f"{u} -> {e}")

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url-file", required=True, help="File with one paged-object page URL per line")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="Only count URLs, don't fetch images")
    args = ap.parse_args()

    with open(args.url_file) as f:
        urls = [line.strip() for line in f if line.strip()]

    start = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(lambda u: warm_object(u, args.dry_run), urls):
            results.append(r)
            status = "OK" if not r["errors"] else "ERR"
            print(f"[{status}] {r['page']} warmed={r['warmed']} failed={r['failed']} total={r.get('total_urls', 0)}")
            for e in r["errors"]:
                print(f"    - {e}")

    elapsed = time.time() - start
    total_warmed = sum(r["warmed"] for r in results)
    total_failed = sum(r["failed"] for r in results)
    print(f"\nSummary: pages={len(urls)} warmed={total_warmed} failed={total_failed} elapsed={elapsed:.1f}s")

    if total_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
