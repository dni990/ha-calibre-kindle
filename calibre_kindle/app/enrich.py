"""Metadaten nach dem Import ergänzen: EPUB-OPF richtig auslesen, Sprache erkennen,
Autor normalisieren und per ISBN/Titel online Reihe, Band, Beschreibung usw. holen."""
import html
import logging
import os
import re
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET

log = logging.getLogger("enrich")

LANG_MAP = {"ger": "deu", "de": "deu", "deu": "deu", "german": "deu", "deutsch": "deu",
            "en": "eng", "eng": "eng", "english": "eng", "it": "ita", "ita": "ita",
            "fr": "fra", "fre": "fra", "fra": "fra", "es": "spa", "spa": "spa",
            "nl": "nld", "dut": "nld", "nld": "nld", "pt": "por", "por": "por",
            "pl": "pol", "pol": "pol", "tr": "tur", "tur": "tur"}
STOPWORDS = {
    "deu": " und der die das nicht ist ich sie es zu mit sich auf ein eine dem den ".split(),
    "eng": " the and of to was is that he she it with his her you not ".split(),
    "ita": " il di che e la per non una sono della con mi ".split(),
    "fra": " le de et la les des que est pas une il dans ".split(),
    "spa": " el de que y la los las por una con para es ".split(),
}
SERIES_WORDS = r"(?:Band|Bd\.|Teil|Buch|Folge|Book|Vol\.?|Volume)"


def norm_lang(code):
    return LANG_MAP.get((code or "").strip().lower().split("-")[0])


def clean_isbn(value):
    digits = re.sub(r"[^0-9Xx]", "", value or "")
    if len(digits) == 13 and digits[:3] in ("978", "979"):
        return digits
    if len(digits) == 10:
        return digits.upper()
    return None


def flip_author(name):
    """'Caine, Rachel' -> 'Rachel Caine'."""
    parts = [p.strip() for p in name.split(",")]
    if len(parts) == 2 and all(parts):
        return "%s %s" % (parts[1], parts[0])
    return name.strip()


# ---------------------------------------------------------------- EPUB lesen

def read_epub(path):
    info = {"text": ""}
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return info
    with zf:
        try:
            container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
            opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
            opf = zf.read(opf_path).decode("utf-8", "replace")
        except (KeyError, AttributeError):
            opf = ""
        meta = opf[opf.find("<metadata"):opf.find("</metadata>")] if "<metadata" in opf else ""
        for key in ("publisher", "rights", "date"):
            m = re.search(r"<dc:%s[^>]*>([^<]+)<" % key, meta)
            info[key] = html.unescape(m.group(1)).strip() if m else None
        lang = re.search(r"<dc:language[^>]*>([^<]+)<", meta)
        info["language"] = norm_lang(lang.group(1)) if lang else None
        for ident in re.findall(r"<dc:identifier[^>]*>([^<]+)<", meta):
            isbn = clean_isbn(ident)
            if isbn:
                info["isbn"] = isbn
                break
        # etwas Text für die Spracherkennung
        texts = sorted((i for i in zf.infolist() if i.filename.lower().endswith((".xhtml", ".html", ".htm"))),
                       key=lambda i: -i.file_size)[:3]
        chunks = []
        for item in texts:
            raw = zf.read(item).decode("utf-8", "replace")
            chunks.append(html.unescape(re.sub(r"<[^>]+>", " ", raw)))
        info["text"] = " ".join(chunks)[:60000]
    return info


def detect_language(text):
    words = re.findall(r"[a-zäöüßàèéìòù]+", text.lower())
    if len(words) < 200:
        return None
    counts = {lang: 0 for lang in STOPWORDS}
    sw = {lang: set(ws) for lang, ws in STOPWORDS.items()}
    for w in words:
        for lang, s in sw.items():
            if w in s:
                counts[lang] += 1
    best = max(counts, key=counts.get)
    return best if counts[best] >= 0.08 * len(words) * 0.5 else None


# ---------------------------------------------------------------- Online

