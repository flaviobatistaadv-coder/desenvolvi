"""Offline behavior checks for incremental worldwide discovery."""
import html
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import discovery


def result(title="Arautos do Evangelho — exorcismo", url="https://publisher.example/video/1", snippet="Referência pública à reunião de João Clá Dias."):
    return (f'<div class="result"><a class="result__a" href="{html.escape(url, quote=True)}">{html.escape(title)}</a>'
            f'<a class="result__snippet">{html.escape(snippet)}</a></div>').encode()


EMPTY = b"<html><body>No results found for the requested query.</body></html>"


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "discovery.sqlite3"
        self.output = self.root / "reports"
        self.config = self.root / "queries.json"

    def configure(self, engines):
        queries = [{"id": f"q{index}", "engine": engine, "language": "Português", "scope": "web", "query": f'"Arautos" exorcismo {index}'} for index, engine in enumerate(engines)]
        data = {"aliases": ["Arautos", "João Clá Dias"], "topic_terms": ["exorc", "libertação"], "queries": queries}
        self.config.write_text(json.dumps(data), encoding="utf-8")
        return data

    def run_round(self, count=2, seconds=100, spacing=0):
        return discovery.run_round(self.config, self.state, self.output, count, seconds, spacing)

    def test_cursor_continues_after_reopening_and_old_urls_are_not_new(self):
        self.configure(["duckduckgo"] * 4)
        with patch.object(discovery, "fetch_query", return_value=result()) as fetch:
            first = self.run_round()
        self.assertEqual([call.args[0]["id"] for call in fetch.call_args_list], ["q0", "q1"])
        self.assertEqual((first["new_count"], first["cursor_next"]), (1, 2))
        with patch.object(discovery, "fetch_query", return_value=result()) as fetch:
            second = self.run_round()
        self.assertEqual([call.args[0]["id"] for call in fetch.call_args_list], ["q2", "q3"])
        self.assertEqual((second["new_count"], second["total_count"], second["cursor_next"]), (0, 1, 0))
        self.assertEqual(len(second["items"][0]["provenance"]), 4)
        self.assertEqual(second["remaining_in_cycle"], 0)
        self.assertEqual(second["rotations_completed"], 1)

    def test_blocked_provider_is_not_retried_and_other_provider_continues(self):
        self.configure(["duckduckgo", "bing", "duckduckgo", "bing"])
        def fetch(query, timeout=20):
            if query["engine"] == "duckduckgo":
                raise discovery.ProviderBlocked("HTTP 202")
            return EMPTY
        with patch.object(discovery, "fetch_query", side_effect=fetch) as mocked:
            report = self.run_round(4)
        self.assertEqual([call.args[0]["id"] for call in mocked.call_args_list], ["q0", "q1", "q3"])
        self.assertEqual([entry["status"] for entry in report["queries"]], ["blocked", "ok", "skipped", "ok"])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["executed_queries"], 3)
        self.assertEqual(report["cursor_next"], 0)

    def test_time_budget_preserves_cursor_before_unattempted_query(self):
        self.configure(["duckduckgo", "duckduckgo"])
        class Clock:
            value = 0
            def now(self):
                return self.value
        clock = Clock()
        def fetch(query, timeout=20):
            self.assertLessEqual(timeout, 1)
            clock.value += 2
            return result()
        with patch.object(discovery.time, "monotonic", side_effect=clock.now), patch.object(discovery, "fetch_query", side_effect=fetch) as mocked:
            report = self.run_round(2, 1)
        self.assertEqual(mocked.call_count, 1)
        self.assertTrue(report["time_budget_exhausted"])
        self.assertEqual(report["cursor_next"], 1)
        self.assertEqual(report["status"], "partial")

    def test_indexed_reference_and_plausible_lead_are_distinct_from_garbage(self):
        config = self.configure(["duckduckgo"])
        page = (result() + result("João Clá Dias — entrevista", "https://independent.example/post", "Uma referência ao fundador.")
                + result("Exorcismo sem vínculo", "https://elsewhere.example/video", "Outro assunto.")
                + result(url="https://www.youtube.com/results?search_query=Arautos+exorcismo"))
        items, filtered = discovery.parse_candidates(page, config["queries"][0], config["aliases"], config["topic_terms"])
        self.assertEqual(filtered, 0)
        self.assertEqual(len(items), 2)
        self.assertEqual({item["verification"] for item in items}, {"indexado", "pista"})
        self.assertTrue(all(item["playback_confirmed"] is False for item in items))
        self.assertTrue(all("search_query" not in item["url"] for item in items))

    def test_complete_blockage_is_error_and_reports_preserve_the_reason(self):
        self.configure(["duckduckgo", "bing", "duckduckgo", "bing"])
        with patch.object(discovery, "fetch_query", side_effect=discovery.ProviderBlocked("HTTP 403")) as fetch:
            report = self.run_round(4)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(report["status"], "error")
        saved = json.loads((self.output / "discovery-latest.json").read_text())
        self.assertEqual(saved["status"], "error")
        self.assertIn("HTTP 403", (self.output / "discovery-latest.md").read_text())
        self.assertEqual(len(saved["blocked_providers"]), 2)
        self.assertEqual(saved["queries_considered"], 4)

    def test_challenge_page_stops_provider_but_explicit_no_results_is_valid(self):
        self.configure(["duckduckgo", "duckduckgo"])
        with patch.object(discovery, "fetch_query", return_value=b"<html>Unfortunately, bots use DuckDuckGo too. Complete the following challenge.</html>") as fetch:
            blocked = self.run_round(2)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(blocked["status"], "error")
        with patch.object(discovery, "fetch_query", return_value=EMPTY):
            valid = self.run_round(2)
        self.assertEqual(valid["status"], "ok")
        self.assertEqual(valid["total_count"], 0)
        self.assertNotIn("mídia sexual", (self.output / "discovery-latest.md").read_text())

    def test_tracking_urls_are_deduplicated_and_all_provenance_is_kept(self):
        self.configure(["duckduckgo", "duckduckgo"])
        with patch.object(discovery, "fetch_query", side_effect=[result(url="https://publisher.example/video/1?utm_source=a#top"), result(url="https://publisher.example/video/1?fbclid=tracking")]):
            report = self.run_round(2)
        self.assertEqual((report["new_count"], report["total_count"]), (1, 1))
        self.assertEqual(report["items"][0]["url"], "https://publisher.example/video/1")
        self.assertEqual(len(report["items"][0]["provenance"]), 2)

    def test_concrete_sensitive_media_indication_is_not_persisted_as_a_link(self):
        self.configure(["duckduckgo"])
        page = result("Arautos — exorcismo", "https://adult.example/fictional-test-only", "underage explicit media")
        with patch.object(discovery, "ADULT_HOSTS", {"adult.example"}), patch.object(discovery, "fetch_query", return_value=page):
            report = self.run_round(1)
        self.assertEqual(report["filtered_sensitive_count"], 1)
        self.assertEqual(report["items"], [])
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn("fictional-test-only", path.read_text())

    def test_google_news_rss_keeps_aggregator_url_and_explicit_coverage_note(self):
        config = self.configure(["google_news_rss"])
        page = b'''<rss version="2.0"><channel><item>
          <title>Arautos do Evangelho: exorcismo em debate</title>
          <link>https://news.google.com/rss/articles/fixture-id?oc=5</link>
          <description><![CDATA[<p>Reportagem sobre a reuni\xc3\xa3o.</p>]]></description>
        </item></channel></rss>'''
        items, filtered = discovery.parse_candidates(page, config["queries"][0], config["aliases"], config["topic_terms"])
        self.assertEqual(filtered, 0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["verification"], "indexado")
        self.assertEqual(items[0]["url"], "https://news.google.com/rss/articles/fixture-id?oc=5")
        self.assertIn("agregador Google News", items[0]["note"])
        self.assertIn("não representa busca integral", items[0]["note"])
        params = parse_qs(urlsplit(discovery.query_url(config["queries"][0])).query)
        self.assertEqual(params["ceid"], ["BR:pt-419"])
        self.assertNotIn("when:", params["q"][0])

    def test_bing_news_rss_unwraps_video_link_and_keeps_partial_connection_as_lead(self):
        config = self.configure(["bing_news_rss"])
        wrapped = "https://www.bing.com/news/apiclick.aspx?" + urlencode({"url": "https://publisher.example/video/2?utm_source=feed", "tid": "varying"})
        page = ('<rss version="2.0"><channel><item><title>Arautos do Evangelho — comunicado</title><link>'
                + html.escape(wrapped) + '</link><description>Documento do fundador.</description></item></channel></rss>').encode()
        items, _ = discovery.parse_candidates(page, config["queries"][0], config["aliases"], config["topic_terms"])
        self.assertEqual(items[0]["url"], "https://publisher.example/video/2")
        self.assertEqual(items[0]["verification"], "pista")
        self.assertEqual(parse_qs(urlsplit(discovery.query_url(config["queries"][0])).query)["format"], ["rss"])

    def test_news_query_locales_follow_portuguese_and_english_instead_of_forcing_us(self):
        config = self.configure(["google_news_rss", "bing_news_rss"])
        google, bing = config["queries"]
        portuguese = parse_qs(urlsplit(discovery.query_url(google)).query)
        english = parse_qs(urlsplit(discovery.query_url({**google, "language": "Inglês"})).query)
        self.assertEqual((portuguese["hl"], portuguese["gl"], portuguese["ceid"]), (["pt-BR"], ["BR"], ["BR:pt-419"]))
        self.assertEqual((english["hl"], english["gl"], english["ceid"]), (["en-US"], ["US"], ["US:en"]))
        self.assertEqual(parse_qs(urlsplit(discovery.query_url(bing)).query)["mkt"], ["pt-BR"])
        self.assertEqual(parse_qs(urlsplit(discovery.query_url({**bing, "language": "Inglês"})).query)["mkt"], ["en-US"])

    def test_topic_only_news_result_is_explicitly_a_query_based_unverified_lead(self):
        config = self.configure(["google_news_rss"])
        page = b'''<rss version="2.0"><channel><item>
          <title>Video de exorcismo em debate</title>
          <link>https://publisher.example/needs-human-check</link>
          <description>Um grupo aparece em uma reportagem.</description>
        </item></channel></rss>'''
        with patch.object(discovery, "fetch_query", return_value=page):
            report = self.run_round(1)
        self.assertEqual(report["items"][0]["verification"], "pista")
        self.assertIn("apenas pelos termos da consulta", report["items"][0]["note"])
        html_query = {**config["queries"][0], "engine": "duckduckgo"}
        with self.assertRaises(discovery.UnusableSearch):
            discovery.parse_candidates(result("Video de exorcismo", "https://publisher.example/unrelated", "Sem nomes relacionados."), html_query, config["aliases"], config["topic_terms"])

    def test_empty_rss_is_valid_but_html_in_its_place_is_not(self):
        config = self.configure(["google_news_rss"])
        empty_feed = b'<rss version="2.0"><channel /></rss>'
        with patch.object(discovery, "fetch_query", return_value=empty_feed):
            report = self.run_round(1)
        self.assertEqual((report["status"], report["total_count"]), ("ok", 0))
        with self.assertRaises(ValueError):
            discovery.parse_candidates(EMPTY, config["queries"][0], config["aliases"], config["topic_terms"])

    def test_two_consecutive_inconclusive_html_responses_suspend_only_that_provider(self):
        self.configure(["bing", "google_news_rss", "bing", "bing", "google_news_rss"])
        def fetch(query, timeout=20):
            if query["engine"] == "google_news_rss":
                return b'<rss version="2.0"><channel /></rss>'
            return b'<html><div>Only an empty JavaScript shell.</div></html>'
        with patch.object(discovery, "fetch_query", side_effect=fetch) as mocked:
            report = self.run_round(5)
        self.assertEqual([call.args[0]["id"] for call in mocked.call_args_list], ["q0", "q1", "q2", "q4"])
        self.assertEqual(report["status"], "partial")
        self.assertIn("inconclusivas", report["blocked_providers"]["bing"])
        self.assertEqual(report["queries"][3]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
