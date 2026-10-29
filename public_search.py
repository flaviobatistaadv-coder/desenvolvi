"""Leitura de resultados públicos: não autentica contas nem acessa áreas privadas."""
from __future__ import annotations

import base64
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit, unquote


def fold(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def unwrap(url):
    for _ in range(3):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if (not parsed.netloc or parsed.hostname in {"duckduckgo.com", "html.duckduckgo.com"}) and "uddg" in query:
            url = query["uddg"][0]
        elif parsed.hostname in {"bing.com", "www.bing.com"} and parsed.path == "/ck/a" and query.get("u", [""])[0].startswith("a1"):
            encoded = query["u"][0][2:]
            try:
                url = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return ""
        else:
            break
    return url


def platform_url(url, platform):
    url = unwrap(url)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
        return None
    host = (parsed.hostname or "").lower()
    path = unquote(parsed.path)
    if platform == "tiktok" and host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
        match = re.fullmatch(r"/(@[A-Za-z0-9_.-]+)/video/(\d+)/?", path)
        if match:
            return f"https://www.tiktok.com/{match[1]}/video/{match[2]}"
    if platform == "x" and host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}:
        match = re.fullmatch(r"/([A-Za-z0-9_]+)/status/(\d+)(?:/video/\d+)?/?", path)
        if match:
            return f"https://x.com/{match[1]}/status/{match[2]}"
    if platform == "vimeo" and host in {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}:
        match = re.fullmatch(r"/(?:video/|channels/[^/]+/|groups/[^/]+/videos/)?(\d+)/?", path)
        if match:
            return f"https://vimeo.com/{match[1]}"
    return None


class SearchPage(HTMLParser):
    def __init__(self, engine):
        super().__init__(convert_charrefs=True)
        self.engine = engine
        self.results = []
        self.current = None
        self.capture = None
        self.capture_depth = 0
        self.in_bing_result = False
        self.in_heading = False
        self.hidden = 0
        self.visible = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.engine == "bing":
            if tag == "li" and "b_algo" in classes:
                self.in_bing_result = True
                self.current = None
            if self.in_bing_result and tag == "h2":
                self.in_heading = True
        title = tag == "a" and ((self.engine == "duckduckgo" and "result__a" in classes) or (self.engine == "bing" and self.in_bing_result and self.in_heading))
        if title:
            self.current = {"url": attrs.get("href", ""), "title": "", "snippet": ""}
            self.results.append(self.current)
            self.capture, self.capture_depth = "title", 1
        elif self.current is not None and ((self.engine == "duckduckgo" and "result__snippet" in classes) or (self.engine == "bing" and self.in_bing_result and tag == "p")):
            self.capture, self.capture_depth = "snippet", 1
        elif self.capture and tag not in {"img", "br", "input", "meta", "link", "hr", "wbr"}:
            self.capture_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if self.capture:
            self.capture_depth -= 1
            if self.capture_depth <= 0:
                self.capture = None
        if self.engine == "bing":
            if tag == "h2":
                self.in_heading = False
            if tag == "li":
                self.in_bing_result = False

    def handle_data(self, text):
        if not self.hidden:
            self.visible.append(text)
            if self.current is not None and self.capture:
                self.current[self.capture] += text


def parse_search(data: bytes, source: dict) -> list[dict]:
    engine = source.get("search_engine", "duckduckgo")
    if engine not in {"duckduckgo", "bing"}:
        raise ValueError("Buscador não suportado")
    parser = SearchPage(engine)
    parser.feed(data.decode("utf-8", errors="replace"))
    visible = fold(" ".join(parser.visible))
    challenge = ["unfortunately, bots use duckduckgo too", "verify you are human", "verifique se voce e humano", "captcha", "anomaly.js", "complete the following challenge"]
    if any(term in visible for term in challenge):
        raise ValueError("Busca bloqueada por desafio de acesso; não foi contornado")
    if not parser.results:
        if any(term in visible for term in ["no results found", "nenhum resultado encontrado", "no results for", "nao ha resultados para", "no hay resultados para"]):
            return []
        raise ValueError("Buscador não forneceu resultados reconhecíveis; não presumir ausência de publicações")
    organization_terms = source.get("match_terms", [])
    topic_terms = source.get("topic_terms", ["exorc", "esorc"])
    if not organization_terms:
        raise ValueError("Busca social requer termos da organização")
    result, seen = [], set()
    platform_candidates = 0
    for record in parser.results:
        url = platform_url(record["url"], source["platform"])
        if not url:
            continue
        platform_candidates += 1
        title = " ".join(record["title"].split())[:500]
        snippet = " ".join(record["snippet"].split())[:1200]
        text = fold(title + " " + snippet + " " + url)
        compact = re.sub(r"\s+", "", text)
        org_match = any(fold(term) in text or re.sub(r"\s+", "", fold(term)) in compact for term in organization_terms)
        topic_match = any(fold(term) in text for term in topic_terms)
        if not title or not org_match or not topic_match or url in seen:
            continue
        seen.add(url)
        result.append({"title": title, "url": url,
                       "summary": "Resultado indexado; reprodução não verificada. Trecho atribuído ao índice: " + snippet,
                       "publisher": source["platform"].capitalize(), "published_at": None,
                       "language": source.get("language", "edição não informada"),
                       "source_id": source["id"], "kind": "rede social (resultado indexado)",
                       "categories": ["Comunicação e cultura"]})
    if not platform_candidates:
        raise ValueError("O buscador retornou apenas URLs sem relação com a plataforma solicitada; amostra=" + repr([(record["title"][:100], unwrap(record["url"])[:180]) for record in parser.results[:2]]) + "; página=" + repr(" ".join(parser.visible)[:700]))
    if not result:
        raise ValueError("O buscador retornou resultados sem correspondência conjunta à organização e ao tema")
    return result
