import argparse
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import warm_mirador_v3 as w

BASE = "http://svc-cantaloupe:8080"


class RewriteBase(unittest.TestCase):
    def test_rewrites_iiif_keeps_path_and_query(self):
        u = "https://site.example/iiif/2/abc~x/full/!256,256/0/default.jpg?a=1"
        self.assertEqual(w.rewrite_base(u, BASE), BASE + "/iiif/2/abc~x/full/!256,256/0/default.jpg?a=1")

    def test_leaves_non_iiif_and_non_http_alone(self):
        for u in ["https://site.example/files/a.jpg", "/sites/default/files/a.jpg",
                  "data:image/png;base64,AAA", "ftp://h/iiif/2/a"]:
            self.assertEqual(w.rewrite_base(u, BASE), u)


class BaseUrl(unittest.TestCase):
    def test_valid_is_normalised(self):
        self.assertEqual(w.base_url("http://svc:8080/"), "http://svc:8080")
        self.assertEqual(w.base_url("HTTPS://svc"), "https://svc")

    def test_rejects(self):
        for v in ["http://user:pw@svc:8080", "http://user@svc", "http://svc:abc", "svc:8080",
                  "ftp://svc", "http://svc/path", "http://svc?q=1", "http://:8080"]:
            with self.assertRaises(argparse.ArgumentTypeError, msg=v):
                w.base_url(v)


class SeparateBreakers(unittest.TestCase):
    PAGE_URLS = [f"{BASE}/iiif/2/{i}/full/full/0/default.jpg" for i in range(3)]

    def run_objects(self, shared):
        # Limit above the 3 images per book: only a streak spanning books can trip it.
        drupal = w.CircuitBreaker(4)
        images = drupal if shared else w.CircuitBreaker(4)
        dead = urllib.error.URLError("connection refused")
        with mock.patch.object(w, "fetch_guarded", side_effect=lambda u, b: (b.record(False), (200, b"x"))[1]), \
             mock.patch.object(w, "extract_manifest_url", return_value="http://m"), \
             mock.patch.object(w.json, "loads", return_value={}), \
             mock.patch.object(w, "extract_warm_urls", return_value=self.PAGE_URLS), \
             mock.patch.object(w, "fetch_discard", side_effect=dead):
            return [w.warm_object(f"http://p/{i}", drupal, False, BASE, images) for i in range(4)]

    def test_shared_breaker_never_trips_on_small_books(self):
        # Documents the bug the split fixes: each Drupal success resets the streak.
        rs = self.run_objects(shared=True)
        self.assertEqual([r["failed"] for r in rs], [3, 3, 3, 3])
        self.assertFalse(any(r["skipped"] for r in rs))

    def test_separate_breaker_trips_and_skips_later_objects(self):
        rs = self.run_objects(shared=False)
        self.assertEqual([r["failed"] for r in rs[:2]], [3, 1])
        self.assertTrue(all(r["skipped"] for r in rs[2:]))


if __name__ == "__main__":
    unittest.main()
