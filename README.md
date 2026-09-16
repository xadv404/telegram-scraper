# Telegram Group Scraper

Scrape les membres d'un groupe Telegram et découvre récursivement les autres groupes où ces membres sont actifs.

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

### 1. Obtenir api_id + api_hash

Allez sur https://my.telegram.org → créez une application.

### 2. Générer une StringSession (une seule fois)

La StringSession encode votre session sans exposer le numéro de téléphone dans la config.

```bash
python generate_session.py
```

Le script vous demande le numéro + OTP une seule fois, puis affiche une chaîne à copier dans `.env`. Après ça, le numéro n'est plus utilisé.

### 3. Configurer .env

```bash
cp .env.example .env
```

Remplissez `ACCOUNT_X_API_ID`, `ACCOUNT_X_API_HASH`, `ACCOUNT_X_SESSION`. Ajoutez autant de comptes que nécessaire pour la rotation.

## Lancer

```bash
python app.py
# http://localhost:5000
```

## Comment ça trouve les groupes

Deux méthodes combinées pour chaque membre :

1. **`messages.search` avec `from_id`** : cherche les messages publics de l'utilisateur → retourne directement les groupes où il est actif. Bien plus complet que `get_common_chats` car il ne se limite pas aux groupes en commun avec votre compte.

2. **Cross-référence** : pour chaque groupe déjà connu en base, vérifie via `channels.getParticipant` si l'utilisateur en est membre. Requête légère (1 appel par groupe).

## Paramètres anti-ban (.env)

| Variable | Défaut | Description |
|---|---|---|
| `MIN_DELAY` | 2 | Délai min entre requêtes (s) |
| `MAX_DELAY` | 6 | Délai max entre requêtes (s) |
| `MAX_MEMBERS_PER_GROUP` | 500 | Membres max scrappés par groupe |
| `MAX_DEPTH` | 3 | Profondeur de récursion |
| `REQUESTS_BEFORE_PAUSE` | 30 | Requêtes avant pause longue |
| `LONG_PAUSE_MIN` | 60 | Durée min pause longue (s) |
| `LONG_PAUSE_MAX` | 180 | Durée max pause longue (s) |

## Notes

- Seuls les membres avec `@username` public sont collectés
- `messages.search` avec `from_id` fonctionne sur les comptes utilisateur (pas les bots)
- Les sessions sont stockées sous forme de strings dans `.env`, pas dans des fichiers
- Augmentez les délais si vous recevez des erreurs FloodWait
