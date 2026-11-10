"""Adobe-Autorisierung für DeACSM, ohne Calibre-GUI.

Wird mit `calibre-debug -e /app/adobe_auth.py` gestartet, damit Calibres
Python und Plugin-Umgebung verfügbar sind. Parameter kommen über Umgebungs-
variablen: ADOBE_MAIL, ADOBE_PASS (leer = anonym), ADOBE_ACTION (auth|status).
Gibt am Ende genau eine JSON-Zeile mit Präfix "RESULT:" aus.
"""
import json
import os
import sys
import traceback


def result(**data):
    print("RESULT:" + json.dumps(data))
    sys.exit(0)


try:
    from calibre.customize.ui import initialized_plugins
    from calibre.utils.config import config_dir

    # Lädt DeACSM -> entpackt oscrypto/asn1crypto und setzt sys.path
    list(initialized_plugins())
    sys.path.insert(0, os.path.join(config_dir, "plugins", "DeACSM.zip"))

    from libadobe import (createDeviceKeyFile, update_account_path,
                          VAR_VER_BUILD_IDS, VAR_VER_DEFAULT_BUILD_ID, VAR_VER_SUPP_CONFIG_NAMES)
    from libadobeAccount import (createDeviceFile, getAuthMethodsAndCert, createUser,
                                 signIn, activateDevice, getAccountUUID,
                                 exportAccountEncryptionKeyBytes)
except Exception:
    result(ok=False, error="DeACSM konnte nicht geladen werden", detail=traceback.format_exc())

account_dir = os.path.join(config_dir, "plugins", "DeACSM", "account")
os.makedirs(account_dir, exist_ok=True)
update_account_path(account_dir)


def current_key():
    key = exportAccountEncryptionKeyBytes()
    return key.hex() if key else None


action = os.environ.get("ADOBE_ACTION", "status")

if action == "status":
    uuid = None
    try:
        uuid = getAccountUUID()
    except Exception:
        pass
    result(ok=True, authorized=bool(uuid), uuid=uuid, key=current_key())

if action == "auth":
    if os.path.exists(os.path.join(account_dir, "activation.xml")):
        result(ok=False, error="Es ist bereits ein Adobe-Konto verknüpft.")
    mail = os.environ.get("ADOBE_MAIL", "").strip()
    passwd = os.environ.get("ADOBE_PASS", "")
    wanted = os.environ.get("ADOBE_VERSION", "")
    if wanted in VAR_VER_SUPP_CONFIG_NAMES:
        vers_idx = VAR_VER_SUPP_CONFIG_NAMES.index(wanted)
    else:
        vers_idx = VAR_VER_BUILD_IDS.index(VAR_VER_DEFAULT_BUILD_ID)  # ADE 2.0.1
    print("Autorisiere als %s, Konto: %s" % (VAR_VER_SUPP_CONFIG_NAMES[vers_idx], mail or "anonym"))
    try:
        auth_cert = None
        if mail:
            _types, auth_cert = getAuthMethodsAndCert()
        createDeviceKeyFile()
        createDeviceFile(False, vers_idx)
        ok, resp = createUser(vers_idx, auth_cert)
        if not ok:
            raise RuntimeError("createUser: %s" % resp)
        ok, resp = signIn("AdobeID", mail, passwd) if mail else signIn("anonymous", "", "")
        if not ok:
            raise RuntimeError("Login fehlgeschlagen: %s" % resp)
        ok, resp = activateDevice(vers_idx, None)
        if not ok:
            raise RuntimeError("Aktivierung fehlgeschlagen: %s" % resp)
    except Exception as err:
        # Halbfertige Autorisierung entfernen, damit ein neuer Versuch möglich ist
        for name in ("activation.xml", "device.xml", "devicesalt"):
            try:
                os.remove(os.path.join(account_dir, name))
            except OSError:
                pass
        result(ok=False, error=str(err), detail=traceback.format_exc())
    result(ok=True, authorized=True, uuid=getAccountUUID(), key=current_key(),
           account=mail or "anonym")

result(ok=False, error="Unbekannte Aktion %s" % action)
