#!/usr/bin/env python3
"""Descoberta incremental de referências públicas, com limites e proveniência."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from monitor import atomic_write, normalize_url, parse_feed
from public_search import SearchPage, fold, unwrap

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 4 * 1024 * 1024
ENGINES = {
    "duckduckgo": "https://html.duckduckgo.com/html/",
    "bing": "https://www.bing.com/search",
    "google_news_rss": "https://news.google.com/rss/search",
    "bing_news_rss": "https://www.bing.com/news/search",
}
RSS_ENGINES = {"google_news_rss", "bing_news_rss"}
NEWS_LOCALES = {
    "Português": ("pt-BR", "BR", "BR:pt-419", "pt-BR"),
    "Inglês": ("en-US", "US", "US:en", "en-US"),
    "Espanhol": ("es", "ES", "ES:es", "es-ES"),
    "Francês": ("fr", "FR", "FR:fr", "fr-FR"),
    "Italiano": ("it", "IT", "IT:it", "it-IT"),
    "Alemão": ("de", "DE", "DE:de", "de-DE"),
    "Polonês": ("pl", "PL", "PL:pl", "pl-PL"),
    "Russo": ("ru", "RU", "RU:ru", "ru-RU"),
    "Neerlandês": ("nl", "NL", "NL:nl", "nl-NL"),
    "Árabe": ("ar", "EG", "EG:ar", "ar-EG"),
    "Japonês": ("ja", "JP", "JP:ja", "ja-JP"),
    "Chinês": ("zh-TW", "TW", "TW:zh-Hant", "zh-TW"),
}
CHALLENGES = (
    "unfortunately, bots use duckduckgo too", "verify you are human",
    "verifique se voce e humano", "complete the following challenge",
    "anomaly.js", "captcha",
)
NO_RESULTS = ("no results found", "nenhum resultado encontrado", "no results for")
ADULT_HOSTS = {"xvideos.com", "xhamster.com", "pornhub.com"}
MINOR_MARKERS = ("underage", "under age", "child", "children", "minor", "menor", "crianca", "enfant", "bambin", "nino", "nina", "under 18", "mineur", "minderjahr", "minderjar", "nielet", "dzieci", "несовершеннолет", "малолет", "ребен", "дети", "قاصر", "طفل", "未成年", "儿童", "兒童", "児童", "子供")
SEXUAL_MEDIA_MARKERS = ("porn", "naked", "nude", "nudez", "sexual video", "sexo explicito", "sex tape", "sex video", "порно", "إباح", "ポルノ", "裸", "性爱", "性愛")


class ProviderBlocked(RuntimeError):
    """Acesso exige desafio ou foi recusado; não tentar contornar."""


class UnusableSearch(ValueError):
    """Formato/relevância inconclusivos; não equivale a ausência de conteúdo."""


def query_url(query: dict) -> str:
    parameters = {"q": query["query"]}
    hl, gl, ceid, market = NEWS_LOCALES.get(query["language"], NEWS_LOCALES["Inglês"])
    if query["engine"] == "google_news_rss":
        parameters.update(hl=hl, gl=gl, ceid=ceid)
    elif query["engine"] == "bing_news_rss":
        parameters["format"] = "rss"
        parameters["mkt"] = market
    return ENGINES[query["engine"]] + "?" + urlencode(parameters)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", fold(text))


def related(text: str, terms: list[str]) -> bool:
    normalized = fold(text)
    dense = compact(text)
    return any(fold(term) in normalized or compact(term) in dense for term in terms if term.strip())


def adult_host(host: str) -> bool:
    return any(host == value or host.endswith("." + value) for value in ADULT_HOSTS)


def unsafe_media_reference(text: str, host: str) -> bool:
    """Filtra indicação concreta de mídia sexual de menores sem persistir URL."""
    value = fold(text)
    minor = any(re.search(r"\b" + re.escape(term) + r"\w*\b", value) for term in MINOR_MARKERS)
    explicit = adult_host(host) or any(term in value for term in SEXUAL_MEDIA_MARKERS)
    return minor and explicit


def candidate_url(value: str) -> str | None:
    try:
        url = normalize_url(unwrap(value))
        parsed = urlsplit(url)
    except (ValueError, UnicodeDecodeError):
        return None
    host = parsed.hostname or ""
    search_hosts = {"www.google.com", "google.com", "www.bing.com", "bing.com", "duckduckgo.com", "html.duckduckgo.com"}
    if host in search_hosts:
        return None
    generic = {
        "www.youtube.com": ("/results", "/feed/"),
        "www.tiktok.com": ("/search", "/tag/"),
        "www.facebook.com": ("/search",),
        "www.instagram.com": ("/explore/tags/",),
        "x.com": ("/search", "/i/flow/"),
        "twitter.com": ("/search",),
        "vimeo.com": ("/search",),
    }
    if any(parsed.path.startswith(prefix) for prefix in generic.get(host, ())):
        return None
    return url


def parse_candidates(data: bytes, query: dict, aliases: list[str], topic_terms: list[str]) -> tuple[list[dict], int]:
    if query["engine"] in RSS_ENGINES:
        feed = parse_feed(data, {"id": query["id"], "name": query["engine"], "url": query_url(query), "language": query["language"], "kind": "índice de notícias"})
        records = [{"url": item["url"], "title": item["title"], "snippet": item["summary"]} for item in feed]
        if not records:
            return [], 0
    else:
        parser = SearchPage(query["engine"])
        raw = data.decode("utf-8", errors="replace")
        parser.feed(raw)
        visible = fold(" ".join(parser.visible))
        if any(marker in visible for marker in CHALLENGES) or "anomaly.js" in raw.lower():
            raise ProviderBlocked("Desafio de acesso identificado; provedor suspenso nesta rodada")
        if not parser.results:
            if any(marker in visible for marker in NO_RESULTS):
                return [], 0
            raise UnusableSearch("Interface sem resultados reconhecíveis; não presumir ausência de referências")
        records = parser.results
    result, seen, filtered = [], set(), 0
    for record in records:
        url = candidate_url(record["url"])
        if url is None:
            continue
        title = " ".join(record["title"].split())[:500]
        snippet = " ".join(record["snippet"].split())[:1500]
        evidence = title + " " + snippet + " " + url
        host = urlsplit(url).hostname or ""
        if unsafe_media_reference(evidence, host):
            filtered += 1
            continue
        name_match = related(evidence, aliases)
        topic_match = related(evidence, topic_terms)
        query_only_lead = query["engine"] in RSS_ENGINES and topic_match and related(query["query"], aliases)
        if not title or (not name_match and not query_only_lead) or url in seen:
            continue
        seen.add(url)
        level = "indexado" if name_match and topic_match else "pista"
        note = "Referência do índice público; reprodução, autoria e autenticidade não verificadas."
        if not name_match:
            note += " Associação ao nome apenas pelos termos da consulta; vínculo precisa ser conferido."
        if query["engine"] in RSS_ENGINES:
            note += " Resultado de índice de notícias; não representa busca integral de blogs ou redes sociais."
        if host == "news.google.com":
            note += " URL do agregador Google News; destino final não verificado."
        result.append({"url": url, "title": title, "snippet": snippet,
                       "verification": level, "playback_confirmed": False,
                       "language": query["language"], "scope": query["scope"],
                       "engine": query["engine"], "query_id": query["id"],
                       "search_url": query_url(query),
                       "note": note})
    if not result and not filtered:
        raise UnusableSearch("Resultados sem conexão identificável aos nomes pesquisados; possível busca irrelevante")
    return result, filtered


def fetch_query(query: dict, timeout: float = 20) -> bytes:
    request = Request(query_url(query), headers={
        "User-Agent": "ArautosDiscovery/1.0 (public index research)",
        "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml" if query["engine"] in RSS_ENGINES else "text/html,application/xhtml+xml",
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status in {202, 403, 429}:
                raise ProviderBlocked(f"HTTP {response.status}; sem contorno ou repetição neste provedor")
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}: busca não concluída")
            if urlsplit(response.url).scheme != "https":
                raise ValueError("Redirecionamento sem TLS recusado")
            data = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {202, 403, 429}:
            raise ProviderBlocked(f"HTTP {exc.code}; provedor suspenso nesta rodada") from exc
        raise ValueError(f"HTTP {exc.code}: busca não concluída") from exc
    if len(data) > MAX_BYTES:
        raise ValueError("Página excede o limite de 4 MiB")
    return data


def load_config(path: Path) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    queries = config.get("queries")
    if not isinstance(queries, list) or not queries or not config.get("aliases") or not config.get("topic_terms"):
        raise ValueError("Configuração requer queries, aliases e topic_terms")
    ids = set()
    for query in queries:
        if not isinstance(query, dict) or any(not isinstance(query.get(key), str) or not query[key].strip() for key in ("id", "engine", "language", "scope", "query")):
            raise ValueError("Consulta incompleta")
        if query["engine"] not in ENGINES or query["id"] in ids:
            raise ValueError("Provedor inválido ou identificador duplicado")
        ids.add(query["id"])
    return config


def database(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discoveries (
            url TEXT PRIMARY KEY, title TEXT NOT NULL, snippet TEXT NOT NULL,
            verification TEXT NOT NULL, language TEXT NOT NULL, scope TEXT NOT NULL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sightings (
            url TEXT NOT NULL REFERENCES discoveries(url), query_id TEXT NOT NULL,
            engine TEXT NOT NULL, search_url TEXT NOT NULL,
            PRIMARY KEY(url, query_id)
        );
        CREATE TABLE IF NOT EXISTS query_runs (
            id INTEGER PRIMARY KEY, collected_at TEXT NOT NULL, query_id TEXT NOT NULL,
            engine TEXT NOT NULL, language TEXT NOT NULL, scope TEXT NOT NULL,
            query TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
            results_count INTEGER NOT NULL, new_count INTEGER NOT NULL
        );
    """)
    return db


def setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def save_setting(db, key, value):
    db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))


def save_results(db, values, now):
    count = 0
    for item in values:
        cursor = db.execute("INSERT OR IGNORE INTO discoveries VALUES (?,?,?,?,?,?,?,?)", (
            item["url"], item["title"], item["snippet"], item["verification"], item["language"], item["scope"], now, now))
        count += cursor.rowcount
        db.execute("UPDATE discoveries SET last_seen=? WHERE url=?", (now, item["url"]))
        if item["verification"] == "indexado":
            db.execute("UPDATE discoveries SET verification='indexado',title=?,snippet=?,language=?,scope=? WHERE url=?", (item["title"], item["snippet"], item["language"], item["scope"], item["url"]))
        db.execute("INSERT OR IGNORE INTO sightings VALUES (?,?,?,?)", (item["url"], item["query_id"], item["engine"], item["search_url"]))
    return count


def saved_results(db, now, aliases):
    values = []
    for row in db.execute("SELECT * FROM discoveries ORDER BY last_seen DESC, url"):
        value = dict(row)
        value["is_new"] = value["first_seen"] == now
        value["playback_confirmed"] = False
        value["provenance"] = [dict(r) for r in db.execute("SELECT query_id,engine,search_url FROM sightings WHERE url=? ORDER BY query_id", (value["url"],))]
        for provenance in value["provenance"]:
            provenance["coverage"] = "índice de notícias; não é pesquisa integral de blogs/redes" if provenance["engine"] in RSS_ENGINES else "índice público da web"
        value["note"] = "URL do agregador Google News; destino final e reprodução não verificados." if urlsplit(value["url"]).hostname == "news.google.com" else "Reprodução, autoria e autenticidade não verificadas."
        if not related(value["title"] + " " + value["snippet"] + " " + value["url"], aliases):
            value["note"] += " Associação ao nome apenas pelos termos da consulta; vínculo precisa ser conferido."
        values.append(value)
    return values


