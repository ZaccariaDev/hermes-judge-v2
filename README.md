# Hermes Judge V2

Contrôleur de goals autonome fondé sur des preuves, conçu pour s’intégrer au
`GoalManager` de Hermes Agent sans créer un second moteur.

## Ce qui est livré

- état V2 durable : SessionDB autoritative + miroir atomique vérifié ;
- demande initiale et métadonnées Discord immuables ;
- protection stricte du workspace ;
- verdicts structurés `CONTINUE`, `REPAIR`,
  `RETRY_DIFFERENT_STRATEGY`, `WAIT_HUMAN`, `BLOCKED_EXTERNAL`,
  `COMPLETE` ;
- refus déterministe des faux `COMPLETE` ;
- détection du footer réel `File-mutation verifier` ;
- reconstruction origine + checkpoint + delta, sans contamination de fil ;
- compaction vérifiée avant changement volontaire de modèle ;
- stress par provider/modèle/fil, cooldown et hystérésis ;
- lease et heartbeat empêchant deux workers simultanés ;
- checkpoint Discord unique, édité, relu et épinglé ;
- renommage idempotent avec cooldown ;
- DM explicitement autorisé, envoyé puis relu, avec classification 50007/50278 ;
- collecte de fichiers et commandes sans shell implicite ;
- bridge minimal vers `hermes_state.SessionDB`.

Les fichiers de `scripts/` sont les prototypes historiques conservés pour
audit. Le produit maintenu se trouve dans `src/hermes_judge_v2/`.

## Validation locale

```bash
python -m unittest discover -s tests -v
PYTHONPATH=src python -m hermes_judge_v2.cli self-check
```

Sous PowerShell :

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v
python -m hermes_judge_v2.cli self-check
```

## Installation dans l’environnement Hermes

```bash
python -m pip install /chemin/vers/HermesJudgeV2
```

Lire ensuite [DEPLOYMENT.md](DEPLOYMENT.md). L’activation est volontairement
séparée de l’installation : le flag est désactivé par défaut et aucun fichier
du runtime n’est remplacé automatiquement.

Pour confier l’installation complète à un nouvel agent après téléversement du
workspace, utiliser `DEPLOYMENT_AGENT_PROMPT.md`, qui contient également la
commande `/goal` courte et la publication contrôlée du patchnote Discord.

## Sécurité de complétion

Le texte du worker n’est jamais une preuve. Un verdict `COMPLETE` est
rétrogradé en `REPAIR` si un critère explicite reste non vérifié, si aucune
preuve vérifiée n’existe ou si le contrat exige la production sans état
`PRODUCTION_VALIDATED`.

## Compatibilité

- Python 3.11 ou supérieur ;
- aucune dépendance d’exécution externe ;
- adaptateur SessionDB limité à `get_meta` / `set_meta` ;
- adaptateur de plateforme défini par protocole, testable sans Discord.
