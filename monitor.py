#!/usr/bin/env python3
"""Monitor de fontes públicas sobre os Arautos do Evangelho, sem dependências externas."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import tempfile
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 4 * 1024 * 1024
TOPICS = {
    "Jurídico": ["justic", "judici", "tribunal", "processo", "investig", "ministerio publico", "court", "lawsuit", "prosecut", "abuso", "abus", "denunci", "acusac", "alegac", "allegat", "sentenc"],
    "Governança e Igreja": ["vatican", "papa", "pope", "pontific", "govern", "diretor", "president", "comissar", "comisar", "comision", "comisari", "comissari", "commissioner", "fundador", "founder", "bento", "benedict", "joao scognamiglio", "joao cla"],
    "Educação": ["escola", "colegio", "educa", "school", "student", "aluno", "seminari", "internat", "college", "college", "universit"],
    "Finanças e patrimônio": ["financ", "patrimon", "doac", "donat", "donacion", "imovel", "imoveis", "terreno", "receita", "balanco", "orcament", "budget", "asset", "fundrais"],
    "Presença internacional": ["internacion", "international", "pais", "paises", "country", "countries", "mundo", "world", "europa", "europe", "america", "africa", "asia", "portugal", "canada", "colombia", "espanha", "spain", "italia", "italy", "franca", "france", "germany"],
    "Atividades e eventos": ["evento", "event", "missa", "liturg", "retiro", "retreat", "peregrin", "pilgrim", "concerto", "concert", "campanha", "mission", "missao", "missoes", "visita"],
    "Comunicação e cultura": ["revista", "magazine", "livro", "book", "youtube", "video", "musica", "music", "podcast", "entrevista", "interview", "documentar"],
}


def folded(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def plain(value: str, limit=1500) -> str:
    parser = TextExtractor()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())[:limit]


def normalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("URL pública HTTP(S) inválida")
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    if parsed.hostname.lower() in {"bing.com", "www.bing.com"} and parsed.path == "/news/apiclick.aspx":
        target = dict(parameters).get("url")
        if target and urlsplit(target).hostname not in {"bing.com", "www.bing.com"}:
            return normalize_url(target)
    query = [(k, v) for k, v in parameters
             if not k.casefold().startswith("utm_") and k.casefold() not in {"fbclid", "gclid", "msclkid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", urlencode(query), ""))


def iso_date(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()


def localname(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(element, *names):
    for name in names:
        for child in element:
            if localname(child.tag) == name:
                return "".join(child.itertext()).strip()
    return ""


def categorize(text: str, kind: str) -> list[str]:
    value = folded(text)
    categories = [topic for topic, words in TOPICS.items() if any(word in value for word in words)]
    if kind == "institucional":
        categories.append("Institucional")
    return categories or ["Geral"]


def parse_feed(data: bytes, source: dict) -> list[dict]:
    root = ET.fromstring(data)
    if localname(root.tag) not in {"rss", "feed", "RDF"}:
        raise ValueError("A resposta não é RSS/Atom")
    items = []
    for entry in root.iter():
        if localname(entry.tag) not in {"item", "entry"}:
            continue
        title = plain(child_text(entry, "title"), 500)
        link = child_text(entry, "link")
        if not link:
            link = next((e.attrib.get("href", "") for e in entry if localname(e.tag) == "link" and e.attrib.get("rel", "alternate") == "alternate"), "")
        if not title or not link:
            continue
        try:
            link = normalize_url(link)
        except ValueError:
            continue
        summary = plain(child_text(entry, "description", "summary", "content", "encoded"))
        terms = source.get("match_terms", [])
        if terms and not any(folded(term) in folded(title + " " + summary) for term in terms):
            continue
        publisher = plain(child_text(entry, "source"), 200)
        if not publisher:
            author = next((e for e in entry if localname(e.tag) == "author"), None)
            publisher = plain(child_text(author, "name"), 200) if author is not None else source["name"]
        kind = source.get("kind", "imprensa")
        items.append({
            "title": title, "url": link, "summary": summary,
            "published_at": iso_date(child_text(entry, "pubDate", "published", "updated", "date")),
            "publisher": publisher, "language": source.get("language", "não informado"),
            "source_id": source["id"], "kind": kind,
            "categories": categorize(title + " " + summary, kind),
        })
    return items


class OfficialNewsParser(HTMLParser):
    """Cartões públicos da listagem oficial, sem depender de classes CSS."""
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.current = None
        self.section = None
        self.items = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a":
            self.current = None
            link = urljoin(self.source["url"], attributes.get("href", ""))
            if urlsplit(link).hostname == urlsplit(self.source["url"]).hostname and urlsplit(link).path.startswith("/arautos-no-mundo/"):
                self.current = {"url": normalize_url(link), "title": [], "summary": [], "date": [], "alt": "", "priority": 0}
        if self.current is None:
            return
        if tag in {"h2", "h3", "h4"}:
            self.section = "title"
            self.current["priority"] = max(self.current["priority"], 2 if tag == "h2" else 1)
        elif tag == "small":
            self.section = "date"
        elif tag == "div" and "text-dark" in attributes.get("class", "").split():
            self.section = "summary"
        elif tag == "img":
            self.current["alt"] = attributes.get("alt", "")

    def handle_data(self, value):
        if self.current is not None and self.section:
            self.current[self.section].append(value)

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if tag in {"h2", "h3", "h4", "small", "div"}:
            self.section = None
        if tag == "a":
            item = self.current
            previous = self.items.get(item["url"])
            if previous is None or item["priority"] > previous["priority"]:
                self.items[item["url"]] = item
            self.current = None
            self.section = None


def parse_official_news(data: bytes, source: dict) -> list[dict]:
    parser = OfficialNewsParser(source)
    parser.feed(data.decode("utf-8"))
    result = []
    months = {name: index for index, name in enumerate(["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"], 1)}
    for card in parser.items.values():
        title = plain(" ".join(card["title"]) or card["alt"], 500)
        if not title:
            continue
        date_text = plain(" ".join(card["date"]))
        match = re.search(r"(\d{1,2}) de (\w+) de (\d{4})", folded(date_text))
        published = None
        if match and match[2] in months:
            try:
                published = datetime(int(match[3]), months[match[2]], int(match[1])).date().isoformat()
            except ValueError:
                pass
        summary = plain(" ".join(card["summary"]))
        result.append({"title": title, "url": card["url"], "summary": summary, "publisher": source["name"], "published_at": published, "language": source.get("language", "português"), "source_id": source["id"], "kind": "institucional", "categories": categorize(title + " " + summary, "institucional")})
    if not result:
        raise ValueError("Nenhum cartão de notícia oficial reconhecido; verificar mudança na estrutura da página")
    return result


def fetch_source(source: dict) -> list[dict]:
    url = normalize_url(source["url"])
    if urlsplit(url).scheme != "https":
        raise ValueError("As fontes devem usar HTTPS com verificação TLS")
    request = Request(url, headers={"User-Agent": "ArautosMonitor/1.1 (public sources reader)", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html"})
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}: coleta não concluída; verificar bloqueio ou indisponibilidade")
        if urlsplit(response.url).scheme != "https":
            raise ValueError("Redirecionamento para conexão sem TLS")
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Feed excede o limite de 4 MiB")
    if source.get("format") == "web_search_html":
        from public_search import parse_search
        return parse_search(data, source)
    return parse_official_news(data, source) if source.get("format") == "official_news_html" else parse_feed(data, source)


def open_database(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            summary TEXT NOT NULL, publisher TEXT NOT NULL, published_at TEXT,
            language TEXT NOT NULL, kind TEXT NOT NULL, categories TEXT NOT NULL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sightings (
            item_id TEXT NOT NULL REFERENCES items(id), source_id TEXT NOT NULL,
            PRIMARY KEY(item_id, source_id)
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, collected_at TEXT NOT NULL, status TEXT NOT NULL,
            new_count INTEGER NOT NULL, sources TEXT NOT NULL
        );
    """)
    return connection


