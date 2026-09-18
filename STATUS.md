# État vérifié

## Produit local

- État persistant et miroir : **implémentés**
- Judge déterministe et validation de sortie : **implémentés**
- Contexte, checkpoint et compaction : **implémentés**
- Router et stress : **implémentés**
- Heartbeat/lease : **implémentés**
- Contrat plateforme Discord : **implémenté**
- Bridge SessionDB : **implémenté**
- Suite locale : **34 tests sur 34**
- Régression `GoalManager` upstream : **40 sur 40**
- Régression gates portable : **15 sur 15** (3 scénarios POSIX exclus sous Windows)
- Wheel installée dans un répertoire isolé : **self-check réussi**
- SHA-256 wheel : `2DCB843CE055E8BF3E3E4708AA6B1CC43A660123363FE42ED776A7A24FE933C8`

## Niveau de livraison

`INTEGRATED_NOT_DEPLOYED`

Le paquet est prêt à être installé et branché. La copie locale ne contient ni
le token, ni la base SessionDB de production, ni le processus gateway actif ;
elle ne peut donc honnêtement pas porter le statut
`PRODUCTION_VALIDATED`.

## Interdiction

Ne jamais recopier un `goals.py` complet entre
`/usr/local/lib/hermes-agent` et `dist-packages`. Installer le paquet dans
le même interpréteur que la gateway, puis appliquer seulement les hooks
documentés.
