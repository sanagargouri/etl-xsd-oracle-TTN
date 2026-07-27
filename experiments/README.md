# Prototypes non utilisés en production

Ce dossier contient une ancienne tentative de détection automatique
multi-XSD, développée séparément du pipeline actuellement en production.

## Statut : NON UTILISÉ, NON CONNECTÉ à l'application

Aucun fichier de ce dossier n'est importé par `app.py`, `web/routes.py`,
`web/scheduler_manager.py`, ni par le dashboard. Rien ici n'est appelé
automatiquement -- ces scripts ne s'exécutent que si on les lance
manuellement en ligne de commande.

## Le pipeline réellement en production est ailleurs :

- `document_router.py` (dans `src/`) : détection du type de document
  (TEIF vs DOCUMENT/TCE) par nom de balise racine, avec repli automatique
  (scan de XSD candidats + score de similarité) pour tout futur type de
  document non prévu.
- `batch_loader.py` : orchestre le traitement par lot (détection,
  extraction, création de table, chargement Oracle) pour chaque fichier
  de `data/xml/a_traiter/`.
- `app.py` + `web/scheduler_manager.py` : exécutent ce pipeline
  automatiquement toutes les X minutes, et exposent le dashboard web
  (upload, historique, tables Oracle, bouton "Traiter maintenant").

C'est ce pipeline qui a été testé de bout en bout sur des fichiers réels
(TEIF et tous les types TCE : Z06, Z13, Z19, Z76...), avec 0 erreur.

## Ce que contiennent ces fichiers, pour référence future

- `schema_registry.py` : détection de schéma par VALIDATION XSD STRICTE
  réelle (via la librairie `xmlschema`, XSD 1.1), avec repli tolérant
  (schéma le moins d'erreurs de validation). Approche plus rigoureuse
  que le `document_router.py` actuel sur le papier, mais jamais reliée
  au reste de l'application.
- `process_xml.py` : pipeline complet utilisant `schema_registry.py`,
  avec une logique de nommage de table par préfixe de schéma
  (`apply_schema_naming`) permettant de PARTAGER une table entre deux
  schémas si leur structure (signature de colonnes) est identique.
  Idée intéressante non reprise dans le pipeline actuel.
- `create_invoic_tables.py`, `test_naming.py`, `diagnose_schema.py`,
  `diagnostic_types.py`, `debug_tagmap.py` : scripts de test/diagnostic
  ponctuels liés à cette même expérimentation.

## Pourquoi ce dossier existe plutôt que d'avoir supprimé ces fichiers

Deux approches de détection automatique ont été explorées avant de
converger sur celle utilisée en production (`document_router.py`).
Ce dossier conserve la trace de la première approche -- utile pour
justifier des choix de conception dans un rapport de stage, ou pour
reprendre certaines idées (validation XSD stricte, partage de table par
signature) si le besoin se présente plus tard -- sans risque de confusion
avec le code réellement actif, ni de double exécution accidentelle.

## Si vous voulez réactiver cette approche un jour

Il faudrait : sécuriser les identifiants Oracle en dur (actuellement en
clair dans `process_xml.py`), la connecter à `app.py`/`scheduler_manager.py`
à la place de (ou en complément de) `document_router.py`, et valider sur
des cas réels comme cela a été fait pour le pipeline actuel avant de la
considérer prête pour la production.
