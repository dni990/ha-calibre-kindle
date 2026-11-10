# Calibre → Kindle

## Einrichtung

### 1. Optionen
| Option | Bedeutung |
|---|---|
| `kindle_email` | deine `…@kindle.com`-Adresse (Amazon → Inhalte und Geräte → Einstellungen → Persönliche Dokumente) |
| `library_path` | Calibre-Bibliothek, Standard `/share/calibre/library` |
| `default_sender` | Name des Absenders, der standardmäßig benutzt wird |
| `senders` | Liste von SMTP-Absendern (im Panel auswählbar) |
| `auto_send` | Haken „an Kindle senden“ standardmäßig gesetzt |
| `notify` | HA-Benachrichtigung nach Versand/Fehler |

Jeder Absender **muss** bei Amazon in der „Liste genehmigter E-Mail-Adressen“ stehen.

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

### 2. Adobe-Konto
Im Panel unter „Adobe-DRM einrichten“:
- **Adobe-Konto verknüpfen** – für `.acsm`-Dateien. Am besten dieselbe Adobe-ID wie früher in Adobe
  Digital Editions; dann lassen sich auch ältere Downloads entschlüsseln. Achtung: belegt eine der
  Geräte-Aktivierungen deiner Adobe-ID.
- **adobekey.der hochladen** – falls du schon einen Schlüssel aus einer alten DeDRM-Installation hast.

## Benutzung
Panel „Bücher“ → Datei hochladen (`.acsm`, `.epub`, `.pdf`). Der Fortschritt steht unter „Aufträge“.
Bücher aus der Bibliothek lassen sich mit „An Kindle“ erneut senden.

Eine `.acsm` kann nur **einmal** eingelöst werden – wurde sie schon in ADE geöffnet, lade stattdessen
die EPUB aus `Dokumente/My Digital Editions` hoch.

## Dateien
- `/share/calibre/library` – Calibre-Bibliothek (per Samba erreichbar, mit Calibre-Desktop/Calibre-Web nutzbar)
- `/data/calibre-config` – Calibre-Konfiguration, Plugins, Adobe-Aktivierung (im HA-Backup enthalten)
