"""Конвертация книг (.fb2 и .epub) в структурированный JSON (Этап 1 roadmap).

Каждая книга -> data/processed/books/<slug>.json со схемой:
{
  "title": str, "author": str, "sequence_number": int|None,
  "source_file": str,
  "sections": [{"chapter_title": str|None, "order": int, "text": str}, ...]
}

Кодировка fb2 берётся из XML-декларации файла (lxml делает это сам при parse() на bytes) —
в этой папке встречаются и utf-8, и windows-1251.

Внутренние метаданные (<title-info> у fb2, OPF у epub) у многих файлов битые или
отсутствуют (дают "Unknown", "tmp0", логин конвертера вместо автора и т.п.), поэтому
единственный источник истины для title/author/номера — BOOK_META ниже, составленный
по имени файла (файлы уже названы по схеме NN_Название_Автор.расширение).
"""
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub, ITEM_DOCUMENT
from lxml import etree

BOOK_DIRS = [
    Path(__file__).resolve().parent.parent / "book" / "horus_heresy",
    Path(__file__).resolve().parent.parent / "book" / "primarchs",
    Path(__file__).resolve().parent.parent / "book" / "siege_of_terra",
]
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "processed" / "books"

FB2_NS = "http://www.gribuser.ru/xml/fictionbook/2.0"