def render_markdown(report):
    def safe(value):
        return html.escape(str(value)).replace("\n", " ").replace("[", "&#91;").replace("]", "&#93;")
    lines = ["# Descoberta mundial de referências — Arautos / João Clá Dias", "",
             f"Rodada: {report['collected_at_local']} (America/Sao_Paulo)",
             f"Estado: {report['status']}; novos links: {report['new_count']}; histórico: {report['total_count']}.", "",
             "Resultados são atribuídos aos índices públicos. 'Indexado' indica nome e tema no resultado; 'pista' indica conexão parcial, que pode vir apenas dos termos de uma consulta temática em índice de notícias. O vínculo precisa ser conferido. Nenhum resultado implica reprodução, autoria ou autenticidade confirmadas.", "",
             "Google News RSS e Bing News RSS pesquisam índices de notícias; não representam consulta integral a blogs ou redes sociais. URLs do Google News são links de agregador, com destino final não verificado.", "",
             f"Consultas de rede: {report['executed_queries']}; entradas percorridas: {report['queries_considered']}; matriz: {report['matrix_size']}; próximo cursor: {report['cursor_next']}.",
             f"Pendentes antes de completar a rotação atual: {report['remaining_in_cycle']}. Limite de tempo atingido: {report['time_budget_exhausted']}.", "",
             "## Consultas desta rodada", ""]
    for query in report["queries"]:
        lines.append(f"- {safe(query['id'])} — {safe(query['engine'])} / {safe(query['language'])} / {safe(query['scope'])}: **{safe(query['status'])}**; {query['results_count']} links; {safe(query['detail'])}")
    lines += ["", "## Links e pistas", ""]
    if not report["items"]:
        lines.append("Nenhum link com conexão suficiente foi registrado. Consulte os bloqueios e erros acima; isso não prova ausência de conteúdo.")
    for item in report["items"]:
        lines += [f"### {safe(item['title'])}", "", f"Link: <{html.escape(item['url'], quote=True)}>",
                  f"Nível: **{safe(item['verification'])}** | Idioma da consulta: {safe(item['language'])} | Escopo: {safe(item['scope'])}",
                  f"Primeira detecção: {item['first_seen']} | Última detecção: {item['last_seen']}",
                  "Trecho atribuído ao índice: " + safe(item["snippet"]), safe(item["note"]), ""]
        for provenance in item["provenance"]:
            lines.append(f"- Proveniência: {safe(provenance['query_id'])} / {safe(provenance['engine'])} ({safe(provenance['coverage'])}): <{html.escape(provenance['search_url'], quote=True)}>")
        lines.append("")
    lines += ["## Limites", "",
              "A matriz é uma seleção de idiomas e escopos, não todos os idiomas ou sites da internet. Cada execução percorre um lote e preserva o cursor para a seguinte. Desafios, recusas ou limites suspendem o provedor na rodada; duas respostas HTML consecutivas sem formato/relevância reconhecíveis também suspendem esse provedor como inconclusivo. Não há contorno de login, CAPTCHA ou áreas privadas. Links genéricos de busca e resultados sem conexão ao nome são descartados.", ""]
    if report["filtered_sensitive_count"]:
        lines += [f"Referências descartadas por indício concreto de mídia sexual envolvendo menores: {report['filtered_sensitive_count']}. Nenhuma URL desses descartes é armazenada no relatório.", ""]
    return "\n".join(lines)


