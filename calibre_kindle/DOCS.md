# Calibre → Kindle

## Einrichtung

### 1. Optionen
| Option | Bedeutung |
|---|---|
| `kindles` | Liste der Empfänger (`name` + `email`, die `…@kindle.com`-Adresse aus Amazon → Inhalte und Geräte → Einstellungen → Persönliche Dokumente) |
| `default_kindle` | Name des Kindles, der beim Senden vorausgewählt ist |
| `library_path` | Calibre-Bibliothek, Standard `/share/calibre/library` |
| `default_sender` | Name des Absenders, über den alle Mails verschickt werden |
| `senders` | Liste von SMTP-Absendern (Zugangsdaten) |
| `auto_send` | Haken „an Kindle senden“ standardmäßig gesetzt |
| `notify` | HA-Benachrichtigung nach Versand/Fehler |

Jeder Absender **muss** bei Amazon in der „Liste genehmigter E-Mail-Adressen“ stehen.

Beispiel mit zwei Kindles:
```yaml
kindles:
  - name: Paperwhite
    email: name_abc123@kindle.com
  - name: Kindle Kids
    email: kind_xyz@kindle.com
default_kindle: Paperwhite
```
Im Panel wählst du beim Hochladen bzw. „An Kindle senden“ den Empfänger aus.

Beispiel mit zwei Absendern:
```yaml
senders:
  - name: gmail
    from_address: name@gmail.com
    smtp_host: smtp.gmail.com
    smtp_port: 587
    security: starttls
    username: name@gmail.com
    password: "16-stelliges App-Passwort"
  - name: eigene-domain
    from_address: no-reply@example.com
    smtp_host: smtp.example.com
    smtp_port: 465
    security: ssl
    username: no-reply@example.com
    password: "..."
```
Gmail: App-Passwort unter https://myaccount.google.com/apppasswords erstellen (2-Faktor muss aktiv sein).

### 2. Adobe autorisieren
Adobe hat die Anmeldung mit Adobe-ID für Adobe Digital Editions abgeschaltet
(`E_ADEPT_RESET_PW_REQUIRED`). Das Add-on autorisiert sich daher **anonym**:
Panel → „Adobe-DRM einrichten“ → **Anonym autorisieren**. Damit lassen sich neue `.acsm`-Dateien
einlösen und entschlüsseln.

Bücher, die früher mit einer Adobe-ID in ADE geladen wurden, brauchen den alten Schlüssel:
`adobekey.der` aus einer bestehenden DeDRM-/ADE-Installation exportieren und im Panel hochladen –
oder das Buch beim Shop neu herunterladen (neue `.acsm`).

## Benutzung
Panel „Bücher“ → Datei hochladen (`.acsm`, `.epub`, `.pdf`). Der Fortschritt steht unter „Aufträge“.
Bücher aus der Bibliothek lassen sich mit „An Kindle“ erneut senden.

Eine `.acsm` kann nur **einmal** eingelöst werden – wurde sie schon in ADE geöffnet, lade stattdessen
die EPUB aus `Dokumente/My Digital Editions` hoch.

## Dateien
- `/share/calibre/library` – Calibre-Bibliothek (per Samba erreichbar, mit Calibre-Desktop/Calibre-Web nutzbar)
- `/data/calibre-config` – Calibre-Konfiguration, Plugins, Adobe-Aktivierung (im HA-Backup enthalten)
