# Telegram Group Scraper

Scrape les membres d'un groupe Telegram et découvre récursivement les autres groupes en commun.

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

1. Allez sur https://my.telegram.org → créez une application pour obtenir `api_id` et `api_hash`
2. Copiez `.env.example` en `.env` et remplissez vos infos :

```bash
cp .env.example .env
```

Configurez au minimum `ACCOUNT_1_*`. Ajoutez plusieurs comptes pour la rotation.

## Première connexion (OTP)

```bash
python setup_sessions.py
```

Ce script vous demande le code OTP reçu par SMS/Telegram pour chaque compte.

## Lancer l'app

```bash
python app.py
```

Ouvrez http://localhost:5000

## Paramètres anti-ban (dans .env)

| Variable | Défaut | Description |
|---|---|---|
| `MIN_DELAY` | 2 | Délai min entre requêtes (s) |
| `MAX_DELAY` | 6 | Délai max entre requêtes (s) |
| `MAX_MEMBERS_PER_GROUP` | 500 | Membres max scrappés par groupe |
| `MAX_DEPTH` | 3 | Profondeur de récursion |
| `REQUESTS_BEFORE_PAUSE` | 30 | Requêtes avant pause longue |
| `LONG_PAUSE_MIN` | 60 | Durée min de la pause longue (s) |
| `LONG_PAUSE_MAX` | 180 | Durée max de la pause longue (s) |

## Fonctionnement

1. Vous donnez un `@groupe` seed
2. Le scraper récupère les membres avec un `@username`
3. Pour chaque membre, il cherche les groupes en commun (`get_common_chats`)
4. Il ajoute les nouveaux groupes à la queue et recommence
5. Les résultats sont exportables en JSON

## Notes

- Seuls les membres avec un `@username` public sont collectés
- `get_common_chats` ne fonctionne qu'avec des utilisateurs qui partagent un groupe avec votre compte
- Augmentez les délais si vous recevez des `FloodWait`
- Les sessions sont stockées dans `sessions/` (ignoré par git)