def run_round(config_path: Path, state_path: Path, output_dir: Path, max_queries=48, max_seconds=1200, spacing=4) -> dict:
    if not isinstance(max_queries, int) or isinstance(max_queries, bool) or max_queries < 1:
        raise ValueError("max_queries deve ser inteiro positivo")
    if (not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool) or not math.isfinite(max_seconds) or max_seconds <= 0
            or not isinstance(spacing, (int, float)) or isinstance(spacing, bool) or not math.isfinite(spacing) or spacing < 0):
        raise ValueError("Orçamento de tempo deve ser positivo e espaçamento não negativo")
    config = load_config(config_path)
    state_path, output_dir = Path(state_path), Path(output_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(state_path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Outra rodada de descoberta usa o mesmo banco") from exc
        db = database(state_path)
        try:
            now = datetime.now(timezone.utc).isoformat()
            started = time.monotonic()
            deadline = started + max_seconds
            signature = hashlib.sha256(json.dumps(config["queries"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if setting(db, "matrix_signature") != signature:
                cursor = 0
                considered_total = 0
            else:
                cursor = int(setting(db, "cursor", "0")) % len(config["queries"])
                considered_total = int(setting(db, "considered_total", str(cursor)))
            cursor_before = cursor
            logs, blocked, last_request, unusable_streak = [], {}, {}, {}
            executed, new_count, sensitive, budget_exhausted = 0, 0, 0, False
            window = min(max_queries, len(config["queries"]))
            for _ in range(window):
                if time.monotonic() >= deadline:
                    budget_exhausted = True
                    break
                query = config["queries"][cursor]
                engine = query["engine"]
                entry = {**query, "status": "skipped", "detail": "", "results_count": 0, "new_count": 0}
                if engine in blocked:
                    entry["detail"] = "Provedor suspenso nesta rodada: " + blocked[engine]
                else:
                    host = urlsplit(query_url(query)).hostname
                    delay = max(0, last_request.get(host, -spacing) + spacing - time.monotonic())
                    if time.monotonic() + delay >= deadline:
                        budget_exhausted = True
                        break
                    if delay:
                        time.sleep(delay)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        budget_exhausted = True
                        break
                    last_request[host] = time.monotonic()
                    executed += 1
                    try:
                        data = fetch_query(query, timeout=min(20, remaining))
                        found, filtered = parse_candidates(data, query, config["aliases"], config["topic_terms"])
                        sensitive += filtered
                        entry["status"] = "ok"
                        unusable_streak[engine] = 0
                        entry["results_count"] = len(found)
                        with db:
                            entry["new_count"] = save_results(db, found, now)
                        new_count += entry["new_count"]
                    except ProviderBlocked as exc:
                        entry["status"], entry["detail"] = "blocked", str(exc)
                        blocked[engine] = str(exc)
                    except UnusableSearch as exc:
                        entry["status"], entry["detail"] = "error", f"{type(exc).__name__}: {exc}"
                        if engine not in RSS_ENGINES:
                            unusable_streak[engine] = unusable_streak.get(engine, 0) + 1
                            if unusable_streak[engine] >= 2:
                                blocked[engine] = "Duas respostas HTML consecutivas inconclusivas de formato/relevância; sem novas tentativas nesta rodada"
                    except Exception as exc:
                        entry["status"], entry["detail"] = "error", f"{type(exc).__name__}: {exc}"
                logs.append(entry)
                cursor = (cursor + 1) % len(config["queries"])
                considered_total += 1
                with db:
                    db.execute("INSERT INTO query_runs(collected_at,query_id,engine,language,scope,query,status,detail,results_count,new_count) VALUES (?,?,?,?,?,?,?,?,?,?)", (now, query["id"], engine, query["language"], query["scope"], query["query"], entry["status"], entry["detail"], entry["results_count"], entry["new_count"]))
                    save_setting(db, "matrix_signature", signature)
                    save_setting(db, "cursor", cursor)
                    save_setting(db, "considered_total", considered_total)
            ok = sum(entry["status"] == "ok" for entry in logs)
            failed = any(entry["status"] != "ok" for entry in logs)
            status = "error" if not ok else "partial" if failed or budget_exhausted else "ok"
            items = saved_results(db, now, config["aliases"])
            report = {"collected_at": now, "collected_at_local": datetime.fromisoformat(now).astimezone(ZoneInfo("America/Sao_Paulo")).isoformat(),
                      "status": status, "new_count": new_count, "total_count": len(items),
                      "indexed_count": sum(item["verification"] == "indexado" for item in items),
                      "candidate_count": sum(item["verification"] == "pista" for item in items),
                      "matrix_size": len(config["queries"]), "cursor_before": cursor_before, "cursor_next": cursor,
                      "remaining_in_cycle": len(config["queries"]) - cursor if cursor else 0 if considered_total else len(config["queries"]),
                      "rotations_completed": considered_total // len(config["queries"]), "considered_total": considered_total,
                      "queries_considered": len(logs), "executed_queries": executed,
                      "max_queries": max_queries, "max_seconds": max_seconds, "spacing_seconds": spacing,
                      "elapsed_seconds": round(time.monotonic() - started, 3), "time_budget_exhausted": budget_exhausted,
                      "blocked_providers": blocked, "filtered_sensitive_count": sensitive,
                      "queries": logs, "items": items}
            atomic_write(output_dir / "discovery-latest.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            rendered = render_markdown(report)
            atomic_write(output_dir / "discovery-latest.md", rendered)
            run_id = datetime.fromisoformat(now).strftime("%Y%m%dT%H%M%S.%fZ")
            atomic_write(output_dir / "discovery-history" / f"{run_id}.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            atomic_write(output_dir / "discovery-history" / f"{run_id}.md", rendered)
            return report
        finally:
            db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Executar um lote incremental")
    parser.add_argument("--config", type=Path, default=ROOT / "discovery_queries.json")
    parser.add_argument("--state", type=Path, default=ROOT / ".monitor" / "discovery.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".monitor" / "reports")
    parser.add_argument("--max-queries", type=int, default=48)
    parser.add_argument("--max-seconds", type=float, default=1200)
    parser.add_argument("--spacing", type=float, default=4)
    args = parser.parse_args(argv)
    try:
        report = run_round(args.config, args.state, args.output_dir, args.max_queries, args.max_seconds, args.spacing)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Falha de descoberta: {exc}", file=sys.stderr)
        return 1
    print(f"{report['collected_at_local']} descoberta={report['status']} consultas={report['executed_queries']} novos={report['new_count']} historico={report['total_count']} cursor={report['cursor_next']}/{report['matrix_size']}")
    for engine, reason in report["blocked_providers"].items():
        print(f"Provedor {engine}: {reason}", file=sys.stderr)
    return {"ok": 0, "partial": 2, "error": 1}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
