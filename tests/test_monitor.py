"""Behavior checks for the public-source monitor; fixtures never need Internet."""

import json
from datetime import datetime
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import monitor


SOURCE = {
    "id": "noticias-pt",
    "name": "Notícias em português",
    "url": "https://feeds.example/noticias.xml",
    "language": "pt-BR",
    "kind": "imprensa",
}

OTHER_SOURCE = {
    "id": "oficial",
    "name": "Publicações oficiais",
    "url": "https://oficial.example/feed.xml",
    "language": "pt-BR",
    "kind": "institucional",
}

RSS = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Not\xc3\xadcias</title>
  <item>
    <title>Arautos do Evangelho: educa\xc3\xa7\xc3\xa3o e cultura</title>
    <link>https://jornal.example/noticia?edition=2&amp;utm_source=rss#topo</link>
    <description><![CDATA[<p>Atividade p\xc3\xbablica &amp; cultural.</p>]]></description>
    <pubDate>Mon, 26 Oct 2026 10:30:00 -0300</pubDate>
    <source url="https://jornal.example/">Jornal Exemplo</source>
  </item>
  <item>
    <title>Comunicado sem data informada</title>
    <link>https://jornal.example/comunicado</link>
    <description>Documento institucional.</description>
  </item>
</channel></rss>'''

ATOM = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Publications</title>
  <entry>
    <id>tag:example,2026:article-1</id>
    <title>H\xc3\xa9rauts de l'\xc3\x89vangile : publication</title>
    <link rel="self" href="https://revue.example/api/1"/>
    <link rel="alternate" href="https://revue.example/article/1"/>
    <published>2026-10-26T13:30:00Z</published>
    <summary type="html">&lt;p&gt;Une publication &amp;amp; une activit\xc3\xa9.&lt;/p&gt;</summary>
    <author><name>R\xc3\xa9daction</name></author>
  </entry>
</feed>'''


def article(source=SOURCE, url="https://jornal.example/noticia"):
    return {
        "title": "Arautos do Evangelho: atividade pública",
        "url": url,
        "published_at": "2026-10-26T13:30:00+00:00",
        "summary": "Publicação de uma fonte identificável.",
        "publisher": "Jornal Exemplo",
        "language": source["language"],
        "source_id": source["id"],
        "kind": source["kind"],
        "categories": ["Institucional"],
    }


class FeedTests(unittest.TestCase):
    def test_rss_preserves_accents_source_and_date_without_html(self):
        items = monitor.parse_feed(RSS, SOURCE)

        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertIn("educação", first["title"])
        self.assertIn("pública & cultural", first["summary"])
        self.assertNotIn("<p>", first["summary"])
        self.assertEqual(first["publisher"], "Jornal Exemplo")
        self.assertEqual(first["source_id"], SOURCE["id"])
        self.assertEqual(first["language"], "pt-BR")
        published = datetime.fromisoformat(first["published_at"].replace("Z", "+00:00"))
        self.assertIsNotNone(published.tzinfo)
        self.assertEqual(published.timestamp(), 1793021400)
        self.assertIsNone(items[1]["published_at"])
        self.assertIsInstance(first["categories"], list)

    def test_atom_chooses_the_readable_article_link_and_decodes_text(self):
        source = {**SOURCE, "language": "fr"}
        items = monitor.parse_feed(ATOM, source)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["url"], "https://revue.example/article/1")
        self.assertIn("Hérauts de l'Évangile", item["title"])
        self.assertIn("publication & une activité", item["summary"])
        self.assertNotIn("<p>", item["summary"])
        self.assertEqual(item["language"], "fr")
        self.assertIsNotNone(item["published_at"])

    def test_invalid_xml_cannot_be_treated_as_a_successful_empty_feed(self):
        with self.assertRaises(Exception):
            monitor.parse_feed(b"<html>service unavailable</html", SOURCE)

    def test_relevance_filter_keeps_the_organization_and_discards_unrelated_news(self):
        source = {**SOURCE, "match_terms": ["Arautos do Evangelho"]}
        items = monitor.parse_feed(RSS, source)

        self.assertEqual(len(items), 1)
        self.assertIn("Arautos do Evangelho", items[0]["title"])

    def test_relevance_filter_matches_accented_alias_without_changing_display_title(self):
        source = {**SOURCE, "match_terms": ["Herauts de l'Evangile"], "language": "fr"}
        items = monitor.parse_feed(ATOM, source)

        self.assertEqual(len(items), 1)
        self.assertIn("Hérauts de l'Évangile", items[0]["title"])


