# Vintify — Bot Discord d'alertes Vinted

Bot Discord complet pour **automatiser la recherche d'annonces** et recevoir
des **alertes en temps réel** (nouvelles annonces, baisses de prix, ventes).

Il s'appuie sur [`vinted_scraper.py`](../vinted_scraper.py) (à la racine du
dépôt) en n'utilisant **que ses endpoints publics** : aucune session Vinted,
aucun token, **pas d'offres ni d'achat**.

> ⚠️ L'API Vinted est privée et non documentée. Utilisez ce bot avec
> modération (intervalles ≥ 30 s) et dans le respect des conditions
> d'utilisation de Vinted.

---

## Fonctionnalités

| Commande | Description |
|---|---|
| `/veille ajouter <requete>` | Surveille une recherche (texte **ou URL Vinted**) : chaque **nouvelle annonce** est signalée avec photo, prix, taille, vendeur et lien. Options : `prix_min`, `prix_max`, `marque` (autocomplétion), `intervalle`, `canal`, `mention` (`@here`, `everyone` ou un rôle). |
| `/veille liste` | Toutes les veilles et articles suivis (état, fréquence, cible, alertes envoyées, erreurs). |
| `/veille retirer <id>` | Supprime une veille. |
| `/veille pause <id>` / `/veille reprendre <id>` | Met en pause / relance une veille. |
| `/suivi ajouter <url>` | Suit un **article précis** : alerte sur **baisse de prix**, **vente/retrait**, ou retour en stock. |
| `/suivi retirer <id>` | Arrête un suivi. |
| `/recherche` | Recherche ponctuelle (résultats récents, avec photo et lien). |
| `/stats` | Uptime, veilles actives, recherches, alertes envoyées, erreurs. |
| `/aide` | Récapitulatif des commandes. |

Détails :

- **Aucun token de session Vinted** : bootstrap anonyme automatique
  (cookies + CSRF gérés par `vinted_scraper`), retries et backoff intégrés.