# Источник истины по title/author/номеру — файлы называются NN_Название_Автор.ext,
# но парсить это регэкспом ненадёжно (и название, и автор могут содержать несколько
# слов), поэтому номер сериала — это единственное, что действительно надо для чанкинга
# по циклу; title/author просто дублируют то, что уже видно в имени файла.
#
# cycle различает основной 54-томный цикл "Ересь Хоруса" (номера 1-54, сквозные)
# от серии "Primarchs" (номера 1-17 — это ДРУГАЯ нумерация, публикационный порядок
# внутри своей серии, не пересекается по смыслу с номерами основного цикла).
BOOK_META = {
    "01_Возвышение_Хоруса_Дэн_Абнетт.fb2": ("Возвышение Хоруса", "Дэн Абнетт", 1, "horus_heresy"),
    "02_Лживые_боги_Грэм_Макнилл.fb2": ("Лживые боги", "Грэм Макнилл", 2, "horus_heresy"),
    "03_Галактика_в_огне_Бен_Каунтер.fb2": ("Галактика в огне", "Бен Каунтер", 3, "horus_heresy"),
    "04_Полёт_Эйзенштейна_Джеймс_Сваллоу.fb2": ("Полёт Эйзенштейна", "Джеймс Сваллоу", 4, "horus_heresy"),
    "05_Фулгрим_Грэм_Макнилл.fb2": ("Фулгрим", "Грэм Макнилл", 5, "horus_heresy"),
    "06_Сошествие_ангелов_Митчел_Скэнлон.fb2": ("Сошествие ангелов", "Митчел Скэнлон", 6, "horus_heresy"),
    "07_Легион_Дэн_Абнетт.fb2": ("Легион", "Дэн Абнетт", 7, "horus_heresy"),
    "08_Битва_за_бездну_Бен_Каунтер.fb2": ("Битва за бездну", "Бен Каунтер", 8, "horus_heresy"),
    "09_Механикум_Грэм_Макнилл.fb2": ("Механикум", "Грэм Макнилл", 9, "horus_heresy"),
    "10_Легенды_ереси_сборник.fb2": ("Легенды ереси", "сборник", 10, "horus_heresy"),
    "11_Падшие_ангелы_Майк_Ли.fb2": ("Падшие ангелы", "Майк Ли", 11, "horus_heresy"),
    "12_Тысяча_сынов_Грэм_Макнилл.fb2": ("Тысяча сынов", "Грэм Макнилл", 12, "horus_heresy"),
    "13_Немезида_Джеймс_Сваллоу.fb2": ("Немезида", "Джеймс Сваллоу", 13, "horus_heresy"),
    "14_Первый_еретик_Аарон_Дембски-Боуден.fb2": ("Первый еретик", "Аарон Дембски-Боуден", 14, "horus_heresy"),
    "15_Сожжение_Просперо_Дэн_Абнетт.fb2": ("Сожжение Просперо", "Дэн Абнетт", 15, "horus_heresy"),
    "16_Эпоха_тьмы_сборник.fb2": ("Эпоха тьмы", "сборник", 16, "horus_heresy"),
    "17_Отверженные_мертвецы_Грэм_Макнилл.fb2": ("Отверженные мертвецы", "Грэм Макнилл", 17, "horus_heresy"),
    "18_Потерянное_освобождение_Гэв_Торп.fb2": ("Потерянное освобождение", "Гэв Торп", 18, "horus_heresy"),
    "19_Не_ведая_страха_Дэн_Абнетт.fb2": ("Не ведая страха", "Дэн Абнетт", 19, "horus_heresy"),
    "20_Примархи_сборник.fb2": ("Примархи", "сборник", 20, "horus_heresy"),
    "21_Где_Ангел_не_решится_сделать_шаг_Джеймс_Сваллоу.fb2": ("Где Ангел не решится сделать шаг", "Джеймс Сваллоу", 21, "horus_heresy"),
    "22_Тени_Предательства_сборник.fb2": ("Тени Предательства", "сборник", 22, "horus_heresy"),
    "23_Ангел_Экстерминатус_Грэм_Макнилл.fb2": ("Ангел Экстерминатус", "Грэм Макнилл", 23, "horus_heresy"),
    "24_Предатель_Аарон_Дембски-Боуден.fb2": ("Предатель", "Аарон Дембски-Боуден", 24, "horus_heresy"),
    "25_Отметка_Калта_сборник.fb2": ("Отметка Калта", "сборник", 25, "horus_heresy"),
    "26_Вулкан_жив_Ник_Кайм.fb2": ("Вулкан жив", "Ник Кайм", 26, "horus_heresy"),
    "27_Забытая_империя_Дэн_Абнетт.fb2": ("Забытая империя", "Дэн Абнетт", 27, "horus_heresy"),
    "28_Шрамы_Крис_Райт.fb2": ("Шрамы", "Крис Райт", 28, "horus_heresy"),
    "29_Мстительный_дух_Грэм_Макнилл.fb2": ("Мстительный дух", "Грэм Макнилл", 29, "horus_heresy"),
    "30_Проклятие_Пифоса_Дэвид_Эннандейл.fb2": ("Проклятие Пифоса", "Дэвид Эннандейл", 30, "horus_heresy"),
    "31_Заветы_предательства_сборник.epub": ("Заветы предательства", "сборник", 31, "horus_heresy"),
    "32_Смертельный_огонь_Ник_Кайм.fb2": ("Смертельный огонь", "Ник Кайм", 32, "horus_heresy"),
    "33_Нет_войне_конца_сборник.fb2": ("Нет войне конца", "сборник", 33, "horus_heresy"),
    "34_Фарос_Гай_Хэйли.fb2": ("Фарос", "Гай Хэйли", 34, "horus_heresy"),
    "35_Око_Терры_сборник.fb2": ("Око Терры", "сборник", 35, "horus_heresy"),
    "36_Путь_Небес_Крис_Райт.fb2": ("Путь Небес", "Крис Райт", 36, "horus_heresy"),
    "37_Безмолвная_война_сборник.fb2": ("Безмолвная война", "сборник", 37, "horus_heresy"),
    "38_Ангелы_Калибана_Гэв_Торп.fb2": ("Ангелы Калибана", "Гэв Торп", 38, "horus_heresy"),
    "39_Преторианец_Дорна_Джон_Френч.fb2": ("Преторианец Дорна", "Джон Френч", 39, "horus_heresy"),
    "40_Коракс_Гэв_Торп.fb2": ("Коракс", "Гэв Торп", 40, "horus_heresy"),
    "41_Повелитель_Человечества_Аарон_Дембски-Боуден.fb2": ("Повелитель Человечества", "Аарон Дембски-Боуден", 41, "horus_heresy"),
    "42_Гарро_Джеймс_Сваллоу.epub": ("Гарро", "Джеймс Сваллоу", 42, "horus_heresy"),
    "43_Разбитые_легионы_сборник.fb2": ("Разбитые легионы", "сборник", 43, "horus_heresy"),
    "44_Алый_король_Грэм_Макнилл.fb2": ("Алый король", "Грэм Макнилл", 44, "horus_heresy"),
    "45_Талларн_Джон_Френч.fb2": ("Талларн", "Джон Френч", 45, "horus_heresy"),
    "46_Гибельный_шторм_Дэвид_Эннандейл.fb2": ("Гибельный шторм", "Дэвид Эннандейл", 46, "horus_heresy"),
    "47_Старая_Земля_Ник_Кайм.fb2": ("Старая Земля", "Ник Кайм", 47, "horus_heresy"),
    "48_Бремя_верности_сборник.fb2": ("Бремя верности", "сборник", 48, "horus_heresy"),
    "49_Волчья_погибель_Гай_Хэйли.epub": ("Волчья погибель", "Гай Хэйли", 49, "horus_heresy"),
    "50_Рождённые_в_пламени_Ник_Кайм.fb2": ("Рождённые в пламени", "Ник Кайм", 50, "horus_heresy"),
    "51_Рабы_тьмы_Джон_Френч.fb2": ("Рабы тьмы", "Джон Френч", 51, "horus_heresy"),
    "52_Вестники_Осады_сборник.fb2": ("Вестники Осады", "сборник", 52, "horus_heresy"),
    "53_Бойня_титанов_Гай_Хэйли.epub": ("Бойня титанов", "Гай Хэйли", 53, "horus_heresy"),
    "54_Погребённый_кинжал_Джеймс_Сваллоу.epub": ("Погребённый кинжал", "Джеймс Сваллоу", 54, "horus_heresy"),
    # серия "Primarchs" — предыстории примархов, конец Крестового похода
    # (часть книг захватывает и самое начало Ереси — см. заметку в roadmap)
    "01_Робаут_Жиллиман_Владыка_Ультрамара_Дэвид_Аннандейл.fb2": ("Робаут Жиллиман: Владыка Ультрамара", "Дэвид Аннандейл", 1, "primarchs"),
    "02_Леман_Русс_Великий_Волк_Крис_Райт.fb2": ("Леман Русс: Великий Волк", "Крис Райт", 2, "primarchs"),
    "03_Магнус_Красный_Повелитель_Просперо_Грэм_Макнилл.fb2": ("Магнус Красный: Повелитель Просперо", "Грэм Макнилл", 3, "primarchs"),
    "04_Пертурабо_Молот_Олимпии_Гай_Хэйли.fb2": ("Пертурабо: Молот Олимпии", "Гай Хэйли", 4, "primarchs"),
    "05_Лоргар_Носитель_Слова_Гэв_Торп.epub": ("Лоргар: Носитель Слова", "Гэв Торп", 5, "primarchs"),
    "06_Фулгрим_Палатинский_Феникс_Джош_Рейнольдс.fb2": ("Фулгрим: Палатинский Феникс", "Джош Рейнольдс", 6, "primarchs"),
    "07_Феррус_Манус_Горгон_Медузы_Дэвид_Гаймер.fb2": ("Феррус Манус: Горгон Медузы", "Дэвид Гаймер", 7, "primarchs"),
    "08_Джагатай_Хан_Боевой_Ястреб_Чогориса_Крис_Райт.epub": ("Джагатай Хан: Боевой Ястреб Чогориса", "Крис Райт", 8, "primarchs"),
    "09_Вулкан_Владыка_Змиев_Дэвид_Аннандейл.epub": ("Вулкан: Владыка Змиев", "Дэвид Аннандейл", 9, "primarchs"),
    "10_Коракс_Повелитель_Теней_Гай_Хэйли.fb2": ("Коракс: Повелитель Теней", "Гай Хэйли", 10, "primarchs"),
    "11_Ангрон_Раб_Нуцерии_Йен_Сен_Мартин.epub": ("Ангрон: Раб Нуцерии", "Йен Сен Мартин", 11, "primarchs"),
    "12_Конрад_Курц_Ночной_Призрак_Гай_Хэйли.epub": ("Конрад Курц: Ночной Призрак", "Гай Хэйли", 12, "primarchs"),
    "13_Лев_ЭльДжонсон_Повелитель_Первого_Дэвид_Гаймер.epub": ("Лев Эль'Джонсон: Повелитель Первого", "Дэвид Гаймер", 13, "primarchs"),
    "14_Альфарий_Голова_Гидры_Майк_Брукс.epub": ("Альфарий: Голова Гидры", "Майк Брукс", 14, "primarchs"),
    "15_Мортарион_Бледный_Король_Дэвид_Аннандейл.epub": ("Мортарион: Бледный Король", "Дэвид Аннандейл", 15, "primarchs"),
    "16_Рогал_Дорн_Крестоносец_Императора_Гэв_Торп.epub": ("Рогал Дорн: Крестоносец Императора", "Гэв Торп", 16, "primarchs"),
    "17_Сангвиний_Великий_Ангел_Крис_Райт.epub": ("Сангвиний: Великий Ангел", "Крис Райт", 17, "primarchs"),
    # цикл "Осада Терры" — прямое продолжение и завершение сюжета основного
    # 54-томника; своя нумерация 1-9 (финальная книга издана в 3 томах)
    "01_Солнечная_война_Джон_Френч.epub": ("Солнечная война", "Джон Френч", 1, "siege_of_terra"),
    "02_Заблудшие_и_проклятые_Гай_Хейли.epub": ("Заблудшие и проклятые", "Гай Хейли", 2, "siege_of_terra"),
    "03_Первая_стена_Гэв_Торп.epub": ("Первая стена", "Гэв Торп", 3, "siege_of_terra"),
    "04_Под_знаком_Сатурна_Дэн_Абнетт.epub": ("Под знаком Сатурна", "Дэн Абнетт", 4, "siege_of_terra"),
    "05_Мортис_Джон_Френч.epub": ("Мортис", "Джон Френч", 5, "siege_of_terra"),
    "06_Боевой_Ястреб_Крис_Райт.epub": ("Боевой Ястреб", "Крис Райт", 6, "siege_of_terra"),
    "07_Конец_и_смерть_Том_1_Дэн_Абнетт.epub": ("Конец и смерть. Том 1", "Дэн Абнетт", 7, "siege_of_terra"),
    "08_Конец_и_смерть_Том_2_Дэн_Абнетт.epub": ("Конец и смерть. Том 2", "Дэн Абнетт", 8, "siege_of_terra"),
    "09_Конец_и_смерть_Том_3_Дэн_Абнетт.epub": ("Конец и смерть. Том 3", "Дэн Абнетт", 9, "siege_of_terra"),
}