class UrlTests(unittest.TestCase):
    def test_tracking_removed_but_article_identity_query_preserved(self):
        canonical = monitor.normalize_url(
            "https://jornal.example/article?id=42&utm_source=rss&fbclid=abc&gclid=x#top"
        )
        self.assertEqual(canonical, "https://jornal.example/article?id=42")
        self.assertNotEqual(canonical, monitor.normalize_url("https://jornal.example/article?id=43"))

    def test_links_cannot_inject_active_content_or_embedded_credentials(self):
        for unsafe in ("javascript:alert(1)", "data:text/html,hello", "https://user:password@example.com/item"):
            with self.subTest(url=unsafe), self.assertRaises(ValueError):
                monitor.normalize_url(unsafe)


class OfficialNewsTests(unittest.TestCase):
    source = {
        **OTHER_SOURCE,
        "url": "https://www.arautos.org/artigos/noticias",
        "format": "official_news_html",
    }

    def test_listing_prefers_complete_news_card_over_duplicate_popular_link(self):
        listing = '''<!doctype html><html><body>
          <a href="/arautos-no-mundo/atividade-cultural?utm_source=popular">
            <h4>Mais lidos: título resumido</h4>
          </a>
          <a href="/arautos-no-mundo/atividade-cultural">
            <img alt="Texto alternativo da imagem" src="/imagem.jpg">
            <small>26 de outubro de 2026</small>
            <h2>Educação e cultura em São Paulo</h2>
            <div class="text-dark chamada">Atividade pública &amp; cultural.</div>
          </a>
          <a href="https://externo.example/arautos-no-mundo/falso">
            <h2>Um link externo não é uma notícia oficial</h2>
          </a>
          <a href="/artigos/noticias"><h2>Navegação</h2></a>
        </body></html>'''.encode("utf-8")

        items = monitor.parse_official_news(listing, self.source)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["url"], "https://www.arautos.org/arautos-no-mundo/atividade-cultural")
        self.assertEqual(item["title"], "Educação e cultura em São Paulo")
        self.assertEqual(item["summary"], "Atividade pública & cultural.")
        self.assertEqual(item["published_at"], "2026-10-26")
        self.assertEqual(item["kind"], "institucional")
        self.assertEqual(item["publisher"], self.source["name"])
        self.assertIn("Institucional", item["categories"])

    def test_missing_card_structure_is_a_failure_instead_of_silent_zero_results(self):
        changed_page = b'<html><body><h1>Temporarily unavailable</h1><a href="/artigos">Articles</a></body></html>'

        with self.assertRaises(ValueError):
            monitor.parse_official_news(changed_page, self.source)


