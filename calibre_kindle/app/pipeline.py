"""Verarbeitung: Datei -> Calibre-Bibliothek (DRM via Plugins entfernt) -> Kindle."""
import json
import logging
import os
import re
import smtplib
import ssl
import subprocess
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

def ensure_plugins():
    """DeDRM + DeACSM in die persistente Calibre-Konfiguration installieren."""
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    for zip_name, installed in (("DeDRM_plugin.zip", "DeDRM.zip"), ("DeACSM.zip", "DeACSM.zip")):
        src = os.path.join("/opt/plugins", zip_name)
        dst = os.path.join(PLUGIN_DIR, installed)
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

def calibredb(library, *args, timeout=600):
    return run(["calibredb", "--library-path", library, *args], timeout=timeout)


LIST_FIELDS = ("id,title,authors,series,series_index,languages,pubdate,publisher,tags,"
               "formats,timestamp,identifiers,cover,rating")
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


def book_details(library, book_id):
    rc, out = calibredb(library, "list", "--for-machine", "--fields", LIST_FIELDS + ",comments",
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


def enrich_book(library, book_id, auto=False):
    """Metadaten verbessern. auto=True direkt nach dem Import (darf mehr überschreiben)."""
    import enrich
    b = book_details(library, book_id)
    changes = {}
    formats = b.get("formats") or []
    epub = next((f for f in formats if f.lower().endswith(".epub")), None)
    info = enrich.read_epub(epub) if epub else {"text": ""}

    # Sprache: Text-Erkennung > EPUB-Angabe
    lang = enrich.detect_language(info.get("text", "")) or info.get("language")
    current_langs = b.get("languages") or []
    if isinstance(current_langs, str):
        current_langs = [current_langs]
    if lang and (auto or not current_langs) and current_langs[:1] != [lang]:
        changes["languages"] = lang

    # Autoren 'Nachname, Vorname' -> 'Vorname Nachname'
    authors = b.get("authors") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(" & ")]
    flipped = [enrich.flip_author(a) for a in authors]
    if flipped != authors:
        changes["authors"] = flipped
        authors = flipped

    idents = identifiers_dict(b.get("identifiers"))
    isbn = idents.get("isbn") or info.get("isbn")

    online, cover = enrich.fetch_online(b.get("title"), authors, isbn)
    if not isbn and online.get("isbn"):
        isbn = online["isbn"]
    if isbn and idents.get("isbn") != isbn:
        idents["isbn"] = isbn
        changes["identifiers"] = ",".join("%s:%s" % kv for kv in idents.items())

    comments = b.get("comments") or online.get("comments")
    if not b.get("comments") and online.get("comments"):
        changes["comments"] = online["comments"]
    if not b.get("publisher") and online.get("publisher"):
        changes["publisher"] = online["publisher"]
    if online.get("pubdate") and (auto or not b.get("pubdate") or str(b.get("pubdate")).startswith("0101")):
        changes["pubdate"] = online["pubdate"]
    if not current_langs and not lang and online.get("language"):
        changes["languages"] = online["language"]

    if not b.get("series"):
        series, index = online.get("series"), online.get("series_index")
        if not series:
            series, index = enrich.series_from_text(b.get("title"), comments)
        if series:
            changes["series"] = series
            changes["series_index"] = index or "1"
            new_title = enrich.strip_series_from_title(b.get("title", ""), series)
            m = re.match(r"^(.+?)\s+\d+(?:[.,]\d+)?\s*[:–—-]\s+(.+)$", b.get("title", ""))
            if m and m.group(1).strip() == series:
                new_title = m.group(2).strip()
            if new_title != b.get("title"):
                changes["title"] = new_title

    if cover and not b.get("cover"):
        changes["cover"] = cover
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
              "publisher": "Verlag", "pubdate": "Erscheinungsdatum", "series": "Reihe", "series_index": "Band",
              "title": "Titel", "cover": "Cover"}
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
