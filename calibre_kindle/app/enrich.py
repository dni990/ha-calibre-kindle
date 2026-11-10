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


def strip_series_from_title(title, series):
    if not series:
        return title
    pattern = r"\s+[–—-]\s+%s(?:\s*\(?(?:%s)?\s*\d+\)?)?\s*$" % (re.escape(series), SERIES_WORDS)
    new = re.sub(pattern, "", title, flags=re.I).strip()
    return new or title