class HistoryTests(unittest.TestCase):
    def test_duplicate_from_another_source_is_not_new_after_database_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "history.sqlite3"
            first = monitor.open_database(state)
            try:
                count = monitor.store_items(first, [article()], "2026-10-26T14:00:00+00:00")
                self.assertEqual(count, 1)
                first.commit()
            finally:
                first.close()

            second = monitor.open_database(state)
            try:
                repeated = article(OTHER_SOURCE, "https://jornal.example/noticia?utm_medium=feed#top")
                count = monitor.store_items(second, [repeated], "2026-10-26T15:00:00+00:00")
                self.assertEqual(count, 0)
                second.commit()
            finally:
                second.close()


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.state = self.directory / "history.sqlite3"
        self.output = self.directory / "reports"
        self.config = {"interval": 3600, "sources": [SOURCE, OTHER_SOURCE]}

    def test_partial_failure_keeps_working_source_and_records_error_and_links(self):
        def fetch(source):
            if source["id"] == OTHER_SOURCE["id"]:
                raise OSError("temporary source failure")
            return [article()]

        with patch.object(monitor, "fetch_source", side_effect=fetch):
            result = monitor.run_once(self.config, self.state, self.output)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["total_count"], 1)
        failed = next(source for source in result["sources"] if source["id"] == OTHER_SOURCE["id"])
        self.assertIn("temporary source failure", failed["error"])
        self.assertEqual(len(result["items"]), 1)

        saved = json.loads((self.output / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "partial")
        self.assertEqual(saved["items"][0]["url"], "https://jornal.example/noticia")
        markdown = (self.output / "latest.md").read_text(encoding="utf-8")
        self.assertIn("https://jornal.example/noticia", markdown)
        self.assertTrue((self.output / "painel.html").is_file())

    def test_all_sources_failing_preserves_the_previous_history(self):
        with patch.object(monitor, "fetch_source", return_value=[article()]):
            initial = monitor.run_once(self.config, self.state, self.output)
        self.assertEqual(initial["total_count"], 1)

        with patch.object(monitor, "fetch_source", side_effect=OSError("offline")):
            failed = monitor.run_once(self.config, self.state, self.output)

        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["new_count"], 0)
        self.assertEqual(failed["total_count"], 1)
        self.assertEqual(len(failed["items"]), 1)
        self.assertTrue(all("offline" in source["error"] for source in failed["sources"]))

    def test_dashboard_treats_feed_content_as_data_even_with_script_and_template_tokens(self):
        with patch.object(monitor, "fetch_source", return_value=[article()]):
            report = monitor.run_once(self.config, self.state, self.output)

        titles = [
            'Arautos </script><script>window.OWNED="yes"</script>',
            "Arautos __TOPICS__ na imprensa",
        ]
        for title in titles:
            with self.subTest(title=title):
                report["items"][0]["title"] = title
                rendered = monitor.dashboard(report)
                script_data = re.search(
                    r'<script id="data" type="application/json">(.*?)</script>',
                    rendered,
                    re.DOTALL,
                )
                self.assertIsNotNone(script_data)
                embedded = json.loads(script_data.group(1))
                self.assertEqual(embedded["items"][0]["title"], title)
                self.assertEqual(rendered.count("<script"), 2)

    def test_bing_tracking_wrapper_changes_do_not_create_news_again(self):
        source = {**SOURCE, "id": "bing"}
        config = {"interval_seconds": 3600, "sources": [source]}

        def wrapped(tracking_id):
            return "https://www.bing.com/news/apiclick.aspx?" + urlencode({
                "ref": "FexRss",
                "aid": "",
                "tid": tracking_id,
                "url": "https://jornal.example/noticia?utm_source=bing",
            })

        with patch.object(monitor, "fetch_source", return_value=[article(source, wrapped("first"))]):
            first = monitor.run_once(config, self.state, self.output)
        with patch.object(monitor, "fetch_source", return_value=[article(source, wrapped("second"))]):
            second = monitor.run_once(config, self.state, self.output)

        self.assertEqual(first["new_count"], 1)
        self.assertEqual(second["new_count"], 0)
        self.assertEqual(second["total_count"], 1)
        self.assertEqual(second["items"][0]["url"], "https://jornal.example/noticia")


class ConfigTests(unittest.TestCase):
    def test_empty_and_duplicate_sources_are_rejected_before_any_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            for sources in ([], [SOURCE, {**SOURCE, "name": "Duplicated source"}]):
                with self.subTest(sources=sources):
                    path.write_text(json.dumps({"sources": sources}), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        monitor.load_config(path)


if __name__ == "__main__":
    unittest.main()
