# Tests de régression — HermesJudgeV2

## Suite maintenue

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v
python -m hermes_judge_v2.cli self-check
```

Résultat de livraison : **34 tests sur 34** et self-check de la wheel installée
en cible isolée réussi. Les scripts de test sous `scripts/` caractérisent les
anciens prototypes et ne constituent pas la suite du produit 2.0.0.

Compatibilité Hermes officielle : 40/40 tests `GoalManager` et 15/15 tests de
gates portables sous Windows. Voir `FINAL_REPORT.md`.
