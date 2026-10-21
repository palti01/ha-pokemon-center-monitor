# Pokémon Center Drop-Monitor

Prüft den deutschen Pokémon-Center-Shop alle paar Minuten (mind. 45 s zwischen Abrufen) und meldet
neue Produkte, Restocks und eine aktive Warteschlange an einen Home-Assistant-Webhook.

- `webhook_url`: z. B. `http://homeassistant:8123/api/webhook/<deine-webhook-id>`
- `listings`: Kategorie- oder Suchseiten, `watch`: einzelne Produkt-URLs für Restock-Alarm
- `include_keywords`/`exclude_keywords`: Filter auf den Produktnamen

Beim ersten Start wird eine Baseline angelegt; gemeldet wird erst, was danach neu dazukommt.
Es wird nichts automatisch gekauft und kein Bot-Schutz umgangen.
