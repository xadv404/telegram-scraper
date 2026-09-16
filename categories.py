"""
Définition des catégories et classification automatique des groupes Telegram.
"""

CATEGORIES: dict[str, dict] = {
    "ofm": {
        "label": "OFM / Content",
        "color": "#ec4899",
        "keywords": [
            "onlyfans", "ofm", "of agency", "content creator", "creators agency",
            "fansly", "fanvue", "mym", "chatting agency", "chatter", "ppv",
            "model", "modele", "only fans", "adult content", "agency creator",
        ],
        "seeds": ["ofm agency", "onlyfans management", "chatting agency telegram"],
    },
    "carding": {
        "label": "Carding / CC",
        "color": "#ef4444",
        "keywords": [
            "carding", "cvv", "bins", "dumps", "fullz", "cc shop", "carder",
            "credit card", "carte bancaire", "cashout", "cc", "non vbv",
            "bank logs", "banklogs", "paypal logs", "stripe", "fraud shop",
        ],
        "seeds": ["carding", "cvv shop", "bins dumps", "cashout method"],
    },
    "crypto": {
        "label": "Crypto / DeFi",
        "color": "#f59e0b",
        "keywords": [
            "crypto", "bitcoin", "btc", "eth", "ethereum", "defi", "nft",
            "altcoin", "trading crypto", "binance", "coinbase", "p2p crypto",
            "usdt", "solana", "sol", "meme coin", "pump", "airdrop", "web3",
        ],
        "seeds": ["crypto trading", "bitcoin p2p", "defi telegram", "usdt exchange"],
    },
    "voip": {
        "label": "VOIP / SMS / DID",
        "color": "#06b6d4",
        "keywords": [
            "voip", "sms", "did", "virtual number", "numero virtuel", "sms bypass",
            "otp bypass", "sms verification", "receive sms", "sim", "esim",
            "numero usa", "number shop", "sms activate", "phone number",
            "bypass sms", "sms receive", "short code",
        ],
        "seeds": ["sms bypass", "virtual number shop", "voip did number", "otp sms"],
    },
    "hacking": {
        "label": "Hacking / Cyber",
        "color": "#10b981",
        "keywords": [
            "hacking", "hack", "exploit", "malware", "rat", "stealer",
            "phishing", "rdp", "shell", "botnet", "ddos", "stresser",
            "cracking", "leak", "database leak", "combolist", "combo list",
            "logs", "stealer logs", "infostealer", "pentesting", "red team",
        ],
        "seeds": ["hacking tools", "stealer logs", "combolist", "exploit shop"],
    },
    "forex": {
        "label": "Forex / Trading",
        "color": "#8b5cf6",
        "keywords": [
            "forex", "trading", "signal", "scalping", "prop firm", "funded",
            "mt4", "mt5", "fx", "pips", "investment signal", "trade signal",
            "copy trading", "indices", "gold xauusd",
        ],
        "seeds": ["forex signal", "prop firm trading", "copy trading telegram"],
    },
    "drugs": {
        "label": "Drogues / Marché",
        "color": "#6b7280",
        "keywords": [
            "drugs", "weed", "cocaine", "mdma", "xanax", "plug", "shop drogues",
            "cannabis", "heroin", "meth", "pills", "darknet market",
        ],
        "seeds": ["plug telegram", "weed shop", "drugs market"],
    },
    "money": {
        "label": "Blanchiment / Cash",
        "color": "#d97706",
        "keywords": [
            "money mule", "mule", "cashout", "blanchiment", "money laundering",
            "western union", "moneygram", "transfer", "wire fraud",
            "fake invoice", "scam page", "scampage",
        ],
        "seeds": ["money mule", "cashout method", "money laundering"],
    },
    "other": {
        "label": "Autre",
        "color": "#64748b",
        "keywords": [],
        "seeds": [],
    },
}

# Index inversé : keyword → catégorie
_KEYWORD_INDEX: dict[str, str] = {}
for cat_id, cat in CATEGORIES.items():
    for kw in cat["keywords"]:
        _KEYWORD_INDEX[kw.lower()] = cat_id


def classify_group(username: str, title: str = "", description: str = "") -> str:
    """
    Retourne la catégorie la plus probable pour un groupe.
    Cherche les mots-clés dans username + title + description.
    """
    text = f"{username} {title} {description}".lower()

    scores: dict[str, int] = {}
    for kw, cat_id in _KEYWORD_INDEX.items():
        if kw in text:
            scores[cat_id] = scores.get(cat_id, 0) + 1

    if not scores:
        return "other"
    return max(scores, key=lambda k: scores[k])


def get_category_seeds() -> dict[str, list[str]]:
    """Retourne les requêtes de recherche par catégorie."""
    return {cat_id: cat["seeds"] for cat_id, cat in CATEGORIES.items() if cat["seeds"]}
