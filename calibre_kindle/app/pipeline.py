"""Verarbeitung: Datei -> Calibre-Bibliothek (DRM via Plugins entfernt) -> Kindle."""
import json
import logging
import os
import re
import smtplib
import ssl
import subprocess
import threading
import zipfile
from email.message import EmailMessage

log = logging.getLogger("pipeline")

CONFIG_DIR = os.environ.get("CALIBRE_CONFIG_DIRECTORY", "/data/calibre-config")
PLUGIN_DIR = os.path.join(CONFIG_DIR, "plugins")
DEDRM_PREFS = os.path.join(PLUGIN_DIR, "dedrm.json")
BOOK_EXTS = {".acsm", ".epub", ".pdf", ".azw3", ".mobi", ".kfx", ".txt", ".docx"}
KINDLE_FORMAT_ORDER = ["EPUB", "PDF", "DOCX", "TXT"]
MAX_MAIL_BYTES = 50 * 1024 * 1024


class PipelineError(Exception):
    pass


def run(cmd, timeout=600, env=None):
    log.info("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})
    output = (proc.stdout or "") + (proc.stderr or "")
    for line in output.splitlines():
        log.debug("  %s", line)
    return proc.returncode, output


# ---------------------------------------------------------------- Plugins

PLUGIN_SOURCES = ("/opt/plugins", "/share/calibre/plugins")
INSTALLED_NAMES = {"DeDRM_plugin.zip": "DeDRM.zip"}


def ensure_plugins():
    """Calibre-Plugins installieren: DeDRM, DeACSM, DNB_DE (im Image) und Zusätze aus /share/calibre/plugins."""
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    seen = set()
    for folder in PLUGIN_SOURCES:
        if not os.path.isdir(folder):
            continue
        for zip_name in sorted(os.listdir(folder)):
            if not zip_name.endswith(".zip") or zip_name in seen:
                continue
            seen.add(zip_name)
            src = os.path.join(folder, zip_name)
            dst = os.path.join(PLUGIN_DIR, INSTALLED_NAMES.get(zip_name, zip_name))
            if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
                continue
            rc, out = run(["calibre-customize", "-a", src])
            if rc != 0:
                log.error("Plugin %s konnte nicht installiert werden:\n%s", zip_name, out)
            else:
                log.info("Plugin %s installiert", zip_name)


def adobe(action, version=""):
    rc, out = run(["calibre-debug", "-e", "/app/adobe_auth.py"], timeout=180,
                  env={"ADOBE_ACTION": action, "ADOBE_VERSION": version})
    data = None
    for line in reversed(out.splitlines()):
        if line.startswith("RESULT:"):
            data = json.loads(line[len("RESULT:"):])
            break
    if data is None:
        data = {"ok": False, "error": "Keine Antwort vom Adobe-Skript"}
    if data.get("key"):
        add_adept_key("DeACSM_" + (data.get("uuid") or "account").replace(":", "_"), data.pop("key"))
    if not data.get("ok"):
        # Ausgabe des Skripts (HTTP-Fehler von Adobe usw.) für die Fehlersuche
        log_lines = [l for l in out.splitlines() if l.strip() and not l.startswith("RESULT:")]
        data["log"] = "\n".join(log_lines[-30:])
        log.error("Adobe-%s fehlgeschlagen: %s\n%s\n%s", action, data.get("error"),
                  data["log"], data.get("detail", ""))
    return data


def load_dedrm_prefs():
    try:
        with open(DEDRM_PREFS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def add_adept_key(name, key_hex):
    """Adobe-Schlüssel direkt in DeDRMs Einstellungen eintragen."""
    prefs = load_dedrm_prefs()
    keys = prefs.setdefault("adeptkeys", {})
    if key_hex in keys.values():
        return False
    keys[name] = key_hex
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    with open(DEDRM_PREFS, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)
    log.info("Adobe-Schlüssel '%s' für DeDRM hinterlegt", name)
    return True


def adept_key_names():
    return list(load_dedrm_prefs().get("adeptkeys", {}).keys())


def detect_ext(path):
    """Dateityp anhand der ersten Bytes bestimmen (None = unbekannt)."""
    with open(path, "rb") as f:
        head = f.read(2048)
    if b"<fulfillmentToken" in head:
        return ".acsm"
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK") and b"mimetypeapplication/epub+zip" in head:
        return ".epub"
    return None


# ---------------------------------------------------------------- Bibliothek

_db_lock = threading.RLock()


def calibredb(library, *args, timeout=600):
    # calibredb darf nicht parallel auf derselben Bibliothek laufen
    with _db_lock:
        return run(["calibredb", "--library-path", library, *args], timeout=timeout)


LIST_FIELDS = ("id,title,authors,series,series_index,languages,pubdate,publisher,tags,"
               "formats,timestamp,identifiers,cover,rating,comments")
_cache = {"version": 0, "books": None}


def library_version():
    return _cache["version"]


def invalidate():
    _cache["version"] += 1
    _cache["books"] = None


def list_books(library):
    """Alle Bücher (gecacht, bis sich die Bibliothek ändert)."""
    if _cache["books"] is not None:
        return _cache["books"]
    if not os.path.exists(os.path.join(library, "metadata.db")):
        return []
    rc, out = calibredb(library, "list", "--for-machine", "--fields", LIST_FIELDS,
                        "--sort-by", "timestamp")
    if rc != 0:
        log.warning("calibredb list fehlgeschlagen: %s", out[-500:])
        return []
    try:
        books = json.loads(out[out.index("["):])
    except ValueError:
        return []
    _cache["books"] = books
    return books


def cached_book(library, book_id):
    """Schnell für die Detailansicht: aus dem Cache statt eigenem calibredb-Aufruf."""
    for b in list_books(library):
        if b.get("id") == book_id:
            return b
    raise PipelineError("Buch %d nicht gefunden" % book_id)


def book_details(library, book_id):
    rc, out = calibredb(library, "list", "--for-machine", "--fields", LIST_FIELDS,
                        "--search", "id:%d" % book_id)
    try:
        rows = json.loads(out[out.index("["):])
    except ValueError:
        rows = []
    if not rows:
        raise PipelineError("Buch %d nicht gefunden" % book_id)
    return rows[0]


EDITABLE = {"title", "authors", "series", "series_index", "languages", "publisher",
            "pubdate", "tags", "comments", "isbn"}


def identifiers_dict(value):
    if isinstance(value, dict):
        return dict(value)
    result = {}
    for part in str(value or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            result[k.strip()] = v.strip()
    return result


def set_metadata(library, book_id, fields, internal=False):
    fields = dict(fields)
    if "isbn" in fields:
        current = identifiers_dict(book_details(library, book_id).get("identifiers"))
        isbn = re.sub(r"[^0-9Xx]", "", str(fields.pop("isbn") or ""))
        if isbn:
            current["isbn"] = isbn
        else:
            current.pop("isbn", None)
        fields["identifiers"] = ",".join("%s:%s" % kv for kv in current.items())
    args = []
    for key, value in fields.items():
        if key not in EDITABLE and not (internal and key in ("identifiers", "cover")) and key != "identifiers":
            continue
        if isinstance(value, (list, tuple)):
            value = (" & " if key == "authors" else ", ").join(value)
        value = "" if value is None else str(value).strip()
        if key == "series_index" and not value:
            value = "1"
        args += ["-f", "%s:%s" % (key, value)]
    if not args:
        return
    rc, out = calibredb(library, "set_metadata", str(book_id), *args)
    invalidate()
    if rc != 0:
        raise PipelineError("Metadaten konnten nicht gespeichert werden:\n" + tail(out))


def _authors_list(value):
    if isinstance(value, str):
        return [a.strip() for a in value.split(" & ") if a.strip()]
    return list(value or [])


def canonical_series(library, book_id, series):
    """Einheitlicher Reihenname: gleichwertige Namen (Aliase) -> der in der Bibliothek häufigste."""
    import enrich
    from collections import Counter
    keys = enrich.alias_keys(series)
    used = Counter(o["series"] for o in list_books(library)
                   if o.get("id") != book_id and o.get("series") and enrich.series_key(o["series"]) in keys)
    return used.most_common(1)[0][0] if used else series


REVIEW_TAG = "Bitte prüfen"


def _find_isbn(enrich, candidates, search_title, authors):
    """ISBN + DNB-Datensatz bestimmen. Reihenfolge: gespeicherte/Datei-ISBN, die die DNB kennt ->
    einmalige Titelsuche, deren Treffer per Titelvergleich bestätigt sein muss."""
    for cand in candidates:
        record = enrich.dnb_record(cand)
        if record and (not search_title or not record.get("title")
                       or enrich.same_title(record["title"], search_title)):
            return cand, record
        if record:
            log.info("ISBN %s gehört laut DNB zu „%s“, nicht zu „%s“", cand, record["title"], search_title)
    # Datei-ISBN, die die DNB nicht kennt (z. B. ausländische Ausgabe): Online-Titel muss passen
    for cand in candidates:
        if enrich.dnb_record(cand) is None:
            data, cover = enrich.fetch_online(search_title, authors, cand)
            if cover:
                os.remove(cover)
            if data and enrich.same_title(data.get("title"), search_title):
                return cand, None
    # keine brauchbare ISBN: einmal nach Titel + Autor suchen
    if search_title:
        data, cover = enrich.fetch_online(search_title, authors, None)
        if cover:
            os.remove(cover)
        isbn = data.get("isbn")
        if isbn and enrich.same_title(data.get("title"), search_title):
            record = enrich.dnb_record(isbn)
            if record is None or enrich.same_title(record.get("title"), search_title):
                return isbn, record
    return None, None


def enrich_book(library, book_id, auto=False):
    """Metadaten nach dem Schema: ISBN -> DNB-Datensatz (Hauptquelle) -> Lücken per ISBN online
    -> sonst Markierung 'Bitte prüfen'. Danach einheitliche Schreibweisen und Einbetten in die EPUB."""
    import enrich
    b = book_details(library, book_id)
    changes = {}
    formats = b.get("formats") or []
    epub = next((f for f in formats if f.lower().endswith(".epub")), None)
    info = enrich.read_epub(epub) if epub else {"text": ""}

    cur_title = b.get("title") or ""
    cur_authors = _authors_list(b.get("authors"))
    file_authors = [enrich.flip_author(a) for a in cur_authors]
    hint_title, hint_series, hint_index = enrich.series_title_hint(enrich.clean_text(cur_title))
    search_title, title_suffix = enrich.split_title_suffix(hint_title)

    # 1. ISBN
    idents = identifiers_dict(b.get("identifiers"))
    candidates = []
    for c in (idents.get("isbn"), info.get("isbn")):
        c = enrich.clean_isbn(c or "")
        if c and c not in candidates:
            candidates.append(c)
    isbn, record = _find_isbn(enrich, candidates, search_title, file_authors)
    record = record or {}

    # 2. Lücken füllen – zuerst direkt per ISBN (DNB-Cover, Google-Books-Beschreibung),
    #    die langsamere Calibre-Online-Suche nur, wenn danach noch etwas fehlt
    cover = enrich.dnb_cover(isbn) if isbn else None
    comments = b.get("comments") or (enrich.google_description(isbn) if isbn else None)
    online = {}
    if isbn and (not record or not comments or not (cover or b.get("cover"))):
        online, online_cover = enrich.fetch_online(record.get("title") or search_title,
                                                   record.get("authors") or file_authors, isbn)
        if online_cover and (not cover or enrich.better_cover(cover, online_cover)):
            if cover:
                os.remove(cover)
            cover = online_cover
        elif online_cover:
            os.remove(online_cover)
        comments = comments or online.get("comments")
    found = bool(record or online)

    title = record.get("title") or (online.get("title") if online else None) or hint_title
    authors = record.get("authors") or [enrich.flip_author(a) for a in online.get("authors", [])] or file_authors
    language = record.get("language") or enrich.detect_language(info.get("text", "")) \
        or info.get("language") or online.get("language")
    year = record.get("year") or enrich.year_of(online.get("pubdate")) or enrich.year_of(info.get("date")) \
        or enrich.year_of(b.get("pubdate"))

    # 3. Reihe: DNB-Datensatz > andere DNB-Ausgaben desselben Titels > online > Hinweise im Titel
    editions = enrich.dnb_editions(enrich.split_title_suffix(enrich.clean_text(title))[0],
                                   authors[0] if authors else None)
    online_series = enrich.clean_series(enrich.clean_text(online.get("series")))
    enrich.remember_aliases([n for _, n, _ in editions] + [record.get("series"), online_series])
    series, index = record.get("series"), record.get("series_index")
    if not series:
        series, index = enrich.series_from_editions(editions)
    if not series and online_series:
        series, index = online_series, online.get("series_index")
    if not series and hint_series:
        series, index = hint_series, hint_index
    if not series and title_suffix:
        for other in list_books(library):
            if other.get("id") != book_id and other.get("series") and \
                    enrich.series_key(other["series"]) == enrich.series_key(title_suffix):
                series, index = other["series"], None
                break
    if not series and b.get("series"):
        series, index = enrich.clean_series(enrich.clean_text(b["series"])), None

    # 4. Einheitliche Schreibweisen
    title = enrich.clean_text(title)
    if series:
        series = canonical_series(library, book_id, series)
        title = enrich.strip_series_from_title(title, series)
        main, suffix = enrich.split_title_suffix(title)
        if suffix and enrich.series_key(suffix) in enrich.alias_keys(series):
            title = main  # ' – Die Dämonensaga' o. Ä. am Titelende
    known = {x.get("publisher") for x in list_books(library) if x.get("publisher") and x.get("id") != book_id}
    publisher = enrich.pick_publisher(record.get("publishers", []) + [
        enrich.imprint_from_rights(info.get("rights")), info.get("publisher"), online.get("publisher"),
        b.get("publisher")], known)
    pubdate = "%d-01-01T12:00:00+00:00" % year if year else None

    # 5. Änderungen sammeln
    if title and title != cur_title:
        changes["title"] = title
    if authors and authors != cur_authors:
        changes["authors"] = authors
    cur_langs = b.get("languages") or []
    if isinstance(cur_langs, str):
        cur_langs = [cur_langs]
    if language and cur_langs[:1] != [language]:
        changes["languages"] = language
    if publisher and publisher != b.get("publisher"):
        changes["publisher"] = publisher
    if pubdate and not str(b.get("pubdate") or "").startswith(pubdate[:10] + "T12:00"):
        changes["pubdate"] = pubdate
    if series and series != b.get("series"):
        changes["series"] = series
    if series and index and float(index) != float(b.get("series_index") or 0):
        changes["series_index"] = index
    wanted = {"isbn": isbn} if isbn else {}
    if wanted != idents:
        changes["identifiers"] = ",".join("%s:%s" % kv for kv in wanted.items())
    if not b.get("comments") and comments:
        changes["comments"] = comments
    if cover and enrich.better_cover(b.get("cover"), cover):
        changes["cover"] = cover

    # Markierung, wenn keine verlässliche Quelle gefunden wurde -> von Hand prüfen
    tags = [t.strip() for t in (b.get("tags") if isinstance(b.get("tags"), list)
                                else str(b.get("tags") or "").split(",")) if t.strip()]
    needs_review = not isbn or not found
    new_tags = [t for t in tags if t != REVIEW_TAG] + ([REVIEW_TAG] if needs_review else [])
    if new_tags != tags:
        changes["tags"] = new_tags
    if needs_review:
        log.info("#%s: keine verlässlichen Daten gefunden – markiert als „%s“", book_id, REVIEW_TAG)

    if changes:
        log.info("Metadaten für #%s ergänzt: %s", book_id, ", ".join(sorted(changes)))
        set_metadata(library, book_id, changes, internal=True)
        if epub:
            calibredb(library, "embed_metadata", str(book_id))
    if cover:
        try:
            os.remove(cover)
        except OSError:
            pass
    labels = {"languages": "Sprache", "authors": "Autor", "identifiers": "ISBN", "comments": "Beschreibung",
              "publisher": "Verlag", "pubdate": "Erscheinungsjahr", "series": "Reihe", "series_index": "Band",
              "title": "Titel", "cover": "Cover", "tags": "Markierung"}
    return [labels.get(k, k) for k in changes if k in labels]


def remove_book(library, book_id):
    rc, out = calibredb(library, "remove", str(book_id))
    invalidate()
    if rc != 0:
        raise PipelineError("Löschen fehlgeschlagen:\n" + tail(out))


def book(library, book_id):
    rc, out = calibredb(library, "list", "--for-machine", "--fields",
                        "id,title,authors,formats", "--search", "id:%d" % book_id)
    try:
        rows = json.loads(out[out.index("["):])
    except ValueError:
        rows = []
    if not rows:
        raise PipelineError("Buch %d nicht gefunden" % book_id)
    return rows[0]


def still_drm(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".epub":
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                if "META-INF/rights.xml" in names:
                    return True
                if "META-INF/encryption.xml" in names:
                    enc = zf.read("META-INF/encryption.xml").decode("utf-8", "ignore")
                    # Schriftart-Verschleierung (idpf/adobe obfuscation) ist kein DRM
                    algos = re.findall(r'Algorithm="([^"]+)"', enc)
                    return any("obfuscation" not in a and "xmlenc" in a for a in algos)
        except zipfile.BadZipFile:
            return True
        return False
    if ext == ".pdf":
        with open(path, "rb") as f:
            head = f.read(4 * 1024 * 1024)
        return b"EBX_HANDLER" in head or b"/ADEPT" in head
    return False


def import_book(library, path):
    """Fügt die Datei hinzu. DeACSM/DeDRM laufen dabei als Import-Plugins."""
    os.makedirs(library, exist_ok=True)
    rc, out = calibredb(library, "add", path, timeout=900)
    invalidate()
    match = re.search(r"Added book ids?:\s*([\d, ]+)", out)
    if not match:
        if "already exist" in out:
            raise PipelineError("Das Buch ist schon in der Bibliothek (gleicher Titel/Autor). "
                                "Du kannst es unten erneut senden.")
        raise PipelineError("Import fehlgeschlagen:\n" + tail(out))
    book_id = int(match.group(1).split(",")[0])
    info = book(library, book_id)
    formats = info.get("formats") or []
    real = [f for f in formats if not f.lower().endswith(".acsm")]
    if not real:
        calibredb(library, "remove", str(book_id))
        invalidate()
        raise PipelineError("Die ACSM-Datei konnte nicht eingelöst werden. Ist ein Adobe-Konto "
                            "verknüpft und wurde die ACSM noch nicht anderweitig benutzt?\n" + tail(out))
    locked = [f for f in real if still_drm(f)]
    if locked:
        calibredb(library, "remove", str(book_id))
        invalidate()
        raise PipelineError("DRM konnte nicht entfernt werden – passender Adobe-Schlüssel fehlt "
                            "(bei Büchern aus einem bestehenden ADE-Konto adobekey.der hochladen).\n"
                            + tail(out))
    return info, out


def tail(text, lines=25):
    keep = [l for l in text.splitlines() if l.strip()]
    return "\n".join(keep[-lines:])


# ---------------------------------------------------------------- Kindle

def pick_format(formats):
    by_ext = {os.path.splitext(f)[1].lstrip(".").upper(): f for f in formats}
    for ext in KINDLE_FORMAT_ORDER:
        if ext in by_ext:
            return by_ext[ext]
    raise PipelineError("Kein Kindle-taugliches Format (EPUB/PDF) vorhanden: "
                        + (", ".join(by_ext) or "-"))


def send_to_kindle(sender, kindle_email, path, title):
    if not kindle_email:
        raise PipelineError("Keine Kindle-Adresse in den Add-on-Optionen eingetragen.")
    if not sender.get("password") and sender.get("security") != "none":
        raise PipelineError("Für Absender '%s' ist kein Passwort hinterlegt." % sender.get("name"))
    size = os.path.getsize(path)
    if size > MAX_MAIL_BYTES:
        raise PipelineError("Datei ist %.0f MB groß – Amazon akzeptiert per Mail max. 50 MB."
                            % (size / 1048576))

    msg = EmailMessage()
    msg["From"] = sender["from_address"]
    msg["To"] = kindle_email
    msg["Subject"] = title
    msg.set_content("Gesendet von Home Assistant (Calibre → Kindle).")
    ext = os.path.splitext(path)[1].lower()
    subtype = {".epub": "epub+zip", ".pdf": "pdf"}.get(ext, "octet-stream")
    filename = safe_filename(title) + ext
    with open(path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype=subtype, filename=filename)

    host, port, sec = sender["smtp_host"], int(sender["smtp_port"]), sender.get("security", "starttls")
    ctx = ssl.create_default_context()
    log.info("Sende '%s' über %s:%s an %s", filename, host, port, kindle_email)
    if sec == "ssl":
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=120)
    else:
        server = smtplib.SMTP(host, port, timeout=120)
    with server:
        if sec == "starttls":
            server.starttls(context=ctx)
        if sender.get("username"):
            server.login(sender["username"], sender.get("password", ""))
        server.send_message(msg)


def safe_filename(title):
    name = re.sub(r'[\\/:*?"<>|]+', "", title).strip() or "Buch"
    return name[:120]
