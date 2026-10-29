"""Fixtures for public-page link extraction and persistent catalogue history."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import catalogue


PAGE = "https://archive.example/catalogue/videos.html"


class ExtractionTests(unittest.TestCase):
    def test_gallery_caption_after_anchor_beats_global_heading_and_duplicate_label(self):
        fixture = '''<h2>Legenda</h2>
          <a href="clips/reuniao_2016.mp4">Download</a>
          <div class="gallery"><a href="clips/reuniao_2016.mp4"><img src="thumb.jpg"></a>
          <div class="desc">Reunião de exorcistas 4/5/2016 — textos em português</div></div>
          <div class="gallery"><a href="clips/concerto.mp4"><img src="thumb2.jpg"></a>
          <div class="desc">Concerto de Natal</div></div>'''

        items = catalogue.extract_media(fixture, PAGE)

        self.assertEqual(len(items), 2)
        first = next(item for item in items if "reuniao" in item["url"])
        self.assertEqual(first["url"], "https://archive.example/catalogue/clips/reuniao_2016.mp4")
        self.assertIn("Reunião de exorcistas", first["title"])
        self.assertIn("exorcismo", first["topic_terms"])
        self.assertIn("reunião", first["topic_terms"])
        self.assertEqual(first["verification"], catalogue.VERIFICATION)
        self.assertEqual(first["source_url"], PAGE)
        second = next(item for item in items if "concerto" in item["url"])
        self.assertEqual(second["topic"], "assunto a conferir")

    def test_onclick_is_inert_filename_data_and_heading_labels_relative_media(self):
        fixture = '''<h3>Depoimento sobre exorcismo</h3>
          <button onclick="playVideo('../media/trecho.webm'); dangerousFunction()">Assistir</button>
          <video data-src="../media/playlist.m3u8"></video>
          <a href="javascript:alert('unsafe.mp4')">Ativo</a>
          <source src="https://user:password@cdn.example/unsafe.mp4">
          <a href="data:video/mp4,abc">Dados</a>'''

        items = catalogue.extract_media(fixture, PAGE)

        self.assertEqual({item["url"] for item in items}, {
            "https://archive.example/media/trecho.webm",
            "https://archive.example/media/playlist.m3u8",
        })
        self.assertTrue(all(item["kind"] == "arquivo direto" for item in items))
        self.assertTrue(all("Depoimento" in item["title"] for item in items))

    def test_embeds_are_distinct_from_link_clues_and_generic_profiles_are_rejected(self):
        fixture = '''<h2>Vídeos citados</h2>
          <iframe src="https://www.youtube.com/embed/ABCdef123_-"></iframe>
          <a href="https://youtu.be/ABCdef123_-">Mesmo vídeo com título mais completo</a>
          <iframe src="https://www.facebook.com/plugins/video.php?href=https%3A%2F%2Fwww.facebook.com%2Fcanal%2Fvideos%2F123456789%2F"></iframe>
          <iframe data-src="https://ok.ru/videoembed/987654321"></iframe>
          <a href="https://vimeo.com/123456789">Vídeo citado</a>
          <a href="https://www.youtube.com/@canal">Perfil</a>
          <a href="https://www.youtube.com/results?search_query=arautos">Busca</a>
          <a href="https://www.facebook.com/canal">Perfil</a>
          <a href="https://ok.ru/profile/123">Perfil</a>
          <a href="https://vimeo.com/search?q=arautos">Busca</a>
          <iframe src="https://youtube.com.evil.example/embed/ABCdef123_-"></iframe>'''

        items = catalogue.extract_media(fixture, PAGE)

        self.assertEqual(len(items), 4)
        urls = {item["url"] for item in items}
        self.assertIn("https://www.youtube.com/watch?v=ABCdef123_-", urls)
        self.assertIn("https://ok.ru/video/987654321", urls)
        vimeo = next(item for item in items if "vimeo.com" in item["url"])
        self.assertEqual(vimeo["kind"], "pista")
        youtube = next(item for item in items if "youtube.com" in item["url"])
        self.assertEqual(youtube["kind"], "vídeo incorporado")


class PersistenceTests(unittest.TestCase):
    def test_partial_failure_then_total_failure_preserves_candidates_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "catalogues.sqlite3"
            output = Path(directory) / "reports"

            def partial(url, limiter):
                if url == catalogue.SEEDS[1]:
                    raise HTTPError(url, 403, "Forbidden", {}, None)
                return '<h2>Exorcismo: rótulo da fonte</h2><video src="https://cdn.example/clip.mp4"></video>', url, 200

            with patch.object(catalogue, "fetch_page", side_effect=partial):
                first = catalogue.run_catalogues(state, output, spacing=0)
            self.assertEqual(first["status"], "partial")
            self.assertEqual(first["new_count"], 1)
            self.assertEqual(first["total_count"], 1)
            self.assertEqual(len(first["items"][0]["source_urls"]), 2)
            failed_page = first["pages"][1]
            self.assertEqual(failed_page["http_status"], 403)
            self.assertIn("403", failed_page["error"])

            with patch.object(catalogue, "fetch_page", side_effect=OSError("network unavailable")):
                second = catalogue.run_catalogues(state, output, spacing=0)
            self.assertEqual(second["status"], "error")
            self.assertEqual(second["new_count"], 0)
            self.assertEqual(second["total_count"], 1)
            saved = json.loads((output / "catalogue-latest.json").read_text())
            self.assertEqual(saved["items"][0]["url"], "https://cdn.example/clip.mp4")
            self.assertEqual(len(saved["items"][0]["source_urls"]), 2)
            self.assertTrue((output / "catalogue-latest.md").is_file())
            self.assertEqual(len(list((output / "history").glob("catalogue-*.json"))), 2)

    def test_repeated_collection_is_not_new_and_http_202_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.sqlite3"
            output = Path(directory) / "reports"

            def fetch(url, limiter):
                return '<a href="/clip.mp4">Reunião mencionada na fonte</a>', url, 200

            with patch.object(catalogue, "fetch_page", side_effect=fetch):
                first = catalogue.run_catalogues(state, output, spacing=0)
                second = catalogue.run_catalogues(state, output, spacing=0)
            self.assertEqual(first["new_count"], 3)
            self.assertEqual(second["new_count"], 0)
            with patch.object(catalogue, "fetch_page", return_value=("<html>Challenge</html>", catalogue.SEEDS[0], 202)):
                blocked = catalogue.run_catalogues(state, output, spacing=0)
            self.assertEqual(blocked["status"], "error")
            self.assertEqual(blocked["total_count"], 3)
            self.assertTrue(all("202" in page["error"] for page in blocked["pages"]))


if __name__ == "__main__":
    unittest.main()
