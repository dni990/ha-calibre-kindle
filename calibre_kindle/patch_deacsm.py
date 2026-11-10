"""Patcht das in DeACSM gebündelte oscrypto.

oscrypto 1.3.0 erkennt OpenSSL-Versionen mit zweistelliger Patch-Nummer
(z. B. "OpenSSL 3.0.15") nicht und bricht dann ab. Debian bookworm liefert
genau so eine Version, deshalb wird die Regex hier toleranter gemacht.
"""
import io
import sys
import zipfile

OLD = r"\\b(\\d\\.\\d\\.\\d[a-z]*)\\b"
NEW = r"\\b(\\d+\\.\\d+\\.\\d+[a-z]*)\\b"


def rezip(data, transform):
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, transform(info.filename, src.read(info)))
    return out.getvalue()


patched = []


def patch_oscrypto(name, content):
    if name.endswith(("_libcrypto_ctypes.py", "_libcrypto_cffi.py")):
        text = content.decode("utf-8")
        if OLD in text:
            patched.append(name)
            return text.replace(OLD, NEW).encode("utf-8")
    return content


def patch_plugin(name, content):
    if name == "oscrypto.zip":
        return rezip(content, patch_oscrypto)
    return content


path = sys.argv[1]
with open(path, "rb") as f:
    result = rezip(f.read(), patch_plugin)
if not patched:
    sys.exit("oscrypto-Regex nicht gefunden – DeACSM-Version prüfen")
with open(path, "wb") as f:
    f.write(result)
print("oscrypto gepatcht:", ", ".join(patched))
