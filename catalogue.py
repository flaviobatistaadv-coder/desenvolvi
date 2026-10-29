#!/usr/bin/env python3
"""Catalogue media links cited by known public pages, without downloading media."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
MAX_BYTES = 4 * 1024 * 1024
VERIFICATION = "link extraído da página; reprodução não verificada"
SEEDS = (
    "https://arautoleaks.com/videos.html",
    "https://omunicipio.com.br/seguranca/arautos-do-evangelho-ex-integrantes-da-instituicao-denunciam-violencia-e-tortura-psicologica-no-norte-de-sc/",
    "https://notaseinformaciones.blogspot.com/2019/10/heraldos-del-evangelio-y-exorcismos-i.html",
)
MEDIA_SUFFIX = re.compile(r"\.(?:mp4|webm|m3u8)$", re.IGNORECASE)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def folded(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))


def compact(value, limit=1000):
    return " ".join(value.split())[:limit]


def clean_url(value, page_url):
    value = html.unescape(value).strip()
    if any(ord(char) < 32 for char in value) or "\\" in value:
        raise ValueError("URL inválida")
    parsed = urlsplit(urljoin(page_url, value))
    if parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("URL pública HTTP(S) inválida")
    parameters = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                  if not key.casefold().startswith("utm_") and key.casefold() not in {"fbclid", "gclid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", urlencode(parameters), ""))


def media_url(value, page_url):
    """Return a concrete media URL and its type, or None for generic pages."""
    try:
        url = clean_url(value, page_url)
        parsed = urlsplit(url)
        if MEDIA_SUFFIX.search(unquote(parsed.path)):
            return url, "arquivo direto"
        host = parsed.hostname.lower()
        parameters = dict(parse_qsl(parsed.query))
        video_id = None
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}:
            if parsed.path == "/watch":
                video_id = parameters.get("v")
            else:
                match = re.fullmatch(r"/(?:embed|shorts|live)/([A-Za-z0-9_-]{6,})/?", parsed.path)
                video_id = match[1] if match else None
        elif host in {"youtu.be", "www.youtu.be"}:
            video_id = parsed.path.strip("/")
        if video_id and re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
            return "https://www.youtube.com/watch?" + urlencode({"v": video_id}), "platform"
        if host in {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}:
            match = re.fullmatch(r"/(?:video/)?(\d+)/?", parsed.path)
            if match:
                return "https://vimeo.com/" + match[1], "platform"
        if host in {"ok.ru", "www.ok.ru"}:
            match = re.fullmatch(r"/(?:video|videoembed)/(\d+)/?", parsed.path)
            if match:
                return "https://ok.ru/video/" + match[1], "platform"
        if host in {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}:
            if parsed.path == "/plugins/video.php":
                target = parameters.get("href", "")
                target_host = urlsplit(target).hostname
                if target_host and target_host.lower() in {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}:
                    # One public embed wrapper is enough; nested wrappers are ignored.
                    if urlsplit(target).path != "/plugins/video.php":
                        return media_url(target, page_url)
            if parsed.path.rstrip("/") in {"/watch", "/video.php"} and parameters.get("v", "").isdigit():
                return "https://www.facebook.com/watch/?" + urlencode({"v": parameters["v"]}), "platform"
            if re.fullmatch(r"/(?:[^/]+/videos|reel)/(\d+)/?", parsed.path):
                return urlunsplit(("https", "www.facebook.com", parsed.path, "", "")), "platform"
    except (TypeError, ValueError):
        return None
    return None


@dataclass
class Node:
    tag: str
    attrs: dict
    parent: Node | None = None
    children: list = field(default_factory=list)
    order: int = 0


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack = [self.root]
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.stack[-1], order=len(self.nodes))
        self.nodes.append(node)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack[-1].tag not in {"script", "style"}:
            self.stack[-1].children.append(data)


def descendants(node):
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from descendants(child)


def text(node):
    if node.tag in {"script", "style"}:
        return ""
    return compact(" ".join(text(child) if isinstance(child, Node) else child for child in node.children))


def useful_label(value):
    return compact(value) if folded(compact(value)).strip(" .:—-") not in {"", "legenda", "video", "videos", "clique aqui", "assistir", "download", "veja", "ver", "link"} else ""


def title_for(node, preceding_heading, url):
    current = node
    while current is not None:
        classes = current.attrs.get("class", "").split()
        if "gallery" in classes or current.tag == "figure":
            captions = [text(child) for child in descendants(current)
                        if child.tag == "figcaption" or "desc" in child.attrs.get("class", "").split()]
            caption = useful_label(" ".join(captions))
            if caption:
                return caption, 100
        current = current.parent
    label = useful_label(text(node))
    if label:
        return label, 80
    for key in ("title", "aria-label"):
        label = useful_label(node.attrs.get(key, ""))
        if label:
            return label, 70
    for child in descendants(node):
        label = useful_label(child.attrs.get("alt", ""))
        if label:
            return label, 60
    heading = useful_label(preceding_heading)
    if heading:
        return heading, 40
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = MEDIA_SUFFIX.sub("", name).replace("_", " ")
    return compact(name) or "Mídia citada na página", 20


def topic_for(title, url):
    value = folded(title + " " + unquote(urlsplit(url).path))
    matched = []
    for label, patterns in (("exorcismo", ("exorc",)), ("demônio", ("demon", "diabo", "devil")), ("reunião", ("reuniao", "reunio", "reunion", "meeting"))):
        if any(pattern in value for pattern in patterns):
            matched.append(label)
    return ("tema de interesse (indicação textual da fonte)" if matched else "assunto a conferir"), matched


def extract_media(html_text: str, page_url: str) -> list[dict]:
    page_url = clean_url(page_url, page_url)
    document = Document()
    document.feed(html_text)
    result = {}
    preceding_heading = ""
    for node in document.nodes:
        if node.tag in {"h2", "h3", "h4", "h5"}:
            preceding_heading = text(node)
        values = []
        if node.tag in {"a", "video", "source", "iframe"}:
            values.extend(node.attrs[key] for key in ("href", "src", "data-src") if node.attrs.get(key))
        onclick = node.attrs.get("onclick", "")
        if onclick:
            # Treat quoted filenames as inert text. Never eval or execute JavaScript.
            for match in re.finditer(r"(['\"])(.*?)\1", onclick):
                candidate = match[2]
                try:
                    if MEDIA_SUFFIX.search(unquote(urlsplit(candidate).path)):
                        values.append(candidate)
                except ValueError:
                    continue
        for value in values:
            found = media_url(value, page_url)
            if found is None:
                continue
            url, kind = found
            if kind == "platform":
                kind = "vídeo incorporado" if node.tag in {"iframe", "video", "source"} else "pista"
            title, priority = title_for(node, preceding_heading, url)
            topic, terms = topic_for(title, url)
            item = {"url": url, "title": title, "source_url": page_url, "verification": VERIFICATION,
                    "kind": kind, "topic": topic, "topic_terms": terms}
            previous = result.get(url)
            if previous is not None and previous[1]["kind"] == "vídeo incorporado" and kind == "pista":
                item["kind"] = "vídeo incorporado"
            score = (priority, len(title))
            if previous is None or score > previous[0]:
                result[url] = (score, item)
            elif previous[1]["kind"] == "pista" and kind == "vídeo incorporado":
                previous[1]["kind"] = kind
    return [item for _, item in result.values()]


class HostSpacing:
    def __init__(self, seconds):
        self.seconds = seconds
        self.last = {}

    def wait(self, url):
        host = urlsplit(url).hostname
        elapsed = time.monotonic() - self.last.get(host, -math.inf)
        if elapsed < self.seconds:
            time.sleep(self.seconds - elapsed)
        self.last[host] = time.monotonic()


class HTTPSRedirect(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def __init__(self, limiter):
        self.limiter = limiter

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = clean_url(newurl, req.full_url)
        if urlsplit(target).scheme != "https":
            raise ValueError("Redirecionamento sem TLS recusado")
        if self.limiter:
            self.limiter.wait(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch_page(url, limiter=None):
    if urlsplit(clean_url(url, url)).scheme != "https":
        raise ValueError("Fontes exigem HTTPS com validação TLS")
    if limiter:
        limiter.wait(url)
    opener = build_opener(HTTPSRedirect(limiter))
    request = Request(url, headers={"User-Agent": "ArautosCatalogue/1.0 (public-page link catalogue)", "Accept": "text/html, application/xhtml+xml"})
    with opener.open(request, timeout=20) as response:
        if response.status == 202:
            raise HTTPError(url, 202, "Bloqueio/desafio HTTP 202; sem contorno", response.headers, None)
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
            raise ValueError("Fonte não retornou HTML; mídia não será baixada")
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Página excede o limite de 4 MiB")
        charset = response.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace"), response.url, response.status


def open_database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS media (
            url TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
            topic TEXT NOT NULL, topic_terms TEXT NOT NULL,
            verification TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provenance (
            media_url TEXT NOT NULL REFERENCES media(url), source_url TEXT NOT NULL,
            title TEXT NOT NULL, PRIMARY KEY(media_url, source_url)
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, collected_at TEXT NOT NULL,
            status TEXT NOT NULL, new_count INTEGER NOT NULL, pages TEXT NOT NULL
        );
    """)
    return connection


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".catalogue-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def report_markdown(report):
    def safe(value):
        return html.escape(str(value)).replace("[", "&#91;").replace("]", "&#93;").replace("\n", " ")
    lines = ["# Catálogo de mídia citada em páginas públicas", "", f"Coleta UTC: {report['collected_at']}",
             f"Estado: {report['status']} | Novos candidatos: {report['new_count']} | Histórico: {report['total_count']}", "",
             "Os títulos e vínculos temáticos são indicações das fontes, sem verificação de autenticidade ou reprodução. Nenhum vídeo foi baixado. Novos candidatos significam URLs novas para este histórico, não vídeos inéditos.", "", "## Páginas consultadas", ""]
    for page in report["pages"]:
        detail = f"{page['items']} candidatos" if page["status"] == "ok" else page["error"]
        lines.append(f"- <{html.escape(page['url'], quote=True)}> — {page['status']}: {safe(detail)}")
    lines += ["", "## Candidatos no histórico", ""]
    for item in report["items"]:
        lines += ["### " + safe(item["title"]), "", "URL: <" + html.escape(item["url"], quote=True) + ">",
                  f"Tipo: {item['kind']} | Tema: {item['topic']}", "Verificação: " + item["verification"],
                  "Encontrado em: " + ", ".join("<" + html.escape(url, quote=True) + ">" for url in item["source_urls"]), ""]
    return "\n".join(lines)