def fetch_online(title, authors, isbn, timeout=90):
    """calibre-Metadatenquellen abfragen. Gibt dict + Pfad zum Cover zurück."""
    cover = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
    cmd = ["fetch-ebook-metadata", "--opf", "--cover", cover, "--timeout", str(timeout)]
    if isbn:
        cmd += ["--isbn", isbn]
    if title:
        cmd += ["--title", title]
    if authors:
        cmd += ["--authors", " & ".join(authors)]
    log.info("$ %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60)
    except subprocess.TimeoutExpired:
        return {}, None
    out = proc.stdout
    if "<package" not in out:
        log.info("Keine Online-Metadaten gefunden")
        return {}, None
    data = parse_opf(out[out.index("<"):])
    cover_path = cover if os.path.exists(cover) and os.path.getsize(cover) > 1000 else None
    return data, cover_path


def parse_opf(xml_text):
    ns = {"dc": "http://purl.org/dc/elements/1.1/", "opf": "http://www.idpf.org/2007/opf"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    md = root.find("opf:metadata", ns)
    if md is None:
        return {}

    def text(tag):
        el = md.find("dc:" + tag, ns)
        return el.text.strip() if el is not None and el.text else None

    data = {
        "title": text("title"),
        "authors": [e.text.strip() for e in md.findall("dc:creator", ns) if e.text],
        "publisher": text("publisher"),
        "pubdate": text("date"),
        "comments": text("description"),
        "language": norm_lang(text("language")),
        "tags": [e.text.strip() for e in md.findall("dc:subject", ns) if e.text],
    }
    for el in md.findall("dc:identifier", ns):
        scheme = (el.get("{%s}scheme" % ns["opf"]) or "").lower()
        if scheme == "isbn" or clean_isbn(el.text or ""):
            isbn = clean_isbn(el.text or "")
            if isbn:
                data["isbn"] = isbn
                break
    for el in md.findall("opf:meta", ns):
        if el.get("name") == "calibre:series":
            data["series"] = el.get("content")
        elif el.get("name") == "calibre:series_index":
            data["series_index"] = el.get("content")
    return {k: v for k, v in data.items() if v}


# ---------------------------------------------------------------- Heuristiken

def series_from_text(title, comments):
    """Vorsichtige Erkennung, nur wenn eindeutig:
    - 'Reihe 2 - Titel' / 'Titel (Reihe, Band 3)'
    - 'Titel – Reihe', wenn die Beschreibung die Reihe mit 'Band N' erwähnt."""
    title = title or ""
    plain = re.sub(r"<[^>]+>", " ", comments or "")
    m = re.search(r"\((.+?),?\s+(?:%s)\s*(\d+(?:[.,]\d+)?)\)\s*$" % SERIES_WORDS, title)
    if m:
        return m.group(1).strip(), m.group(2).replace(",", ".")
    m = re.match(r"^(.+?)\s+(\d+(?:[.,]\d+)?)\s*[:–—-]\s+(.+)$", title)
    if m:
        return m.group(1).strip(), m.group(2).replace(",", ".")
    m = re.match(r"^(.+?)\s+[–—-]\s+(.+)$", title)
    if m and plain:
        candidate = m.group(2).strip()
        if candidate.lower() in plain.lower():
            n = re.search(r"(?:%s)\s+(\d+)" % SERIES_WORDS, plain)
            if n:
                return candidate, n.group(1)
    return None, None


def clean_text(value):
    """Entfernt Sortier-/Steuerzeichen aus Bibliothekskatalogen: '¬Die¬ Stimmen' -> 'Die Stimmen'."""
    if not value:
        return value
    import unicodedata
    value = unicodedata.normalize("NFC", value)
    value = value.replace("\u00ac", "").replace("\u0098", "").replace("\u009c", "")
    return re.sub(r"\s{2,}", " ", value).strip()


def clean_series(name):
    """DNB liefert z. B. 'Die Magische Bibliothek-Reihe' -> 'Die Magische Bibliothek'."""
    if not name:
        return name
    # nur abgetrennte Zusätze: 'X-Reihe', 'X Saga' (nicht 'Dämonensaga')
    cleaned = re.sub(r"(?:\s+|\s*-\s*)(?:Reihe|Serie|Saga|Trilogie|Series|Trilogy)\s*$", "", name.strip(), flags=re.I)
    return cleaned.strip(" -–") or name.strip()


ARTICLES = r"(?:die|der|das|the|la|le|il|el)\s+"


def series_key(name):
    """Vergleichsschlüssel ohne führenden Artikel/Zusatz: 'Die Magische Bibliothek-Reihe' -> 'magische bibliothek'."""
    name = clean_series(clean_text(name) or "").strip().lower()
    name = re.sub(r"[\s\-–—_:]+", " ", name)
    return re.sub(r"^" + ARTICLES, "", name).strip()


def split_title_suffix(title):
    """'Tinte und Knochen – Die Magische Bibliothek' -> ('Tinte und Knochen', 'Die Magische Bibliothek')."""
    m = re.match(r"^(.+?)\s+[–—-]\s+(.+)$", title or "")
    return (m.group(1).strip(), m.group(2).strip()) if m else (title, None)


def strip_series_from_title(title, series):
    """Entfernt ' – Reihe' am Titelende, auch wenn der Artikel fehlt/abweicht."""
    if not series or not title:
        return title
    main, suffix = split_title_suffix(title)
    if suffix:
        suffix_plain = re.sub(r"\s*\(?(?:%s)?\s*\d+\)?\s*$" % SERIES_WORDS, "", suffix)
        if series_key(suffix_plain) == series_key(series):
            return main
    return title


# ---------------------------------------------------------------- Cover

def image_info(path):
    """(breite, höhe, farbig) aus JPEG/PNG-Header, ohne PIL. farbig=None wenn unbekannt."""
    try:
        with open(path, "rb") as f:
            d = f.read(512 * 1024)
    except OSError:
        return 0, 0, None
    if d[:8] == b"\x89PNG\r\n\x1a\n" and len(d) > 26:
        w, h = int.from_bytes(d[16:20], "big"), int.from_bytes(d[20:24], "big")
        return w, h, d[25] not in (0, 4)
    if d[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(d):
            if d[i] != 0xFF:
                i += 1
                continue
            marker = d[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = int.from_bytes(d[i + 5:i + 7], "big"), int.from_bytes(d[i + 7:i + 9], "big")
                return w, h, d[i + 9] >= 3
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + int.from_bytes(d[i + 2:i + 4], "big")
    return 0, 0, None


def better_cover(current, candidate):
    """Online-Cover übernehmen, wenn das vorhandene fehlt, schwarzweiß oder deutlich kleiner ist."""
    if not candidate:
        return False
    cw, ch, ccolor = image_info(candidate)
    if not cw or ccolor is False:
        return False
    if not current or not os.path.exists(current):
        return True
    w, h, color = image_info(current)
    if color is False:
        return True
    return cw * ch > 1.5 * w * h and min(cw, ch) >= 500


# ---------------------------------------------------------------- Einheitliche Regeln
# Werden bei jedem Import und bei „Online ergänzen“ angewendet und in die EPUB geschrieben.
# RULES_VERSION erhöhen, wenn sich Regeln ändern -> beim nächsten Start werden alle Bücher neu angeglichen.
RULES_VERSION = 4

LEGAL_FORMS = r"(?:GmbH\s*&\s*Co\.?\s*KG|GmbH|mbH|KG|AG|e\.\s?K\.|Ltd\.?|Limited|Inc\.?|LLC|S\.?p\.?A\.?|S\.?r\.?l\.?)"
# Konzern-/Gruppennamen: hier steht nicht der eigentliche Verlag (Imprint)
GROUP_PATTERN = re.compile(r"Verlagsgruppe|Random House|Bonnier Media|Holtzbrinck|Ullstein Buchverlage|"
                           r"HarperCollins Publishers|Hachette Livre|Macmillan Publishers", re.I)
KEEP_IDENTIFIERS = ("isbn",)


def publisher_core(name):
    """Vergleichsform: ohne Rechtsform, 'Verlag', Vornamen-Präfix. 'Wilhelm Heyne Verlag GmbH' -> 'heyne'."""
    core = re.sub(r"\b" + LEGAL_FORMS + r"(?=\s|$|,)", "", name or "", flags=re.I)
    core = re.sub(r"\b(?:verlag|verlage|buchverlag|verlagshaus|publishing|publishers?|books|edition)\b", "", core, flags=re.I)
    return re.sub(r"[^a-zäöüß0-9 ]+", " ", core.lower()).split()


def clean_publisher(name):
    name = re.sub(r",?\s*\b" + LEGAL_FORMS + r"\s*$", "", (name or "").strip(), flags=re.I)
    name = re.sub(r",?\s*(?:München|Berlin|Hamburg|Köln|Frankfurt(?: am Main)?|Stuttgart)\s*$", "", name)
    return name.strip(" ,")


def imprint_from_rights(rights):
    """'Copyright © 2024 … by Wilhelm Heyne Verlag, München' -> 'Wilhelm Heyne Verlag'."""
    m = re.search(r"(?:by|bei|im|©\s*\d{4})\s+((?:[A-ZÄÖÜ][\w.&-]*\s+){0,3}Verlag(?:\s+GmbH)?)", rights or "")
    return clean_publisher(m.group(1)) if m else None


def choose_publisher(online, epub_pub, rights, current, known):
    """Imprint statt Konzern; vorhandene Schreibweise aus der Bibliothek wiederverwenden."""
    candidates = [online, imprint_from_rights(rights), epub_pub, current]
    pick = next((c for c in candidates if c and not GROUP_PATTERN.search(c)), None) or current or online or epub_pub
    if not pick:
        return None
    pick = clean_publisher(pick)
    core = publisher_core(pick)
    if core:
        for k in sorted(known, key=len):
            kc = publisher_core(k)
            if kc and not GROUP_PATTERN.search(k) and core[-len(kc):] == kc:
                return k
    return pick


def year_of(value):
    m = re.match(r"\s*(\d{4})", str(value or ""))
    if m and 1450 <= int(m.group(1)) <= 2100:
        return int(m.group(1))
    return None


def normalize_pubdate(online_date, epub_date, current):
    """Nur das Erscheinungsjahr (Online > EPUB > vorhanden), einheitlich als 1. Januar."""
    year = year_of(online_date) or year_of(epub_date) or year_of(current)
    return "%d-01-01T12:00:00+00:00" % year if year else None


# ---------------------------------------------------------------- DNB: alle Ausgaben eines Titels
# Reihen stehen bei der DNB oft nur bei einer Ausgabe (z. B. Druck), und dieselbe Reihe kann je
# Ausgabe anders heißen ("Die Dämonensaga" / "Demon Zyklus"). Namen, die beim selben Werk vorkommen,
# werden als gleichwertig gespeichert (series_aliases.json) und auf einen Namen vereinheitlicht.
ALIAS_FILE = "/data/series_aliases.json"
DNB_SRU = "https://services.dnb.de/sru/dnb"


def dnb_editions(title, author, timeout=20):
    """[(isbn, reihe, band)] aller DNB-Ausgaben mit diesem Titel und Autor-Nachnamen."""
    import urllib.parse
    import urllib.request
    if not title or not author:
        return []
    surname = author.split()[-1]
    query = 'tit="%s" and per=%s' % (title.replace('"', ""), surname)
    url = DNB_SRU + "?" + urllib.parse.urlencode({
        "version": "1.1", "operation": "searchRetrieve", "query": query,
        "recordSchema": "MARC21-xml", "maximumRecords": "30"})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            xml = resp.read().decode("utf-8", "replace")
    except Exception as err:  # noqa: BLE001
        log.info("DNB-Abfrage fehlgeschlagen: %s", err)
        return []
    # Nicht-Sortier-Markierungen der DNB (&#152;Das&#156;) entfernen, bevor HTML-Entities aufgelöst werden
    xml = xml.replace("&#152;", "").replace("&#156;", "")
    want = clean_text(title).lower()
    result = []
    for rec in xml.split("<record ")[1:]:
        m = re.search(r'tag="245".*?code="a">([^<]+)', rec, re.S)
        rec_title = clean_text(html.unescape(m.group(1))).strip(" /:") if m else ""
        isbn = re.search(r'tag="020".*?code="a">([^<]+)', rec, re.S)
        isbn = clean_isbn(isbn.group(1)) if isbn else None
        if rec_title.lower() != want:
            # z. B. 'Das Lied der Dunkelheit (Demon Zyklus 1)'
            pm = re.match(r"^(.+?)\s*\((.+?),?\s+(?:%s\s*)?(\d+(?:[.,]\d+)?)\)$" % SERIES_WORDS, rec_title)
            if pm and pm.group(1).lower() == want:
                result.append((isbn, clean_series(pm.group(2).strip()), pm.group(3).replace(",", ".")))
            continue
        for field in re.findall(r'tag="(?:490|830)"[^>]*>(.*?)</datafield>', rec, re.S):
            a = re.search(r'code="a">([^<]+)', field)
            v = re.search(r'code="v">([^<]+)', field)
            if not a:
                continue
            name, _, rest = html.unescape(a.group(1)).partition(";")
            name = clean_series(clean_text(name.strip(" []")))
            num = re.search(r"\d+(?:[.,]\d+)?", (v.group(1) if v else rest) or "")
            if name:
                result.append((isbn, name, num.group(0).replace(",", ".") if num else None))
    return result


def load_aliases():
    import json
    try:
        with open(ALIAS_FILE) as f:
            return [set(g) for g in json.load(f)]
    except (OSError, ValueError):
        return []


def remember_aliases(names):
    """Namen, die beim selben Werk vorkommen, zu einer Gruppe zusammenführen."""
    import json
    keys = {series_key(n): n for n in names if n}
    if len(keys) < 2:
        return
    groups = load_aliases()
    merged = set(keys.values())
    rest = []
    for g in groups:
        if {series_key(n) for n in g} & set(keys):
            merged |= g
        else:
            rest.append(g)
    rest.append(merged)
    try:
        with open(ALIAS_FILE, "w") as f:
            json.dump([sorted(g) for g in rest], f, ensure_ascii=False, indent=1)
    except OSError as err:
        log.warning("Reihen-Aliase nicht gespeichert: %s", err)


def alias_keys(name):
    key = series_key(name)
    for g in load_aliases():
        keys = {series_key(n) for n in g}
        if key in keys:
            return keys
    return {key}


def series_from_editions(editions):
    """Häufigste Reihe/Band über alle Ausgaben."""
    from collections import Counter
    counts = Counter((series_key(n), idx) for _, n, idx in editions if idx)
    if not counts:
        return None, None
    (key, idx), _ = counts.most_common(1)[0]
    name = next(n for _, n, i in editions if series_key(n) == key)
    return name, idx
