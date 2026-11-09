# `/fallback off|on|status` — Hermes et Judge V2

## Deux parties indissociables pour une installation avec Judge V2

La commande et sa politique persistante appartiennent au **core Hermes** :
`agent/fallback_policy.py`, `gateway/slash_commands_fallback.py` et leurs
points d'appel. `changes.patch` livre cette extension, ses tests et son
registre, contre la base exacte Hermes
`869228cab4a8276d3b4c78da9d9939670c47bd0f`.

Le paquet Python **Hermes Judge V2** ajoute le pont
`src/hermes_judge_v2/thread_fallback.py` et consulte cette même politique
avant les replis de son juge et les changements de route de son contrôleur.
Installer seulement Judge V2 n'ajoute donc PAS la commande au gateway.
Le patch core ne contient pas les autres extensions locales (auth,
plugins de capture, journalisation ou commandes supplémentaires).

## Comportement

- `/fallback off` interdit les prochains changements automatiques de route
  dans le fil Discord de serveur concerné : worker, auxiliaires, vision,
  compression, repli après erreur d'authentification et contrôleur Judge V2.
- `/fallback status` lit l'état sans le modifier et distingue route demandée
  et worker actuellement en cache. Sans worker, la route effective est inconnue.
- `/fallback on` rétablit la politique héritée ; ce n'est pas un ordre de
  changer immédiatement de fournisseur.
- Aucun changement du modèle courant, du prompt, des outils, de l'historique
  ou de `config.yaml`. Aucun appel en cours n'est interrompu.
- Persistance dans `state.db` du profil courant, avec clé par serveur/fil ;
  pas de partage avec les fils voisins. `/new` ne réactive pas le repli.
- La commande passe par les contrôles d'accès existants du gateway. Ce n'est
  pas une commande utilisateur qui contourne les permissions administrateur.
- Sans core Hermes compatible, Judge V2 fonctionne seul selon sa politique
  habituelle ; il ne prétend pas avoir installé une commande Discord.

La version de l'intégration du juge livrée ici conserve la priorité GPT déjà
présente dans les sources locales : `openai-codex` / `gpt-6.1-sol` par défaut,
paramétrable par `judge_v2.primary_provider` / `judge_v2.primary_model`.
Le repli Nous du juge n'est permis qu'après épuisement explicite de quota,
crédits ou limite d'usage, jamais pour un simple 429 transitoire, timeout ou
JSON invalide. `off` interdit aussi ce repli de quota. Les modèles du
catalogue ne sont pas garantis accessibles : aucun abonnement ni accès réel
n'est validé par les tests. Aucune configuration personnelle n'est distribuée.

## Vérification sans toucher au runtime fourni

Prérequis : Python >=3.11, Git, dépendances de tests/runtime Hermes, dont
`pytest` (utilisé par le fixture du dispatcher). Utiliser l'environnement
Python Hermes, ou un environnement de test séparé.

Depuis la racine du présent dépôt :

```sh
python integrations/hermes-thread-fallback/verify.py --runtime /chemin/vers/hermes-agent
```

Ce script clone localement une copie temporaire, vérifie/applique le patch
**dans cette copie seulement**, puis lance les suites Judge V2, politique
fallback et dispatch réel avec objets simulés. `HERMES_HOME` est temporaire.
Il ne démarre pas de gateway et ne consulte pas de vrais logs utilisateur.
Il ne prouve pas le parcours Discord réel après un redémarrage.

Validation de publication : **45 tests Judge V2 + 27 tests fallback + 1 test
dispatch réussis**. Les réponses fournisseurs et Discord sont simulées.
L'interpréteur de test a signalé SQLite 3.40.1 vulnérable au bug WAL-reset ;
Hermes a utilisé son mode DELETE de protection. Aucune mise à jour du runtime
actif n'a été faite dans le cadre de cette publication.

## Installation contrôlée sur une autre machine

1. Identifier le dépôt et le processus Hermes réels, le profil et le Python
   utilisés. Sauvegarder les fichiers concernés ; préserver les modifications
   locales. Ne copier aucun credential depuis une autre machine.
2. Exécuter le vérificateur ci-dessus. Il valide une copie de la base, **pas la
   compatibilité automatique de l'installation active**.
3. Sur une copie jetable du runtime cible, exécuter `git apply --check` sur
   `changes.patch`. Si cela échoue, examiner le diff et porter les changements
   minimaux : jamais de `reset --hard`, remplacement massif ou application
   forcée pour masquer une divergence.
4. Vérifier que l'autre copie n'a pas déjà la fonctionnalité (`git apply
   --reverse --check` est utile pour reconnaître le patch exact déjà appliqué).
5. Après revue, appliquer les changements au runtime voulu et installer ce
   paquet Judge V2 dans **son** environnement Python. Préserver le fournisseur,
   le modèle et le flag d'activation du juge configurés par l'utilisateur.
6. Un rechargement/redémarrage coordonné du gateway est nécessaire pour du
   code déjà importé. Le demander avant d'interrompre une session active.
7. Tester dans un fil autorisé `off`, `status`, puis `on`, et vérifier qu'un
   autre fil reste inchangé. Distinguer ce test réel des tests simulés.

Le flag `judge_v2.enabled`, les hooks d'activation du juge et ses autres
prérequis ne sont ni activés ni installés implicitement par le patch fallback.