def run_catalogues(state_path, output_dir, spacing=4):
    if isinstance(spacing, bool) or not isinstance(spacing, (int, float)) or not math.isfinite(spacing) or spacing < 0:
        raise ValueError("spacing deve ser um número finito não negativo")
    now = datetime.now(timezone.utc).isoformat()
    connection = open_database(state_path)
    limiter = HostSpacing(spacing)
    pages = []
    new_count = 0
    try:
        for seed in SEEDS:
            page = {"url": seed, "status": "error", "items": 0}
            try:
                body, final_url, status = fetch_page(seed, limiter)
                page.update({"final_url": final_url, "http_status": status})
                if status in {202, 403}:
                    raise ValueError(f"Bloqueio/desafio HTTP {status}; sem contorno")
                items = extract_media(body, final_url)
                if not items:
                    raise ValueError("Nenhum link de mídia reconhecido; verificar acesso ou mudança de estrutura")
                with connection:
                    for item in items:
                        inserted = connection.execute("INSERT OR IGNORE INTO media VALUES (?,?,?,?,?,?,?,?)",
                            (item["url"], item["title"], item["kind"], item["topic"], json.dumps(item["topic_terms"], ensure_ascii=False), item["verification"], now, now))
                        new_count += inserted.rowcount
                        connection.execute("UPDATE media SET last_seen=? WHERE url=?", (now, item["url"]))
                        connection.execute("INSERT INTO provenance VALUES (?,?,?) ON CONFLICT(media_url,source_url) DO UPDATE SET title=excluded.title",
                            (item["url"], item["source_url"], item["title"]))
                page.update({"status": "ok", "items": len(items)})
            except (OSError, ValueError, LookupError, sqlite3.Error) as exc:
                page["error"] = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, HTTPError):
                    page["http_status"] = exc.code
                    if exc.code in {202, 403}:
                        page["error"] = f"Bloqueio/desafio HTTP {exc.code}; sem contorno"
            pages.append(page)
        successes = sum(page["status"] == "ok" for page in pages)
        status = "ok" if successes == len(pages) else "partial" if successes else "error"
        items = []
        for row in connection.execute("SELECT * FROM media ORDER BY first_seen DESC, url"):
            item = dict(row)
            item["topic_terms"] = json.loads(item["topic_terms"])
            item["source_urls"] = [entry[0] for entry in connection.execute("SELECT source_url FROM provenance WHERE media_url=? ORDER BY source_url", (item["url"],))]
            item["source_url"] = item["source_urls"][0]
            item["is_new"] = item["first_seen"] == now
            items.append(item)
        report = {"collected_at": now, "status": status, "new_count": new_count,
                  "total_count": len(items), "pages": pages, "items": items}
        with connection:
            connection.execute("INSERT INTO runs(collected_at,status,new_count,pages) VALUES (?,?,?,?)", (now, status, new_count, json.dumps(pages, ensure_ascii=False)))
        output = Path(output_dir)
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        markdown = report_markdown(report)
        atomic_write(output / "catalogue-latest.json", encoded)
        atomic_write(output / "catalogue-latest.md", markdown)
        stamp = datetime.fromisoformat(now).strftime("%Y%m%dT%H%M%S.%fZ")
        atomic_write(output / "history" / f"catalogue-{stamp}.json", encoded)
        atomic_write(output / "history" / f"catalogue-{stamp}.md", markdown)
        return report
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Uma rodada nas três páginas públicas fixas (padrão)")
    parser.add_argument("--state", type=Path, default=ROOT / ".monitor" / "catalogues.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".monitor" / "reports")
    parser.add_argument("--spacing", type=float, default=4, help="Segundos mínimos entre pedidos ao mesmo host")
    args = parser.parse_args(argv)
    try:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        with args.state.with_suffix(args.state.suffix + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Já existe uma coleta usando este histórico.", file=sys.stderr)
                return 1
            report = run_catalogues(args.state, args.output_dir, args.spacing)
        print(f"{report['collected_at']} status={report['status']} novos_candidatos={report['new_count']} total={report['total_count']}")
        return {"ok": 0, "partial": 2, "error": 1}[report["status"]]
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Falha no catálogo: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
