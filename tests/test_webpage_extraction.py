"""Offline regressions for the fork's actual Trafilatura/lxml webpage path."""

import json
from pathlib import Path
import tempfile
import unittest

import httpx

from knowledge_storm.utils import WebPageHelper


class WebPageExtractionTests(unittest.TestCase):
    """WebPageExtractionTests verifies extraction and snippet behavior with real parsers."""

    def setUp(self):
        """setUp creates the real helper with a local HTTP transport and serial retrieval."""
        self.pages = {}
        self.helper = WebPageHelper(
            min_char_count=30, snippet_chunk_size=80, max_thread_num=1
        )
        self.helper.httpx_client.close()
        self.helper.httpx_client = httpx.Client(
            transport=httpx.MockTransport(self.respond)
        )

    def tearDown(self):
        """tearDown closes the fixture transport without affecting other clients."""
        self.helper.httpx_client.close()

    def respond(self, request):
        """respond returns fixture HTML or HTTP404 without making network requests."""
        value = self.pages.get(str(request.url))
        return httpx.Response(
            404 if value is None else 200,
            content=b"" if value is None else value.encode(),
            request=request,
        )

    def article(self, text):
        """article wraps supplied paragraph content in an HTML article fixture."""
        return (
            "<html><head><title>Offline evidence</title></head>"
            "<body><article><h1>Offline evidence</h1><p>"
            + text
            + "</p></article></body></html>"
        )

    def test_unicode_article_excludes_scripts_tables_and_html_comments(self):
        """test_unicode_article_excludes_scripts_tables_and_html_comments exercises real extraction options."""
        text = "Research evidence preserves café and 中文. " * 12
        html = self.article(text).replace(
            "</article>",
            "<script>SCRIPT_MARKER</script>"
            "<table><tr><td>TABLE_MARKER</td></tr></table>"
            "<!-- COMMENT_MARKER --></article>",
        )
        self.pages["https://offline.test/article"] = html
        articles = self.helper.urls_to_articles(["https://offline.test/article"])
        value = articles["https://offline.test/article"]["text"]
        self.assertIn("café", value)
        self.assertIn("中文", value)
        self.assertNotIn("SCRIPT_MARKER", value)
        self.assertNotIn("TABLE_MARKER", value)
        self.assertNotIn("COMMENT_MARKER", value)

    def test_snippets_retain_text_and_skip_failed_or_short_downloads(self):
        """test_snippets_retain_text_and_skip_failed_or_short_downloads verifies actual retrieval results."""
        url = "https://offline.test/article"
        self.pages[url] = self.article(
            "Offline sentence preserves article evidence. " * 15
        )
        self.pages["https://offline.test/short"] = self.article("Tiny")
        results = self.helper.urls_to_snippets(
            [url, "https://offline.test/short", "https://offline.test/missing"]
        )
        self.assertEqual(set(results), {url})
        snippets = results[url]["snippets"]
        self.assertGreater(len(snippets), 1)
        self.assertTrue(all(0 < len(s) <= 80 for s in snippets))
        self.assertIn("article evidence", " ".join(snippets))

    def test_html_external_entity_is_not_expanded_with_positive_text_control(self):
        """test_html_external_entity_is_not_expanded_with_positive_text_control checks the exposed HTML path."""
        marker = "HARMLESS_EXTERNAL_ENTITY_MARKER"
        text = "Offline article evidence must remain readable. " * 12
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "harmless-fixture.txt"
            file.write_text(marker)
            self.pages["https://offline.test/entity"] = (
                '<!DOCTYPE html [<!ENTITY external SYSTEM "'
                + file.as_uri()
                + '">]>'
                + self.article(text + " &external;")
            )
            self.pages["https://offline.test/control"] = self.article(text + marker)
            results = self.helper.urls_to_articles(
                ["https://offline.test/entity", "https://offline.test/control"]
            )
            self.assertIn(
                "Offline article evidence",
                results["https://offline.test/entity"]["text"],
            )
            self.assertNotIn(marker, json.dumps(results["https://offline.test/entity"]))
            self.assertIn(marker, results["https://offline.test/control"]["text"])


if __name__ == "__main__":
    unittest.main()