def store_items(connection, items: list[dict], now: str) -> int:
    count = 0
    with connection:
        for item in items:
            url = normalize_url(item["url"])
            key = hashlib.sha256(url.encode()).hexdigest()
            cursor = connection.execute("""
                INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (key, url, item["title"], item.get("summary", ""), item["publisher"], item.get("published_at"), item["language"], item["kind"], json.dumps(item["categories"], ensure_ascii=False), now, now))
            count += cursor.rowcount
            connection.execute("UPDATE items SET last_seen=? WHERE id=?", (now, key))
            connection.execute("INSERT OR IGNORE INTO sightings VALUES (?,?)", (key, item["source_id"]))
    return count


def history(connection) -> list[dict]:
    result = []
    sightings = {}
    for row in connection.execute("SELECT item_id, source_id FROM sightings ORDER BY source_id"):
        sightings.setdefault(row["item_id"], []).append(row["source_id"])
    for row in connection.execute("SELECT * FROM items ORDER BY COALESCE(published_at, first_seen) DESC, id"):
        item = dict(row)
        item["categories"] = json.loads(item["categories"])
        item["sources"] = sightings.get(item["id"], [])
        result.append(item)
    return result


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def markdown(report: dict) -> str:
    def safe(value):
        return html.escape(str(value)).replace("[", "&#91;").replace("]", "&#93;").replace("\n", " ")
    lines = ["# Monitor Arautos do Evangelho — visão 360", "",
             f"Coleta: {report['collected_at_local']} (America/Sao_Paulo)",
             f"Estado: {report['status']} | Novos: {report['new_count']} | Histórico: {report['total_count']}", "",
             "Títulos e trechos pertencem às fontes. Classificação temática automática por palavras-chave; não verifica alegações. O idioma indica a edição consultada, não uma tradução.", "", "## Fontes desta coleta", ""]
    for source in report["sources"]:
        detail = f"{source['items']} itens" if source["status"] == "ok" else safe(source["error"])
        lines.append(f"- {safe(source['name'])}: {source['status']} — {detail}")
    lines += ["", "## Novidades desta coleta", ""]
    new_items = [item for item in report["items"] if item["is_new"]]
    if not new_items:
        lines.append("Nenhum novo item nas fontes consultadas. Consulte o estado das fontes acima.")
    for item in new_items:
        lines += [f"### {safe(item['title'])}", "",
                  f"Fonte: {safe(item['publisher'])} | Origem: {safe(item['kind'])} | Edição: {safe(item['language'])}",
                  f"Publicado: {item['published_at'] or 'não informado'} | Temas: {safe(', '.join(item['categories']))}",
                  f"Link: <{html.escape(item['url'], quote=True)}>", ""]
        if item["summary"]:
            lines += ["Trecho da fonte: " + safe(item["summary"]), ""]
    lines += ["## Índice do histórico", ""]
    for item in report["items"]:
        lines.append(f"- {safe(item['published_at'] or 'Data não informada')} — {safe(item['title'])} — {safe(item['publisher'])}: <{html.escape(item['url'], quote=True)}>")
    return "\n".join(lines)


def dashboard(report: dict) -> str:
    payload = json.dumps(report, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return r"""<!doctype html>
<html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arautos do Evangelho · Monitor 360</title>
<style>
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#182a38;background:#f3f5f7}body{max-width:1120px;margin:auto;padding:30px 20px}header{border-top:5px solid #146b65;padding-top:12px}h1{font-size:2.1rem;margin-bottom:6px}h2{font-size:1.2rem}.muted,small{color:#516574}a{color:#11665e}#metrics{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}.metric{background:white;border:1px solid #d5dfe3;padding:16px;min-width:130px;border-radius:10px}.metric b{display:block;font-size:1.8rem}.filters{display:flex;gap:10px;flex-wrap:wrap;position:sticky;top:0;background:#f3f5f7;padding:12px 0}input,select{padding:11px;border:1px solid #b8c6cd;border-radius:6px;background:white;font:inherit}input{flex:1;min-width:180px}article{background:white;border:1px solid #d5dfe3;border-radius:10px;padding:20px;margin:14px 0}article h2{margin:0 0 8px}.tag{display:inline-block;background:#e4f1ed;border-radius:4px;font-size:.8rem;padding:4px 7px;margin:3px}#coverage{display:flex;gap:5px;flex-wrap:wrap}.warning{padding:14px;background:#fff4dc;border-left:4px solid #a7670b}li{margin:7px 0}details{margin:22px 0}button{cursor:pointer}p{line-height:1.5}footer{margin-top:25px;border-top:1px solid #ccd7dd;padding-top:15px}
</style>
<header><small>FONTES PÚBLICAS · HISTÓRICO LOCAL</small><h1>Arautos do Evangelho — visão 360</h1><p id="stamp" class="muted"></p></header>
<div id="metrics"></div><div id="health"></div>
<p class="muted">Publicações institucionais, repercussão na imprensa e menções em fontes de autoridades. Os textos são atribuídos às fontes; alegações não são verificadas pelo coletor. Temas são sugeridos por palavras-chave.</p>
<h2>Cobertura temática no histórico</h2><div id="coverage"></div>
<details><summary>Fontes e limites da coleta</summary><ul id="sources"></ul><p>Busca em agregadores e feeds disponíveis, com nomes da organização em seis idiomas. Não cobre toda a internet, redes sociais privadas, documentos não indexados ou todos os processos judiciais. Zero resultados não comprova ausência de atividade. URLs dos agregadores podem redirecionar ao veículo.</p></details>
<div class="filters"><input id="search" type="search" aria-label="Pesquisar" placeholder="Pesquisar título, veículo ou trecho"><select id="topic" aria-label="Tema"><option value="">Todos os temas</option></select><select id="language" aria-label="Edição"><option value="">Todas as edições</option></select><select id="kind" aria-label="Origem"><option value="">Todas as origens</option></select><label><input id="newOnly" type="checkbox"> Só novidades</label></div>
<p id="count" class="muted" aria-live="polite"></p><main id="items"></main>
<footer class="muted">Relatório estático: refaça a coleta para atualizar. Datas exibidas no fuso America/Sao_Paulo; edição não equivale a idioma detectado ou tradução.</footer>
<script id="data" type="application/json">__PAYLOAD__</script>
<script>
const data=JSON.parse(document.getElementById('data').textContent), $=id=>document.getElementById(id);
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
function date(value){if(!value)return 'não informada';if(/^\d{4}-\d{2}-\d{2}$/.test(value))return value.split('-').reverse().join('/')+' (hora não informada)';return new Date(value).toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo'});}
$('stamp').textContent='Última coleta: '+date(data.collected_at)+' · Estado: '+data.status;
[[data.total_count,'itens no histórico'],[data.new_count,'novidades nesta coleta'],[data.sources.filter(x=>x.status==='ok').length+'/'+data.sources.length,'fontes consultadas']].forEach(([n,label])=>{let card=el('div',undefined,'metric');card.append(el('b',n),el('span',label));$('metrics').append(card);});
if(data.status!=='ok')$('health').append(el('p','Coleta '+data.status+': há fontes indisponíveis. O histórico anterior foi preservado.','warning'));
data.sources.forEach(s=>$('sources').append(el('li',s.name+' — '+s.status+' — '+(s.status==='ok'?s.items+' itens':s.error))));
const topics=__TOPICS__;
topics.forEach(t=>{const n=data.items.filter(i=>i.categories.includes(t)).length;$('coverage').append(el('span',t+': '+n,'tag'));});
['topic','language','kind'].forEach(field=>{let values=field==='topic'?topics:[...new Set(data.items.map(i=>i[field]))].sort();values.forEach(v=>{let option=el('option',v);option.value=v;$(field).append(option);});});
function draw(){const query=$('search').value.toLocaleLowerCase();const items=data.items.filter(i=>(!query||(i.title+' '+i.summary+' '+i.publisher).toLocaleLowerCase().includes(query))&&(!$('topic').value||i.categories.includes($('topic').value))&&(!$('language').value||i.language===$('language').value)&&(!$('kind').value||i.kind===$('kind').value)&&(!$('newOnly').checked||i.is_new));$('count').textContent=items.length+' resultado(s)';$('items').replaceChildren();items.forEach(i=>{let card=el('article'),title=el('h2'),link=el('a',i.title);link.href=i.url;link.target='_blank';link.rel='noopener noreferrer';title.append(link);card.append(title,el('small',i.publisher+' · '+i.kind+' · Edição: '+i.language),el('p','Publicação: '+date(i.published_at)+' · Encontrado: '+date(i.first_seen),'muted'));i.categories.forEach(t=>card.append(el('span',t,'tag')));if(i.is_new)card.append(el('span','NOVO','tag'));if(i.summary)card.append(el('p',i.summary));$('items').append(card);});}
['search','topic','language','kind','newOnly'].forEach(id=>$(id).addEventListener('input',draw));draw();
</script></html>""".replace("__TOPICS__", json.dumps(list(TOPICS) + ["Institucional", "Geral"], ensure_ascii=False)).replace("__PAYLOAD__", payload)


def run_once(config: dict, state_path: Path, output_dir: Path) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    results, new_count = [], 0
    connection = open_database(state_path)
    try:
        def collect(source):
            try:
                return source, fetch_source(source), None
            except Exception as exc:
                return source, [], f"{type(exc).__name__}: {exc}"
        with ThreadPoolExecutor(max_workers=4) as pool:
            for source, items, error in pool.map(collect, config["sources"]):
                result = {"id": source["id"], "name": source["name"], "url": source["url"], "status": "error" if error else "ok", "items": len(items)}
                if error:
                    result["error"] = error
                else:
                    new_count += store_items(connection, items, now)
                results.append(result)
        successes = sum(s["status"] == "ok" for s in results)
        status = "ok" if successes == len(results) else "partial" if successes else "error"
        all_items = history(connection)
        for item in all_items:
            item["is_new"] = item["first_seen"] == now
        report = {"collected_at": now, "collected_at_local": datetime.fromisoformat(now).astimezone(ZoneInfo("America/Sao_Paulo")).isoformat(), "status": status, "new_count": new_count, "total_count": len(all_items), "sources": results, "items": all_items}
        with connection:
            connection.execute("INSERT INTO runs(collected_at,status,new_count,sources) VALUES (?,?,?,?)", (now, status, new_count, json.dumps(results, ensure_ascii=False)))
        output_dir = Path(output_dir)
        atomic_write(output_dir / "latest.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        text = markdown(report)
        atomic_write(output_dir / "latest.md", text)
        run_id = datetime.fromisoformat(now).strftime("%Y%m%dT%H%M%S.%fZ")
        atomic_write(output_dir / "history" / f"{run_id}.md", text)
        atomic_write(output_dir / "painel.html", dashboard(report))
        return report
    finally:
        connection.close()


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config.get("sources"), list) or not config["sources"]:
        raise ValueError("Configure ao menos uma fonte em sources")
    ids = set()
    for source in config["sources"]:
        for key in ("id", "name", "url"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise ValueError(f"Fonte sem {key}")
        if source["id"] in ids:
            raise ValueError("Identificador de fonte duplicado: " + source["id"])
        ids.add(source["id"])
        if urlsplit(normalize_url(source["url"])).scheme != "https":
            raise ValueError("Fontes exigem HTTPS")
    interval = config.get("interval_seconds", 3600)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 60:
        raise ValueError("interval_seconds deve ser pelo menos 60")
    return config


def next_cycle_delay(started: float, interval: int, now: float) -> float:
    """Próximo horário da cadência fixa; pula horários perdidos sem sobrepor coletas."""
    slots = max(1, math.ceil(max(0, now - started) / interval))
    return max(0, started + slots * interval - now)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Uma coleta (padrão)")
    mode.add_argument("--watch", action="store_true", help="Coletar periodicamente enquanto a máquina permanecer ativa")
    parser.add_argument("--config", type=Path, default=ROOT / "sources.json")
    parser.add_argument("--state", type=Path, default=ROOT / ".monitor" / "state.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".monitor" / "reports")
    parser.add_argument("--interval", type=int, help="Intervalo em segundos (mínimo 60)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        interval = args.interval if args.interval is not None else config.get("interval_seconds", 3600)
        if interval < 60:
            parser.error("--interval deve ser pelo menos 60")
        args.state.parent.mkdir(parents=True, exist_ok=True)
        with args.state.with_suffix(args.state.suffix + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Já existe um monitor usando este banco de dados.", file=sys.stderr)
                return 1
            schedule_path = args.state.with_suffix(args.state.suffix + ".schedule.json")
            anchor = time.monotonic()
            schedule = {"pid": os.getpid(), "interval_seconds": interval, "mode": "watch" if args.watch else "once"}
            def stop_handler(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop_handler)
            while True:
                schedule.update(status="running", next_run_at=None)
                atomic_write(schedule_path, json.dumps(schedule, ensure_ascii=False, indent=2))
                report = run_once(config, args.state, args.output_dir)
                print(f"{report['collected_at_local']} status={report['status']} novos={report['new_count']} total={report['total_count']}", flush=True)
                for source in report["sources"]:
                    if source["status"] != "ok":
                        print(f"Fonte {source['id']}: {source['error']}", file=sys.stderr, flush=True)
                if not args.watch:
                    schedule.update(status="completed", last_run_at=report["collected_at_local"], last_result=report["status"])
                    atomic_write(schedule_path, json.dumps(schedule, ensure_ascii=False, indent=2))
                    return {"ok": 0, "partial": 2, "error": 1}[report["status"]]
                delay = next_cycle_delay(anchor, interval, time.monotonic())
                next_run = datetime.fromtimestamp(time.time() + delay, timezone.utc).astimezone(ZoneInfo("America/Sao_Paulo")).isoformat()
                schedule.update(status="waiting", last_run_at=report["collected_at_local"], last_result=report["status"], next_run_at=next_run)
                atomic_write(schedule_path, json.dumps(schedule, ensure_ascii=False, indent=2))
                print(f"Próxima tentativa: {next_run}; intervalo={interval}s; depende de máquina ativa.", flush=True)
                time.sleep(delay)
    except KeyboardInterrupt:
        if "schedule_path" in locals():
            schedule.update(status="stopped", next_run_at=None)
            atomic_write(schedule_path, json.dumps(schedule, ensure_ascii=False, indent=2))
        print("Monitor encerrado.", file=sys.stderr)
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Falha no monitor: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
