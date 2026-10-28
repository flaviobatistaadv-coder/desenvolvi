"""Public search parsing and fixed-cadence scheduling, without network access."""

import base64
import html
import unittest
from urllib.parse import urlencode

import monitor
import public_search


def source(engine="duckduckgo", platform="tiktok"):
    return {
        "id": f"{engine}-{platform}",
        "name": f"Busca pública {platform}",
        "url": "https://html.duckduckgo.com/html/?q=arautos+exorcismo"
        if engine == "duckduckgo"
        else "https://www.bing.com/search?q=arautos+exorcismo",
        "search_engine": engine,
        "platform": platform,
        "language": "português",
        "match_terms": ["Arautos do Evangelho", "Joao Cla Dias", "Heralds of the Gospel"],
        "topic_terms": ["exorcismo", "exorcism"],
    }


def ddg_result(url, title, snippet):
    return f'''<div class="result results_links results_links_deep web-result">
      <div class="links_main result__body">
        <h2 class="result__title"><a class="result__a" href="{html.escape(url, quote=True)}">{html.escape(title)}</a></h2>
        <a class="result__snippet" href="{html.escape(url, quote=True)}">{html.escape(snippet)}</a>
      </div>
    </div>'''


def bing_result(url, title, snippet):
    return f'''<li class="b_algo"><h2><a href="{html.escape(url, quote=True)}">{html.escape(title)}</a></h2>
       <div class="b_caption"><p>{html.escape(snippet)}</p></div></li>'''


def page(*cards):
    return ("<!doctype html><html><body><main>" + "".join(cards) + "</main></body></html>").encode("utf-8")


class SearchResultTests(unittest.TestCase):
    def assert_indexed_only(self, item):
        self.assertIsNone(item["published_at"])
        self.assertEqual(item["kind"], "rede social (resultado indexado)")
        prefix = monitor.folded(item["summary"][:180])
        self.assertIn("indexad", prefix)
        self.assertIn("reproducao nao verificada", prefix)

    def test_ddg_unwraps_real_tiktok_video_and_filters_search_and_other_domains(self):
        video = "https://www.tiktok.com/@canalpublico/video/7291234567890123456"
        wrapped = "//duckduckgo.com/l/?" + urlencode({"uddg": video, "rut": "tracking"})
        fixture = page(
            ddg_result(wrapped, "Arautos do Evangelho — exorcismo", "Publicação pública com trecho descrito pelo autor."),
            ddg_result("https://www.tiktok.com/search?q=arautos", "Arautos do Evangelho: exorcismo", "Página genérica de resultados."),
            ddg_result("https://www.tiktok.com/@canalpublico", "Arautos do Evangelho: exorcismo", "Perfil sem vídeo específico."),
            ddg_result("https://www.tiktok.com.evil.example/@canal/video/7291234567890123456", "Arautos do Evangelho: exorcismo", "Domínio diferente."),
        )

        items = public_search.parse_search(fixture, source())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], video)
        self.assertEqual(items[0]["source_id"], "duckduckgo-tiktok")
        self.assertIn("Publicação pública", items[0]["summary"])
        self.assert_indexed_only(items[0])

    def test_bing_unwraps_base64_url_and_canonicalizes_twitter_status(self):
        target = "https://twitter.com/CanalPublico/status/1234567890123456789?s=20"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        redirect = "https://www.bing.com/ck/a?" + urlencode({"u": "a1" + encoded, "ntb": "1"})
        fixture = page(
            bing_result(redirect, "JOÃO CLÁ DIAS em vídeo público", "Discussão sobre EXORCÍSMO, atribuída ao publicador."),
            bing_result("https://x.com/search?q=arautos", "João Clá Dias e exorcismo", "Busca genérica."),
            bing_result("https://www.example.com/artigo", "João Clá Dias e exorcismo", "Artigo, não postagem X."),
        )

        items = public_search.parse_search(fixture, source("bing", "x"))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://x.com/CanalPublico/status/1234567890123456789")
        self.assertIn("JOÃO CLÁ DIAS", items[0]["title"])
        self.assert_indexed_only(items[0])

    def test_vimeo_numeric_video_is_accepted_but_search_page_is_not(self):
        fixture = page(
            ddg_result("https://vimeo.com/123456789", "Heralds of the Gospel — video", "A post labelled exorcism by its publisher."),
            ddg_result("https://vimeo.com/search?q=exorcism", "Heralds of the Gospel exorcism", "Search interface."),
            ddg_result("https://vimeo.com/channels/arautos", "Heralds of the Gospel exorcism", "Channel interface."),
        )

        items = public_search.parse_search(fixture, source(platform="vimeo"))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://vimeo.com/123456789")
        self.assert_indexed_only(items[0])

    def test_organization_and_topic_must_match_the_same_result(self):
        fixture = page(
            ddg_result("https://www.tiktok.com/@canal/video/7291234567890123456", "Arautos do Evangelho", "Concerto público de Natal."),
            ddg_result("https://www.tiktok.com/@cinema/video/7291234567890123457", "Exorcismo no cinema", "Filme sem relação com a organização."),
        )

        with self.assertRaises(ValueError):
            public_search.parse_search(fixture, source())

    def test_bing_response_with_only_irrelevant_results_is_diagnosed_as_failure(self):
        fixture = page(
            bing_result("https://www.example.com/windows", "Microsoft Windows updates", "Security releases."),
            bing_result("https://www.tiktok.com/@cinema/video/7291234567890123457", "Movie exorcism", "A horror film trailer."),
        )

        with self.assertRaises(ValueError):
            public_search.parse_search(fixture, source("bing"))

    def test_explicit_no_results_is_distinct_from_blocked_or_empty_interface(self):
        legitimate_empty = page('<div class="no-results">No results found for this query.</div>')
        self.assertEqual(public_search.parse_search(legitimate_empty, source()), [])

        blocked = (
            b'<html><form action="/anomaly.js"><div class="anomaly-modal">Please complete the CAPTCHA challenge</div></form></html>',
            b'<html><title>Search</title><body><form><input name="q"></form></body></html>',
            b'',
        )
        for fixture in blocked:
            with self.subTest(fixture=fixture), self.assertRaises(ValueError):
                public_search.parse_search(fixture, source())


class FixedCadenceTests(unittest.TestCase):
    def test_runtime_is_subtracted_instead_of_adding_drift_to_three_hour_interval(self):
        self.assertEqual(monitor.next_cycle_delay(100, 10800, 160), 10740)

    def test_late_completion_skips_missed_slots_without_immediate_catch_up(self):
        for now, expected in ((10910, 10790), (32560, 10740), (40000, 3300)):
            with self.subTest(now=now):
                delay = monitor.next_cycle_delay(100, 10800, now)
                self.assertEqual(delay, expected)
                self.assertGreater(delay, 0)
                self.assertLessEqual(delay, 10800)

    def test_exact_slot_boundary_is_due_now_and_fractional_time_is_preserved(self):
        self.assertEqual(monitor.next_cycle_delay(100, 10800, 10900), 0)
        self.assertAlmostEqual(monitor.next_cycle_delay(100, 10800, 10899.5), 0.5)
        self.assertAlmostEqual(monitor.next_cycle_delay(100, 10800, 10900.5), 10799.5)


if __name__ == "__main__":
    unittest.main()
