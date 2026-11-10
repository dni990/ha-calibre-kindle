"""Calibre → Kindle: Ingress-Web-App für Home Assistant."""
import json
import logging
import os
import queue
import shutil
import threading
import time
import uuid

import requests
from flask import Flask, abort, jsonify, render_template, request, send_file

import pipeline
from pipeline import PipelineError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("calibre_kindle")

OPTIONS_FILE = "/data/options.json"
JOBS_FILE = "/data/jobs.json"
INBOX = "/share/calibre/inbox"
MAX_JOBS = 50

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

jobs_lock = threading.Lock()
work = queue.Queue()
adobe_status = {"ok": False, "authorized": False, "checked": False}


def options():
    with open(OPTIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def sender(name=None):
    opts = options()
    senders = opts.get("senders", [])
    if not senders:
        raise PipelineError("Kein Absender in den Add-on-Optionen konfiguriert.")
    wanted = name or opts.get("default_sender")
    for s in senders:
        if s["name"] == wanted:
            return s
    return senders[0]


# ---------------------------------------------------------------- Jobs

def load_jobs():
    try:
        with open(JOBS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


jobs = load_jobs()
for j in jobs:  # nach Neustart hängende Jobs abschließen
    if j["state"] in ("wartet", "läuft"):
        j["state"], j["message"] = "fehler", "Durch Neustart abgebrochen"


def save_jobs():
    with open(JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(jobs[:MAX_JOBS], f, ensure_ascii=False, indent=1)


def new_job(kind, label, **data):
    job = {"id": uuid.uuid4().hex[:8], "kind": kind, "label": label, "state": "wartet",
           "message": "In der Warteschlange", "created": time.time(), **data}
    with jobs_lock:
        jobs.insert(0, job)
        del jobs[MAX_JOBS:]
        save_jobs()
    work.put(job)
    return job


def update(job, state=None, message=None, **extra):
    with jobs_lock:
        if state:
            job["state"] = state
        if message is not None:
            job["message"] = message
        job.update(extra)
        save_jobs()


def notify(title, message):
    opts = options()
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not opts.get("notify", True) or not token:
        return
    try:
        requests.post("http://supervisor/core/api/services/persistent_notification/create",
                      headers={"Authorization": "Bearer " + token},
                      json={"title": title, "message": message, "notification_id": "calibre_kindle"},
                      timeout=10)
    except requests.RequestException as err:
        log.warning("HA-Benachrichtigung fehlgeschlagen: %s", err)


def deliver(job, info):
    opts = options()
    snd = sender(job.get("sender"))
    path = pipeline.pick_format(info.get("formats") or [])
    update(job, "läuft", "Sende an Kindle über %s …" % snd["from_address"])
    pipeline.send_to_kindle(snd, opts.get("kindle_email", ""), path, info.get("title", "Buch"))


def process(job):
    opts = options()
    library = opts["library_path"]
    if job["kind"] == "upload":
        update(job, "läuft", "Importiere und entferne DRM …")
        try:
            info, _ = pipeline.import_book(library, job["path"])
        finally:
            try:
                os.remove(job["path"])
            except OSError:
                pass
        update(job, message="Ergänze Metadaten …", book_id=info["id"], label=info["title"])
        try:
            changed = pipeline.enrich_book(library, info["id"], auto=True)
            info = pipeline.book(library, info["id"])
            update(job, label=info["title"],
                   message="Metadaten ergänzt: " + ", ".join(changed) if changed else "In Bibliothek gespeichert")
        except Exception as err:  # noqa: BLE001 – Ergänzung ist optional
            log.warning("Metadaten-Ergänzung fehlgeschlagen: %s", err)
        if job.get("send"):
            deliver(job, info)
            update(job, "fertig", "DRM entfernt, in Bibliothek und an Kindle gesendet")
            notify("Buch an Kindle gesendet", "„%s“ wurde an deinen Kindle geschickt." % info["title"])
        else:
            update(job, "fertig", "DRM entfernt und in Bibliothek gespeichert")
    elif job["kind"] == "enrich":
        update(job, "läuft", "Suche Metadaten (lokal + online) …")
        changed = pipeline.enrich_book(library, job["book_id"])
        info = pipeline.book(library, job["book_id"])
        update(job, "fertig", ("Ergänzt: " + ", ".join(changed)) if changed else "Nichts Neues gefunden",
               label=info["title"])
    elif job["kind"] == "resend":
        info = pipeline.book(library, job["book_id"])
        update(job, label=info["title"])
        deliver(job, info)
        update(job, "fertig", "An Kindle gesendet")


def worker():
    while True:
        job = work.get()
        try:
            process(job)
        except PipelineError as err:
            log.error("Job %s: %s", job["id"], err)
            update(job, "fehler", str(err))
            notify("Calibre → Kindle: Fehler", "%s: %s" % (job["label"], str(err).splitlines()[0]))
        except Exception as err:  # noqa: BLE001
            log.exception("Job %s fehlgeschlagen", job["id"])
            update(job, "fehler", "%s: %s" % (type(err).__name__, err))
            notify("Calibre → Kindle: Fehler", "%s: %s" % (job["label"], err))


def refresh_adobe_status():
    global adobe_status
    adobe_status = {**pipeline.adobe("status"), "checked": True}


# ---------------------------------------------------------------- Routen

ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1"}  # HA-Ingress-Proxy


@app.before_request
def only_ingress():
    if request.remote_addr not in ALLOWED_CLIENTS:
        return "Nur über Home Assistant erreichbar", 403


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def state():
    opts = options()
    with jobs_lock:
        job_list = list(jobs)
    return jsonify(
        jobs=job_list,
        library_version=pipeline.library_version(),
        senders=[{"name": s["name"], "from": s["from_address"]} for s in opts.get("senders", [])],
        default_sender=opts.get("default_sender"),
        auto_send=opts.get("auto_send", True),
        kindle_email=opts.get("kindle_email", ""),
        library_path=opts["library_path"],
        adobe=adobe_status,
        adept_keys=pipeline.adept_key_names(),
    )


def public_book(b):
    b = dict(b)
    b["has_cover"] = bool(b.pop("cover", None))
    b["isbn"] = pipeline.identifiers_dict(b.pop("identifiers", None)).get("isbn", "")
    b["formats"] = [os.path.splitext(f)[1].lstrip(".").upper() for f in b.get("formats") or []]
    return b


@app.get("/api/books")
def books():
    lib = options()["library_path"]
    return jsonify(version=pipeline.library_version(),
                   books=[public_book(b) for b in pipeline.list_books(lib)])


@app.get("/api/book/<int:book_id>")
def book_detail(book_id):
    try:
        return jsonify(public_book(pipeline.book_details(options()["library_path"], book_id)))
    except PipelineError as err:
        return jsonify(error=str(err)), 404


@app.post("/api/book/<int:book_id>/meta")
def book_meta(book_id):
    try:
        pipeline.set_metadata(options()["library_path"], book_id, request.get_json(silent=True) or {})
    except PipelineError as err:
        return jsonify(error=str(err)), 400
    return jsonify(ok=True)


@app.post("/api/book/<int:book_id>/enrich")
def book_enrich(book_id):
    job = new_job("enrich", "Buch #%d" % book_id, book_id=book_id)
    return jsonify(job=job)


@app.delete("/api/book/<int:book_id>")
def book_delete(book_id):
    try:
        pipeline.remove_book(options()["library_path"], book_id)
    except PipelineError as err:
        return jsonify(error=str(err)), 400
    return jsonify(ok=True)


@app.get("/api/book/<int:book_id>/file/<fmt>")
def book_file(book_id, fmt):
    lib = options()["library_path"]
    try:
        info = pipeline.book(lib, book_id)
    except PipelineError:
        abort(404)
    for path in info.get("formats") or []:
        if path.lower().endswith("." + fmt.lower()):
            real = os.path.realpath(path)
            if real.startswith(os.path.realpath(lib) + os.sep) and os.path.exists(real):
                name = pipeline.safe_filename(info.get("title", "Buch")) + "." + fmt.lower()
                return send_file(real, as_attachment=True, download_name=name)
    abort(404)


@app.get("/api/cover/<int:book_id>")
def cover(book_id):
    lib = options()["library_path"]
    for b in pipeline.list_books(lib):
        if b["id"] == book_id and b.get("cover"):
            path = os.path.realpath(b["cover"])
            if path.startswith(os.path.realpath(lib) + os.sep) and os.path.exists(path):
                resp = send_file(path, mimetype="image/jpeg")
                resp.headers["Cache-Control"] = "max-age=86400"
                return resp
    abort(404)


@app.post("/api/upload")
def upload():
    files = request.files.getlist("file")
    if not files:
        return jsonify(error="Keine Datei"), 400
    send = request.form.get("send", "1") == "1"
    snd = request.form.get("sender") or None
    os.makedirs(INBOX, exist_ok=True)
    created = []
    for f in files:
        name = os.path.basename(f.filename or "upload")
        target_dir = os.path.join(INBOX, uuid.uuid4().hex[:8])
        os.makedirs(target_dir)
        path = os.path.join(target_dir, name)
        f.save(path)
        # iOS benennt Dateien teils um ("URLLink.acsm 2") -> Typ am Inhalt erkennen
        ext = pipeline.detect_ext(path) or os.path.splitext(name)[1].lower()
        if ext not in pipeline.BOOK_EXTS:
            shutil.rmtree(target_dir, ignore_errors=True)
            return jsonify(error="Dateityp von %s wird nicht unterstützt" % name), 400
        if not name.lower().endswith(ext):
            fixed = os.path.join(target_dir, os.path.splitext(name)[0].rstrip(". ") + ext)
            if fixed.endswith(ext + ext):
                fixed = fixed[: -len(ext)]
            os.rename(path, fixed)
            path = fixed
        created.append(new_job("upload", name, path=path, send=send, sender=snd))
    return jsonify(jobs=created)


@app.post("/api/resend/<int:book_id>")
def resend(book_id):
    snd = (request.get_json(silent=True) or {}).get("sender")
    job = new_job("resend", "Buch #%d" % book_id, book_id=book_id, sender=snd)
    return jsonify(job=job)


@app.post("/api/adobe/auth")
def adobe_auth():
    data = request.get_json(silent=True) or {}
    res = pipeline.adobe("auth", data.get("version", ""))
    refresh_adobe_status()
    return jsonify(res), (200 if res.get("ok") else 400)


@app.post("/api/adobe/key")
def adobe_key():
    f = request.files.get("file")
    if not f:
        return jsonify(error="Keine Datei"), 400
    raw = f.read()
    if not 500 < len(raw) < 5000:
        return jsonify(error="Das sieht nicht nach einem Adobe-Schlüssel (.der) aus"), 400
    name = os.path.splitext(os.path.basename(f.filename or "adobekey"))[0]
    added = pipeline.add_adept_key(name, raw.hex())
    return jsonify(ok=True, added=added)


def cleanup_inbox():
    shutil.rmtree(INBOX, ignore_errors=True)


if __name__ == "__main__":
    cleanup_inbox()
    pipeline.ensure_plugins()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=refresh_adobe_status, daemon=True).start()
    log.info("Web-Oberfläche läuft auf Port 8099 (Ingress)")
    app.run(host="0.0.0.0", port=8099, threaded=True)