def tag(el: etree._Element) -> str:
    """Локальное имя тега без namespace-префикса."""
    return etree.QName(el).localname


def local_findall(el: etree._Element, name: str):
    return el.findall(f"{{{FB2_NS}}}{name}")


def local_find(el: etree._Element, name: str):
    return el.find(f"{{{FB2_NS}}}{name}")


def text_of(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def slugify(name: str) -> str:
    name = re.sub(r"\.(fb2|epub)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE)
    return re.sub(r"_+", "_", name).strip("_")


def clean_text(text: str) -> str:
    # склеиваем мягкие переносы/множественные пробелы, сохраняя абзацы
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------- FB2 ----------

def extract_paragraphs_fb2(container: etree._Element) -> str:
    """Собирает текст всех <p>/<v> внутри секции, включая вложенные под-секции,
    но не заходя в под-секции повторно (те обрабатываются отдельно рекурсией)."""
    parts = []
    for child in container:
        t = tag(child)
        if t == "section":
            continue  # обработается отдельным рекурсивным вызовом
        if t in ("p", "subtitle", "v", "cite", "epigraph", "text-author"):
            txt = text_of(child)
            if txt:
                parts.append(txt)
        elif t in ("empty-line",):
            continue
    return clean_text("\n\n".join(parts))


def walk_sections_fb2(section: etree._Element, order_counter: list, out: list):
    title_el = local_find(section, "title")
    chapter_title = text_of(title_el) or None

    body_text = extract_paragraphs_fb2(section)
    if body_text:
        out.append({
            "chapter_title": chapter_title,
            "order": order_counter[0],
            "text": body_text,
        })
        order_counter[0] += 1

    for sub in local_findall(section, "section"):
        walk_sections_fb2(sub, order_counter, out)


def parse_fb2(path: Path) -> list | None:
    try:
        parser = etree.XMLParser(recover=True, huge_tree=True)
        tree = etree.parse(str(path), parser=parser)
    except Exception as e:
        print(f"  [ERROR] parse failed: {e}", file=sys.stderr)
        return None

    root = tree.getroot()

    # основное тело книги — первый <body> без name="notes"/"comments"
    bodies = local_findall(root, "body")
    main_body = None
    for b in bodies:
        if b.get("name") not in ("notes", "comments"):
            main_body = b
            break
    if main_body is None and bodies:
        main_body = bodies[0]

    sections_out = []
    order_counter = [0]
    if main_body is not None:
        for sec in local_findall(main_body, "section"):
            walk_sections_fb2(sec, order_counter, sections_out)
        # некоторые fb2 кладут параграфы прямо в <body> без <section>
        loose_text = extract_paragraphs_fb2(main_body)
        if loose_text and not sections_out:
            sections_out.append({"chapter_title": None, "order": 0, "text": loose_text})

    return sections_out


# ---------- EPUB ----------

def parse_epub(path: Path) -> list | None:
    try:
        book = epub.read_epub(str(path), options={"ignore_ncx": True})
    except Exception as e:
        print(f"  [ERROR] parse failed: {e}", file=sys.stderr)
        return None

    sections_out = []
    order = 0
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for tag_name in ("script", "style"):
            for el in soup.find_all(tag_name):
                el.decompose()

        heading = soup.find(["h1", "h2", "h3"])
        chapter_title = heading.get_text(strip=True) if heading else None

        p_tags = soup.find_all("p")
        if p_tags:
            paragraphs = [p.get_text(" ", strip=True) for p in p_tags]
        else:
            # некоторые epub используют "листовые" <div> вместо <p> для абзацев —
            # берём только div без вложенных div/ul/li, чтобы не задвоить текст
            # родительских контейнеров
            paragraphs = [
                d.get_text(" ", strip=True)
                for d in soup.find_all("div")
                if not d.find(["div", "ul", "li"])
            ]
        paragraphs = [p for p in paragraphs if p]
        text = clean_text("\n\n".join(paragraphs))

        if text:
            sections_out.append({
                "chapter_title": chapter_title,
                "order": order,
                "text": text,
            })
            order += 1

    return sections_out


# ---------- общее ----------

def build_book_json(path: Path) -> dict | None:
    if path.name not in BOOK_META:
        print(f"  [SKIP] нет в BOOK_META: {path.name}", file=sys.stderr)
        return None
    title, author, seq_number, cycle = BOOK_META[path.name]

    ext = path.suffix.lower()
    if ext == ".fb2":
        sections = parse_fb2(path)
    elif ext == ".epub":
        sections = parse_epub(path)
    else:
        print(f"  [SKIP] неизвестный формат: {path.name}", file=sys.stderr)
        return None

    if not sections:
        return None

    return {
        "title": title,
        "author": author,
        "sequence_number": seq_number,
        "cycle": cycle,
        "source_file": path.name,
        "sections": sections,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        p
        for d in BOOK_DIRS if d.exists()
        for p in d.iterdir() if p.suffix.lower() in (".fb2", ".epub")
    )
    print(f"Найдено {len(files)} файлов (.fb2/.epub)")

    ok, failed = 0, []
    for path in files:
        print(f"-> {path.name}")
        data = build_book_json(path)
        if data is None:
            failed.append(path.name)
            continue
        out_path = OUT_DIR / f"{slugify(path.name)}.json"
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        n_chars = sum(len(s["text"]) for s in data["sections"])
        print(f"   title={data['title']!r} seq={data['sequence_number']} "
              f"sections={len(data['sections'])} chars={n_chars}")
        ok += 1

    print(f"\nГотово: {ok} успешно, {len(failed)} с проблемами")
    if failed:
        print("Проблемные файлы (0 секций, ошибка парсинга или нет в BOOK_META):")
        for f in failed:
            print(f"  - {f}")


if __name__ == "__main__":
    main()
