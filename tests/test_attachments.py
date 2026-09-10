"""Unit tests for the attachment (image) ingest module: URL extraction from
markdown/HTML, and the offline download-into-worktree flow with a fake fetch."""

import tempfile
import unittest
import urllib.error
from pathlib import Path

from issuefleet import attachments as a


class ExtractTest(unittest.TestCase):
    def test_markdown_image_embeds(self):
        text = "before ![alt text](https://uploads.linear.app/a/b/img.png) after"
        self.assertEqual(
            a.extract_image_urls(text), ["https://uploads.linear.app/a/b/img.png"]
        )

    def test_markdown_with_title_and_angle_brackets(self):
        text = '![a](<https://x.test/one.png> "a title") ![b](https://x.test/two.jpg "t")'
        self.assertEqual(
            a.extract_image_urls(text),
            ["https://x.test/one.png", "https://x.test/two.jpg"],
        )

    def test_html_img_tag(self):
        text = '<img src="https://user-images.githubusercontent.com/1/2.png" width=40>'
        self.assertEqual(
            a.extract_image_urls(text),
            ["https://user-images.githubusercontent.com/1/2.png"],
        )

    def test_bare_attachment_hosts_kept(self):
        text = (
            "linear https://uploads.linear.app/x/y/z.png and github "
            "https://github.com/user-attachments/assets/abcd and gitlab "
            "https://gl.example/g/p/uploads/hh/pic.gif end"
        )
        self.assertEqual(
            a.extract_image_urls(text),
            [
                "https://uploads.linear.app/x/y/z.png",
                "https://github.com/user-attachments/assets/abcd",
                "https://gl.example/g/p/uploads/hh/pic.gif",
            ],
        )

    def test_bare_image_extension_kept_but_prose_link_dropped(self):
        text = "shot https://cdn.test/pic.jpg but not https://example.com/page here"
        self.assertEqual(a.extract_image_urls(text), ["https://cdn.test/pic.jpg"])

    def test_dedupe_preserves_first_order(self):
        text = (
            "![a](https://uploads.linear.app/i.png) then again "
            "![a2](https://uploads.linear.app/i.png)"
        )
        self.assertEqual(a.extract_image_urls(text), ["https://uploads.linear.app/i.png"])

    def test_non_http_and_empty(self):
        self.assertEqual(a.extract_image_urls(""), [])
        self.assertEqual(a.extract_image_urls(None), [])
        self.assertEqual(a.extract_image_urls("![x](data:image/png;base64,AAAA)"), [])

    def test_is_attachment_url(self):
        self.assertTrue(a.is_attachment_url("https://uploads.linear.app/a"))
        self.assertTrue(
            a.is_attachment_url("https://private-user-images.githubusercontent.com/a")
        )
        self.assertTrue(a.is_attachment_url("https://gl.test/g/p/uploads/h/x.png"))
        self.assertFalse(a.is_attachment_url("https://example.com/normal/page"))


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def attach_dir(self):
        return self.wt / ".agent" / "attachments"

    def test_downloads_and_returns_relative_paths(self):
        def fetch(url, headers):
            return "image/png", b"\x89PNG-bytes"

        saved = a.download_images(
            "![m](https://uploads.linear.app/o/img.png)", self.wt, fetch=fetch
        )
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].startswith(".agent/attachments/"))
        self.assertTrue(saved[0].endswith(".png"))
        self.assertTrue((self.wt / saved[0]).is_file())

    def test_auth_headers_passed_through(self):
        seen = {}

        def fetch(url, headers):
            seen[url] = headers
            return "image/jpeg", b"jpgdata"

        def auth(url):
            return {"Authorization": "Bearer secret"} if "linear" in url else {}

        a.download_images(
            "![m](https://uploads.linear.app/o/i.png) ![n](https://cdn.test/o.jpg)",
            self.wt,
            auth_for_url=auth,
            fetch=fetch,
        )
        self.assertEqual(
            seen["https://uploads.linear.app/o/i.png"], {"Authorization": "Bearer secret"}
        )
        self.assertEqual(seen["https://cdn.test/o.jpg"], {})

    def test_oversize_skipped(self):
        def fetch(url, headers):
            return "image/png", b"x" * (a.MAX_BYTES + 1)

        saved = a.download_images("![m](https://x.test/big.png)", self.wt, fetch=fetch)
        self.assertEqual(saved, [])
        self.assertFalse(self.attach_dir().exists())

    def test_non_image_content_skipped(self):
        def fetch(url, headers):
            return "text/html", b"<html>nope</html>"

        # A URL with no image extension and a non-image content-type is dropped.
        saved = a.download_images("![m](https://x.test/thing)", self.wt, fetch=fetch)
        self.assertEqual(saved, [])

    def test_fetch_error_is_swallowed(self):
        def fetch(url, headers):
            raise urllib.error.URLError("boom")

        saved = a.download_images("![m](https://x.test/i.png)", self.wt, fetch=fetch)
        self.assertEqual(saved, [])

    def test_extension_from_content_type_when_url_has_none(self):
        def fetch(url, headers):
            return "image/gif", b"GIF89a"

        saved = a.download_images("![m](https://x.test/noext)", self.wt, fetch=fetch)
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].endswith(".gif"))

    def test_same_url_not_refetched(self):
        calls = []

        def fetch(url, headers):
            calls.append(url)
            return "image/png", b"data"

        text = "![m](https://uploads.linear.app/o/img.png)"
        a.download_images(text, self.wt, fetch=fetch)
        a.download_images(text, self.wt, fetch=fetch)  # second turn, same image
        self.assertEqual(len(calls), 1)


class RedirectAuthTest(unittest.TestCase):
    """The redirect handler must not carry credentials to a different host."""

    def _redirect(self, src, dst):
        import email.message
        import urllib.request

        handler = a._AuthStrippingRedirect()
        req = urllib.request.Request(
            src, headers={"Authorization": "Bearer secret", "User-Agent": "ua"}
        )
        return handler.redirect_request(req, None, 302, "Found", email.message.Message(), dst)

    def test_authorization_stripped_cross_host(self):
        new = self._redirect("https://uploads.linear.app/a/b.png", "https://signed.s3.test/x")
        self.assertNotIn("Authorization", new.headers)

    def test_authorization_kept_same_host(self):
        new = self._redirect("https://api.test/a", "https://api.test/b")
        self.assertIn("Authorization", new.headers)


class RenderTest(unittest.TestCase):
    def test_render_image_block(self):
        self.assertEqual(a.render_image_block([]), "")
        out = a.render_image_block([".agent/attachments/x.png"])
        self.assertIn("Read tool", out)
        self.assertIn(".agent/attachments/x.png", out)


if __name__ == "__main__":
    unittest.main()
