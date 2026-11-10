# Calibre → Kindle (Home Assistant Add-on)

Calibre-Bibliothek direkt in Home Assistant: Buch im Seitenleisten-Panel **„Bücher“** hochladen,
Adobe-DRM wird entfernt, das Buch landet in der Calibre-Bibliothek und geht per E-Mail an den Kindle.

- `.acsm` wird mit [DeACSM](https://github.com/Leseratte10/acsm-calibre-plugin) eingelöst (ersetzt Adobe Digital Editions)
- Adobe-DRM (EPUB/PDF) wird mit [DeDRM (noDRM-Fork)](https://github.com/noDRM/DeDRM_tools) entfernt
- Bibliothek liegt in `/share/calibre/library` und ist mit Calibre / Calibre-Web kompatibel
- Versand über frei wählbare SMTP-Absender (z. B. Gmail und eigene Domain)

## Installation
Einstellungen → Add-ons → Add-on-Store → ⋮ → Repositories → `https://github.com/dni990/ha-calibre-kindle`,
dann „Calibre → Kindle“ installieren. Details: [calibre_kindle/DOCS.md](calibre_kindle/DOCS.md).

Nur für selbst gekaufte Bücher zur privaten Nutzung.
