"""Парсер MediaWiki API (Fandom Warhammer 40k Wiki, русский раздел) в структурированный
JSON, дополнительный источник для RAG (Этап 1b, расширение roadmap).

Lexicanum блокирует программный доступ (Cloudflare bot-challenge на api.php),
поэтому источник — warhammer40k.fandom.com, чей API открыт для сторонних
инструментов. TextExtracts на этой вики не установлен, поэтому тянем сырую
wikitext-разметку (action=query&prop=revisions) и чистим её сами.

Берём русский раздел (/ru/), а не английский — эмбеддинги multilingual-e5 заметно
предпочитают совпадение языка запроса и документа: на русских запросах английские
чанки почти никогда не попадают в top-k, даже если релевантны по смыслу.

Контент Fandom-вики лицензирован по CC BY-SA — при использовании чанков в
ответах бота источник должен оставаться атрибутируемым, поэтому в каждый
JSON пишется исходный URL страницы.
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

API_URL = "https://warhammer40k.fandom.com/ru/api.php"
WIKI_BASE_URL = "https://warhammer40k.fandom.com/ru/wiki/"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "processed" / "wiki"

HEADERS = {"User-Agent": "warhammer-rag-bot/0.1 (personal local project, not for redistribution)"}
REQUEST_DELAY = 1.0  # секунд между запросами — не нагружать чужой API


def fetch_category_members(category: str, limit: int = 500) -> list[str]:
    """Возвращает названия страниц в категории Fandom-вики, например
    fetch_category_members('Primarchs')."""
    titles = []
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": f"Category:{category}",
        "cmlimit": min(limit, 500),
        "format": "json",
    }
    while True:
        resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        titles.extend(m["title"] for m in data.get("query", {}).get("categorymembers", []))
        cont = data.get("continue", {}).get("cmcontinue")
        if not cont or len(titles) >= limit:
            break
        params["cmcontinue"] = cont
        time.sleep(REQUEST_DELAY)
    return titles[:limit]


def fetch_all_article_titles() -> list[str]:
    """Все страницы основного пространства имён (namespace 0), без редиректов —
    примерно соответствует "articles" из siteinfo/statistics."""
    titles = []
    params = {
        "action": "query",
        "list": "allpages",
        "apnamespace": 0,
        "apfilterredir": "nonredirects",
        "aplimit": 500,
        "format": "json",
    }
    while True:
        resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        titles.extend(p["title"] for p in data.get("query", {}).get("allpages", []))
        cont = data.get("continue", {}).get("apcontinue")
        if not cont:
            break
        params["apcontinue"] = cont
        time.sleep(REQUEST_DELAY)
    return titles


def fetch_raw_wikitext(title: str) -> str | None:
    params = {
        "action": "query",
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "titles": title,
        "format": "json",
    }
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            last_exc = e
            time.sleep(2 * (attempt + 1))
    else:
        print(f"  [ERROR] сетевой сбой после 3 попыток: {last_exc}", file=sys.stderr)
        return None
    pages = resp.json().get("query", {}).get("pages", {})
    for page in pages.values():
        if "missing" in page:
            return None
        revisions = page.get("revisions")
        if revisions:
            return revisions[0]["slots"]["main"]["*"]
    return None


def clean_wikitext(text: str) -> str:
    """Грубая, но достаточная для RAG очистка wikitext -> читаемый текст."""
    # HTML-комментарии
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # ref-сноски целиком выкидываем (обычно источники/цитаты игровых книг, не текст статьи)
    text = re.sub(r"<ref[^>]*/>", "", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL)
    # инфобоксы и шаблоны {{...}} — грубо, без рекурсивного парсинга вложенности
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)  # второй проход на случай одного уровня вложенности
    # файлы/изображения [[File:...]] / [[Image:...]]
    text = re.sub(r"\[\[(File|Image):[^\]]*\]\]", "", text, flags=re.IGNORECASE)
    # внутренние ссылки [[текст]] и [[цель|текст]] -> просто текст
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", text)
    # внешние ссылки [http://... текст] -> текст
    text = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\]", "", text)
    # жирный/курсив
    text = text.replace("'''", "").replace("''", "")
    # прочая HTML-разметка
    text = re.sub(r"<[^>]+>", "", text)
    # таблицы {| ... |} выкидываем целиком — плохо режутся в plain text
    text = re.sub(r"\{\|.*?\|\}", "", text, flags=re.DOTALL)
    # лишние пробелы/пустые строки
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_into_sections(wikitext: str) -> list[dict]:
    """Режет статью по заголовкам == Section == (уровень 2), очищая каждый раздел."""
    parts = re.split(r"\n==\s*([^=\n]+?)\s*==\n", "\n" + wikitext)
    sections = []
    # parts[0] — текст до первого заголовка (вступление)
    intro = clean_wikitext(parts[0])
    if intro:
        sections.append({"chapter_title": None, "order": 0, "text": intro})
    order = len(sections)
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = clean_wikitext(parts[i + 1]) if i + 1 < len(parts) else ""
        if body:
            sections.append({"chapter_title": heading, "order": order, "text": body})
            order += 1
    return sections


def slugify(title: str) -> str:
    return re.sub(r"[^\w\-]+", "_", title, flags=re.UNICODE).strip("_")


def build_page_json(title: str) -> dict | None:
    wikitext = fetch_raw_wikitext(title)
    if wikitext is None:
        print(f"  [SKIP] страница не найдена: {title}", file=sys.stderr)
        return None
    sections = split_into_sections(wikitext)
    if not sections:
        return None
    return {
        "title": title,
        "source_type": "wiki",
        "url": WIKI_BASE_URL + title.replace(" ", "_"),
        "sections": sections,
    }


def fetch_and_save(titles: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok, skipped = 0, []
    for title in titles:
        print(f"-> {title}")
        try:
            data = build_page_json(title)
        except Exception as e:
            print(f"  [ERROR] {e}", file=sys.stderr)
            data = None
        time.sleep(REQUEST_DELAY)
        if data is None:
            skipped.append(title)
            continue
        out_path = OUT_DIR / f"{slugify(title)}.json"
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        n_chars = sum(len(s["text"]) for s in data["sections"])
        print(f"   sections={len(data['sections'])} chars={n_chars} -> {out_path.name}")
        ok += 1
    print(f"\nГотово: {ok} страниц сохранено, {len(skipped)} пропущено")
    if skipped:
        print("Пропущенные (не найдены на вики):", ", ".join(skipped))


if __name__ == "__main__":
    # без аргументов — небольшой смоук-тест на паре тестовых страниц;
    # с аргументами --category NAME или списком названий страниц через запятую
    args = sys.argv[1:]
    if not args:
        fetch_and_save(["Horus", "Council of Nikaea"])
    elif args[0] == "--category":
        titles = fetch_category_members(args[1])
        print(f"В категории '{args[1]}' найдено {len(titles)} страниц")
        fetch_and_save(titles)
    elif args[0] == "--all":
        titles = fetch_all_article_titles()
        print(f"Всего статей на вики: {len(titles)}")
        fetch_and_save(titles)
    else:
        fetch_and_save([t.strip() for t in " ".join(args).split(",") if t.strip()])