- **Déduplication persistante** : chaque annonce n'est signalée qu'une fois,
  même après redémarrage (historique d'ids dans `state.json`).
- **Premier scan immédiat** à la création d'une veille (rattrapage), puis
  uniquement les nouveautés.
- **Cible d'alerte flexible** : salon choisi, salon courant ou MP ; mention
  optionnelle (`@here`, `@everyone`, nom de rôle).
- **Suivi d'articles** : comparaison de prix à chaque cycle, alerte unique
  par événement, désactivation automatique si l'annonce disparaît.
- **Persistance JSON atomique** (écriture temp + `os.replace`) : compatible
  avec un arrêt brutal.
- **Modes de test sans Discord** : `--search` et `--selftest`.

## Arborescence

```text
discord_bot/
├── bot.py                 # point d'entrée (Discord + CLI de test)
├── vintify_bot/
│   ├── config.py          # configuration par variables d'environnement
│   ├── storage.py         # persistance JSON (veilles, suivis, stats)
│   ├── vinted.py          # service Vinted asynchrone (to_thread + verrou)
│   ├── embeds.py          # construction des embeds français
│   ├── poller.py          # moteur de surveillance (veilles + suivis)
│   └── cogs.py            # commandes slash
├── requirements.txt
├── Procfile               # worker: python bot.py
└── .env.example
```

---

## Démarrage rapide (local)

```bash
# depuis la racine du dépôt
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate sur Linux/macOS)
pip install -r discord_bot\requirements.txt

cd discord_bot
copy .env.example .env            # puis renseignez DISCORD_TOKEN
python bot.py
```

Sans token (test rapide du scraping) :

```bash
python bot.py --search "nike tn" --price-to 60
python bot.py --selftest
```

## Créer le bot Discord

1. Rendez-vous sur <https://discord.com/developers/applications> → **New Application**.
2. Onglet **Bot** → **Reset Token** → copiez le token (variable `DISCORD_TOKEN`).
3. Onglet **OAuth2 → URL Generator** :
   - scope **`bot`** + **`applications.commands`** ;
   - permissions : **View Channels**, **Send Messages**, **Embed Links**
     (et **Mention Everyone** si vous utilisez `@here`/`@everyone`) ;
   - ouvrez l'URL générée pour inviter le bot sur votre serveur.
4. Lancez le bot : les commandes slash apparaissent après la synchronisation
   (quelques secondes après le démarrage).

---

## Déploiement sur Railway

1. **Poussez le dépôt sur GitHub** (le bot vit dans `discord_bot/`, la racine
   du dépôt contient `vinted_scraper.py` et `railway.json`).
2. Dans Railway : **New Project → Deploy from GitHub repo** → sélectionnez le
   dépôt. `railway.json` est détecté automatiquement :
   - `rootDirectory: discord_bot` → build Nixpacks Python ;
   - `startCommand: python bot.py` (équivalent au `Procfile`).
3. **Variables d'environnement** (Railway → Variables) :
   - `DISCORD_TOKEN` : le token du bot (obligatoire) ;
   - `VINTED_DOMAIN` : `fr` par défaut (optionnel) ;
   - `LOG_LEVEL`, `VEILLE_INTERVALLE_DEFAUT`, `VEILLE_PAGE_SIZE`, … (optionnels,
     voir `.env.example`).
4. **Volume persistant** (recommandé) : Railway → onglet **Volumes** → créez
   un volume monté sur **`/data`**, puis définissez la variable
   **`DATA_DIR=/data`**. Sans volume, les veilles et l'historique d'annonces
   sont perdus à chaque redéploiement.
5. **Déployez.** Vérifiez dans les logs l'apparition de :
   `Commandes slash synchronisées : N`.

Redéploiements et crashs : Railway relance automatiquement le worker
(`restartPolicyType: ON_FAILURE`).

### Notes Railway

- Le `Procfile` est un filet de sécurité : si vous le supprimez, le
  `startCommand` de `railway.json` prend le relais.
- Le plan gratuit suffit pour un usage personnel (une poignée de veilles).
- Si vous déployez **uniquement** le dossier `discord_bot/` (sans la racine),
  copiez `vinted_scraper.py` à l'intérieur : `bot.py` l'importe depuis la
  racine du dépôt par défaut.

---

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `DISCORD_TOKEN` | — | Token du bot Discord (**obligatoire**) |
| `VINTED_DOMAIN` | `fr` | Domaine Vinted (`fr`, `be`, `de`, `es`, `it`, `pl`, `co.uk`, `com`, …) |
| `DATA_DIR` | `data` | Répertoire de persistance (volume Railway → `/data`) |
| `VEILLE_INTERVALLE_DEFAUT` | `60` | Intervalle par défaut des veilles (s, min 30) |
| `SUIVI_INTERVALLE_DEFAUT` | `300` | Intervalle par défaut des suivis (s, min 30) |
| `VEILLE_PAGE_SIZE` | `24` | Annonces lues par cycle (max 96) |
| `VEILLE_HISTORIQUE_MAX` | `1500` | Ids vus conservés par veille |
| `RECHERCHE_MAX_AFFICHEE` | `5` | Résultats max d'une `/recherche` |
| `VEILLE_SCAN_INITIAL` | `1` | Scan immédiat à la création d'une veille |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Limites et bonnes pratiques

- L'API Vinted peut changer ou bloquer selon l'adresse IP : le bot réessaie
  automatiquement (retries + backoff, rafraîchissement CSRF).
- Ne créez pas des dizaines de veilles à 30 s : restez raisonnable
  (intervalle ≥ 60 s recommandé, total de requêtes modéré).
- Les prix affichés proviennent de l'API : les frais d'achat peuvent varier.
- Le suivi d'article détecte les changements de prix entre deux cycles :
  une baisse très brève peut passer inaperçue selon l'intervalle choisi.
- **Marques** : Vinted ne permet plus de chercher une marque par nom sans
  session. Seules les marques populaires (Nike, adidas, H&M, Levi's…) sont
  résolubles par nom ; pour les autres, appliquez le filtre marque sur
  vinted.fr puis collez l'URL obtenue (elle contient `brand_ids[]=…`).
- **Suivi d'articles** : si l'API de détail est bloquée, le prix et la
  disponibilité sont lus sur la page publique de l'article (JSON-LD) —
  mêmes valeurs, mais taille/vendeur ne sont alors pas disponibles.
- **Test sans bot** : `python bot.py --search "…"` (recherche live) et
  `python bot.py --selftest` (env + persistance + recherche).
  `python _integration_test.py` (dans `discord_bot/`) valide le moteur de
  polling (déduplication, baisse de prix, annonce introuvable) avec du
  réseau réel.
