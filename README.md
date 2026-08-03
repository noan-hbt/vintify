# Vinted Scraper

Client HTTP synchrone, typé et autonome pour interroger l’API interne non documentée de Vinted.

La librairie fournit une interface stable pour les scripts de monitoring, bots Discord et outils d’analyse d’annonces. Elle gère notamment le bootstrap de session, le token CSRF, les retries, le backoff, la pagination, la normalisation des données et les endpoints authentifiés.

> **Avertissement**
>
> L’API utilisée par cette librairie est privée, non documentée et susceptible de changer sans préavis. Utilisez-la de manière responsable, respectez les conditions d’utilisation de Vinted ainsi que la législation applicable. Évitez les requêtes excessives et ne contournez pas les mécanismes de sécurité du service.

## Fonctionnalités

- Client HTTP synchrone basé sur [`cloudscraper`](https://github.com/VeNoMouS/cloudscraper).
- Support de plusieurs domaines Vinted européens.
- Bootstrap automatique de la session :
  - récupération des cookies anonymes ;
  - extraction du token CSRF depuis la page d’accueil ;
  - ajout automatique de `X-CSRF-Token`.
- Gestion centralisée des requêtes HTTP avec :
  - limitation de fréquence ;
  - retries ;
  - backoff exponentiel avec jitter ;
  - prise en charge des erreurs `429`, `401`, `403`, `404` et `5xx` ;
  - rafraîchissement automatique du token CSRF.
- Pagination générique avec déduplication et arrêt automatique.
- Barre de progression optionnelle avec `tqdm`.
- Recherche multi-critères.
- Conversion d’une URL de recherche Vinted en `SearchFilters`.
- Normalisation des annonces et des profils vendeurs.
- Accès aux articles similaires et à l’historique des prix.
- Accès aux évaluations, abonnés et abonnements d’un utilisateur.
- Accès aux données de référence : marques, catégories, tailles, couleurs et matériaux.
- Support optionnel des endpoints authentifiés avec un cookie de session.
- Téléchargement des photos d’un article.
- Utilisation possible comme context manager.

## Installation

### Depuis les sources

```bash
git clone https://github.com/noan-hbt/vintify
cd vintify

python -m venv .venv
source .venv/bin/activate
```

Sous Windows :

```powershell
python -m venv .venv
.venv\Scripts\activate
```

Installez les dépendances :

```bash
pip install -r requirements.txt
```

### Dépendances

Le fichier `requirements.txt` contient :

```txt
cloudscraper>=1.2.71
tqdm>=4.66.0
```

`tqdm` est optionnel à l’exécution. Si le paquet n’est pas installé, les fonctionnalités de progression sont simplement désactivées.

## Structure recommandée

```text
.
├── vinted_scraper.py
├── requirements.txt
├── README.md
└── examples/
    └── search.py
```

La librairie est autonome et peut être utilisée en copiant simplement `vinted_scraper.py` dans votre projet.

## Utilisation rapide

```python
from vinted_scraper import Sort, VintedScraper

with VintedScraper(domain="fr") as vinted:
    items = vinted.search_items(
        "nike tn",
        price_to=60,
        order=Sort.NEWEST,
        pages=2,
        normalize=True,
    )

    for item in items:
        print(
            item["id"],
            item["title"],
            item["price"],
            item["currency"],
            item["url"],
        )
```

La méthode `search_items()` retourne une liste. Pour traiter les résultats progressivement et interrompre rapidement la pagination, utilisez `iter_items()` :

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    for item in vinted.iter_items(
        search_text="carhartt",
        price_to=80,
        pages=3,
        normalize=True,
    ):
        print(item["title"])

        # Exemple :
        # if already_seen(item["id"]):
        #     break
```

## Recherche avec des filtres

### Filtres directs

```python
from vinted_scraper import Sort, VintedScraper

with VintedScraper(domain="fr") as vinted:
    items = vinted.search_items(
        search_text="veste",
        brand_ids=[362],
        size_ids=[3, 4],
        price_from=20,
        price_to=80,
        currency="EUR",
        order=Sort.NEWEST,
        pages=3,
        normalize=True,
    )
```

Les paramètres de recherche disponibles comprennent notamment :

- `search_text`
- `catalog_ids`
- `brand_ids`
- `size_ids`
- `status_ids`
- `color_ids`
- `material_ids`
- `country_ids`
- `city_ids`
- `price_from`
- `price_to`
- `currency`
- `is_for_swap`
- `order`

Les listes d’identifiants sont automatiquement converties au format CSV attendu par l’API.

### Utiliser `SearchFilters`

```python
from vinted_scraper import SearchFilters, VintedScraper

filters = SearchFilters(
    search_text="carhartt",
    price_to=80,
    order="newest_first",
)

with VintedScraper(domain="fr") as vinted:
    for item in vinted.iter_items(
        filters=filters,
        pages=2,
        normalize=True,
    ):
        print(item["title"], item["price"])
```

### Convertir une URL Vinted

```python
from vinted_scraper import SearchFilters, VintedScraper

filters = SearchFilters.from_url(
    "https://www.vinted.fr/catalog"
    "?search_text=carhartt"
    "&price_to=80"
    "&order=newest_first"
)

# Compatible avec une sauvegarde JSON
payload = filters.to_dict()
print(payload)

with VintedScraper(domain="fr") as vinted:
    items = vinted.search_items(
        filters=filters,
        pages=2,
        normalize=True,
    )
```

Recharger les filtres depuis un dictionnaire :

```python
import json

from vinted_scraper import SearchFilters

with open("monitor.json", encoding="utf-8") as file:
    data = json.load(file)

filters = SearchFilters.from_dict(data)
```

## Normalisation des données

L’API peut retourner les prix sous plusieurs formats. Les fonctions de normalisation produisent des dictionnaires plus stables et plus simples à exploiter.

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    item = vinted.get_item(4_123_456_789, normalize=True)

print(item)
```

Exemple de résultat normalisé :

```python
{
    "id": 4123456789,
    "title": "Nike Air Max",
    "description": "...",
    "brand": "Nike",
    "brand_id": 53,
    "size": "42",
    "status": "Très bon état",
    "price": 55.0,
    "total_price": 59.5,
    "currency": "EUR",
    "url": "https://www.vinted.fr/items/4123456789",
    "photo": "https://...",
    "photo_count": 4,
    "favourites": 12,
    "views": 180,
    "user_id": 12345678,
    "user_login": "vendeur",
    "country": "FR",
    "created_at": "...",
    "updated_at": "...",
    "is_closed": False,
    "promoted": False,
}
```

Il est également possible d’utiliser directement les helpers :

```python
from vinted_scraper import normalize_item, normalize_user

normalized_item = normalize_item(raw_item)
normalized_user = normalize_user(raw_user)
```

## Articles et analyse des prix

### Récupérer un article

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    item = vinted.get_item(4_123_456_789)
    full_item = vinted.get_item_full(4_123_456_789)
```

### Articles similaires

```python
with VintedScraper(domain="fr") as vinted:
    similar_items = vinted.get_similar_items(
        4_123_456_789,
        limit=25,
        normalize=True,
    )

    prices = [
        item["price"]
        for item in similar_items
        if item.get("price") is not None
    ]

    if prices:
        median = sorted(prices)[len(prices) // 2]
        print(f"Prix médian : {median}")
```

### Historique de prix

```python
with VintedScraper(domain="fr") as vinted:
    history = vinted.get_item_price_history(4_123_456_789)

    for change in history:
        print(change)
```

### Construire l’URL d’un article

```python
with VintedScraper(domain="fr") as vinted:
    url = vinted.item_url(4_123_456_789, slug="nike-air-max")
    print(url)
```

## Utilisateurs et vendeurs

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    user = vinted.get_user(12345678)
    stats = vinted.get_user_stats(12345678)

    items = vinted.get_user_items(
        12345678,
        pages=2,
        normalize=True,
    )

    feedback = vinted.get_user_feedback(
        12345678,
        pages=3,
    )
```

Autres méthodes disponibles :

```python
vinted.get_user_followers(user_id)
vinted.get_user_following(user_id)
vinted.search_users("pseudo")
```

## Marques et données de référence

Les données de référence sont mises en cache en mémoire après le premier appel.

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    brands = vinted.get_brands()
    categories = vinted.get_categories()
    sizes = vinted.get_sizes()
    colors = vinted.get_colors()
    materials = vinted.get_materials()
    statuses = vinted.get_statuses()

    brand = vinted.find_brand("Carhartt WIP")
    print(brand)
```

Forcer le rafraîchissement du cache :

```python
with VintedScraper(domain="fr") as vinted:
    brands = vinted.get_brands(refresh=True)
```

## Endpoints authentifiés

Les fonctionnalités privées, comme la messagerie, nécessitent un cookie de session Vinted valide.

> Ne stockez jamais votre cookie directement dans le code ou dans le dépôt Git. Utilisez une variable d’environnement ou un gestionnaire de secrets.

```python
import os

from vinted_scraper import VintedAuthError, VintedScraper

session_cookie = os.environ["VINTED_SESSION"]

with VintedScraper(
    domain="fr",
    session_cookie=session_cookie,
) as vinted:
    try:
        threads = vinted.get_messages(
            user_id=12345678,
            pages=3,
        )

        conversation = vinted.get_messages_with_user(
            me_id=12345678,
            other_user_id=87654321,
        )

    except VintedAuthError:
        print("Cookie expiré ou accès refusé.")
```

Exemple de configuration de l’environnement :

```bash
export VINTED_SESSION="votre_cookie_de_session"
```

Sous Windows PowerShell :

```powershell
$env:VINTED_SESSION = "votre_cookie_de_session"
```

## Achat (checkout) et bypass DataDome

Le flux d'achat (build checkout, paiement) et le gateway `api.vinted.fr`
(points relais) sont protégés par **DataDome** : sans contournement, ils
renvoient `403` (challenge slider en JSON inline sur `www.vinted.fr`, block
page HTML sur `api.vinted.fr`).

### Résolution DataDome via Hyper Solutions

Le bypass est intégré et automatique :

1. Installer le client TLS imitant Chrome : `pip install curl_cffi`.
2. Fournir la clé API Hyper Solutions (<https://docs.hypersolutions.co>) au
   constructeur (`hypersolutions_api_key=` ou variable d'environnement
   `HYPERSOLUTIONS_API_KEY`).
3. Le cookie `datadome` de votre navigateur (optionnel mais recommandé) :
   requis par le gateway `api.vinted.fr`. Il se met dans le `cookie_file`.

```python
from vinted_scraper import VintedScraper

with VintedScraper(
    domain="fr",
    cookie_file="vinted_session.json",      # session + JWT + datadome navigateur
    hypersolutions_api_key=os.environ["HYPER_API_KEY"],
) as v:
    # 1. transaction (créée automatiquement si besoin)
    t = v.create_transaction([item_id], seller_id)
    # 2. build du checkout (résout le challenge DataDome automatiquement)
    co = v.build_checkout(t["id"], purchase_type="transaction")
    checkout_id = co["checkout"]["id"]
    # 3. points relais autour de l'adresse (gateway api.vinted.fr)
    pts = v.get_nearby_pickup_points(checkout_id, latitude, longitude)
    # 4. sélection d'un point (rate_uuid doit correspondre au transporteur)
    v.select_pickup_point(
        checkout_id,
        rate_uuid=pts["shipping_rates"][0]["rate_uuid"],
        point_code=pts["shipping_points"][0]["point"]["code"],
        point_uuid=pts["shipping_points"][0]["point"]["uuid"],
    )
    # 5. paiement — ⚠️ débite réellement : confirm=True est obligatoire
    co = v.get_checkout(checkout_id)["checkout"]
    v.pay_checkout(checkout_id, checksum=co["checksum"], card_id=298093835, confirm=True)
```

Points d'attention :

- Le `checksum` change à chaque `update_checkout` : relisez le checkout avant
  de payer.
- Le `point_code`/`point_uuid` doivent appartenir au transporteur du
  `rate_uuid` choisi (un point Chronopost ne se sélectionne pas avec un tarif
  Mondial Relay).
- Le cookie `datadome` du navigateur expire (~30 min à 2 h) : recopiez-le
  avec les cookies de session quand le gateway refuse (`403 access_denied`).
- Le gateway refuse le header `Authorization` (Bearer) : géré automatiquement
  par `get_nearby_pickup_points`.

## Téléchargement des photos

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr") as vinted:
    downloaded = vinted.download_item_images(
        item_id=4_123_456_789,
        directory="images",
        overwrite=False,
        progress=True,
    )

for path in downloaded:
    print(path)
```

Les fichiers sont enregistrés dans le répertoire suivant :

```text
images/
└── 4123456789/
    ├── 01.jpg
    ├── 02.jpg
    └── 03.jpg
```

## Barre de progression

La progression est désactivée par défaut :

```python
with VintedScraper(domain="fr") as vinted:
    items = vinted.search_items(
        "nike",
        pages=None,
        progress=True,
    )
```

Si `tqdm` n’est pas installé, `progress=True` ne provoque pas d’erreur.

## Domaines supportés

| Code | Domaine | Locale |
|---|---|---|
| `fr` | `vinted.fr` | `fr-FR` |
| `be` | `vinted.be` | `fr-BE` |
| `nl` | `vinted.nl` | `nl-NL` |
| `de` | `vinted.de` | `de-DE` |
| `at` | `vinted.at` | `de-AT` |
| `es` | `vinted.es` | `es-ES` |
| `it` | `vinted.it` | `it-IT` |
| `pt` | `vinted.pt` | `pt-PT` |
| `pl` | `vinted.pl` | `pl-PL` |
| `cz` | `vinted.cz` | `cs-CZ` |
| `sk` | `vinted.sk` | `sk-SK` |
| `lt` | `vinted.lt` | `lt-LT` |
| `lv` | `vinted.lv` | `lv-LV` |
| `se` | `vinted.se` | `sv-SE` |
| `fi` | `vinted.fi` | `fi-FI` |
| `dk` | `vinted.dk` | `da-DK` |
| `ro` | `vinted.ro` | `ro-RO` |
| `hu` | `vinted.hu` | `hu-HU` |
| `gr` | `vinted.gr` | `el-GR` |
| `lu` | `vinted.lu` | `fr-LU` |
| `ie` | `vinted.ie` | `en-IE` |
| `co.uk` | `vinted.co.uk` | `en-GB` |
| `com` | `vinted.com` | `en-US` |

Exemple :

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="de") as vinted:
    items = vinted.search_items("jacke", pages=1)
```

## Configuration du client

```python
from vinted_scraper import VintedScraper

vinted = VintedScraper(
    domain="fr",
    timeout=30,
    retries=5,
    min_request_interval=0.25,
    auto_refresh_csrf=True,
    verbose=True,
)
```

Principaux paramètres :

| Paramètre | Description | Valeur par défaut |
|---|---|---:|
| `domain` | Domaine Vinted cible | `fr` |
| `session_cookie` | Cookie de session pour les endpoints privés | `None` |
| `timeout` | Timeout HTTP en secondes | `30` |
| `retries` | Nombre de tentatives supplémentaires | `5` |
| `verbose` | Active les logs détaillés | `False` |
| `proxies` | Mapping de proxies transmis à la session HTTP | `None` |
| `min_request_interval` | Intervalle minimal entre deux requêtes | `0.25` |
| `auto_refresh_csrf` | Rafraîchit automatiquement le CSRF après un refus | `True` |
| `bootstrap` | Initialise automatiquement la session | `True` |

Pour éviter le bootstrap automatique :

```python
vinted = VintedScraper(
    domain="fr",
    bootstrap=False,
)

vinted.refresh_session()
```

## Pagination

Les méthodes paginées acceptent un paramètre `pages`.

```python
with VintedScraper(domain="fr") as vinted:
    first_page = vinted.search_items("nike", pages=1)
    three_pages = vinted.search_items("nike", pages=3)
    all_pages = vinted.search_items("nike", pages=None)
```

Lorsque `pages=None` ou `pages=0`, la pagination continue jusqu’à épuisement, avec une limite de sécurité de `200` pages.

Le paramètre `page_size` est automatiquement limité à `96`, qui correspond à la taille maximale prise en charge par le client.

## Gestion des erreurs

Les exceptions publiques sont :

| Exception | Situation |
|---|---|
| `VintedError` | Erreur générique |
| `VintedAuthError` | Cookie invalide, CSRF expiré ou accès refusé |
| `VintedNotFoundError` | Ressource inexistante ou supprimée |
| `VintedRateLimitError` | Limite de fréquence atteinte après les retries |
| `VintedResponseError` | Réponse non JSON ou JSON illisible |

Exemple :

```python
from vinted_scraper import (
    VintedAuthError,
    VintedNotFoundError,
    VintedRateLimitError,
    VintedScraper,
)

with VintedScraper(domain="fr") as vinted:
    try:
        item = vinted.get_item(4_123_456_789)

    except VintedNotFoundError:
        print("Article introuvable.")

    except VintedRateLimitError:
        print("Trop de requêtes. Réessayez plus tard.")

    except VintedAuthError:
        print("Accès refusé ou session expirée.")
```

Les exceptions héritent de `VintedError` et exposent également :

- `status`
- `url`
- `body`
- `message`

## Ligne de commande

Le fichier peut être exécuté directement :

```bash
python vinted_scraper.py nike \
    --domain fr \
    --pages 2 \
    --price-to 60 \
    --verbose
```

Options disponibles :

```text
usage: vinted_scraper.py [-h] [--domain DOMAIN] [--pages PAGES]
                         [--price-to PRICE_TO] [--verbose]
                         [query]

Recherche rapide sur Vinted.
```

## Logging

La librairie utilise le logger `vinted_scraper`.

Par défaut, aucun handler n’est ajouté à l’application appelante :

```python
import logging

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("vinted_scraper")
logger.setLevel(logging.DEBUG)
```

Ou activez les logs intégrés :

```python
from vinted_scraper import VintedScraper

with VintedScraper(domain="fr", verbose=True) as vinted:
    vinted.search_items("nike", pages=1)
```

## API publique

Les principaux objets et fonctions exportés sont :

```python
from vinted_scraper import (
    KNOWN_DOMAINS,
    SearchFilters,
    Sort,
    VintedAuthError,
    VintedError,
    VintedNotFoundError,
    VintedRateLimitError,
    VintedResponseError,
    VintedScraper,
    normalize_item,
    normalize_user,
)
```

### `VintedScraper`

#### Catalogue

- `iter_items()`
- `search_items()`
- `get_item()`
- `get_item_full()`
- `get_similar_items()`
- `get_item_price_history()`
- `item_url()`

#### Utilisateurs

- `get_user()`
- `get_user_stats()`
- `iter_user_items()`
- `get_user_items()`
- `iter_user_feedback()`
- `get_user_feedback()`
- `get_user_followers()`
- `get_user_following()`
- `search_users()`

#### Données de référence

- `get_catalog_initializers()`
- `get_categories()`
- `get_brands()`
- `get_sizes()`
- `get_colors()`
- `get_materials()`
- `get_statuses()`
- `find_brand()`

#### Messagerie

- `get_messages()`
- `get_thread()`
- `get_messages_with_user()`

#### Médias et session

- `download_item_images()`
- `refresh_session()`
- `close()`

## Notes de conception

- Les méthodes `iter_*` sont paresseuses et adaptées au monitoring.
- Les méthodes `get_*` retournent généralement une liste complète.
- Les résultats paginés sont dédupliqués par identifiant lorsque cela est possible.
- Les requêtes sont espacées par un throttle global.
- Les erreurs `429` utilisent l’en-tête `Retry-After` lorsqu’il est fourni.
- Les erreurs réseau et les réponses `5xx` sont automatiquement retentées.
- Un seul rafraîchissement CSRF automatique est effectué après un `401` ou un `403`.
- Les données d’initialisation du catalogue sont conservées en cache mémoire.
- Le client peut être utilisé avec `with` afin de fermer proprement la session HTTP.

## Limites connues

- L’API Vinted n’est pas publique et peut modifier ses endpoints ou formats de réponse.
- Certains endpoints peuvent être indisponibles selon le domaine, le compte ou l’adresse IP.
- Les endpoints privés nécessitent un cookie de session valide.
- La librairie est synchrone. Dans une application asynchrone comme `discord.py`, utilisez par exemple `asyncio.to_thread()` pour éviter de bloquer la boucle principale.
- `tqdm` est nécessaire uniquement pour afficher les barres de progression.
- Les mécanismes anti-abus peuvent limiter ou bloquer les requêtes trop fréquentes.

## Pistes d’évolution

- Cache disque des données de référence avec expiration.
- Rotation de proxies.
- Client asynchrone basé sur `httpx.AsyncClient`.
- Métriques sur le nombre de requêtes, les erreurs `429` et la latence.
- Suite de tests avec réponses HTTP simulées.
- Modèles typés dédiés pour les annonces, utilisateurs et évaluations.

## Licence

MIT License
