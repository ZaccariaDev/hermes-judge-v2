# Prochaine étape — canary réel

Le développement local 2.0.0 est terminé. La prochaine action n’est plus une
phase de code générale : appliquer `integration/hermes-agent-main.patch` sur
le checkout réellement exécuté par la gateway, installer la wheel, laisser
`judge_v2.enabled: false` pour la régression, puis activer un seul fil canary.

Suivre exactement `DEPLOYMENT.md`. Ne déclarer `PRODUCTION_VALIDATED` qu’après
les dix vérifications réelles qui y sont listées.
