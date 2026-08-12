"""
web/schema_manager.py

Page "Gérer les schémas" : liste les XSD connus (TEIF, TCE) et ceux
détectés en fallback dans data/xsd/, et permet de déclencher manuellement
la création de leurs tables Oracle, avec un nom personnalisé optionnel
pour la table racine.

Renommer un schéma déjà configuré déclenche un vrai ALTER TABLE RENAME
(pas une recréation) : les données existantes et les FK des tables
enfants sont préservées automatiquement par Oracle (les contraintes sont
suivies en interne par OID, pas par nom).

--- Ajouts ---
1. list_available_xsd() n'ignore plus les XSD dont la racine est déjà
   connue (TEIF/DOCUMENT) : ils sont affichés avec un badge "doublon"
   calculé par similarité de structure, plutôt que d'être absorbés
   silencieusement (cf. cas "tce - Copie.xsd").
2. check_schema_changed() détecte si le XSD officiel (TEIF ou TCE) a été
   modifié depuis le dernier traitement, via un hash de structure stocké
   dans ETL_SCHEMA_CONFIG.xsd_structure_hash.
"""

import os
import sys
import glob
import hashlib
import xml.etree.ElementTree as PyET

sys.path.append("src")

import config
from src.data_loader import DataLoader
from table_generator import TableGenerator
from xsd_parser import XSDParser
from xsd_parser_tce import XSDParserTCE

XS_NS = "{http://www.w3.org/2001/XMLSchema}"


def _connect_loader():
    loader = DataLoader(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN,
    )
    loader.connect()
    return loader


_KNOWN_LABELS = {
    "TEIF": "Facture TEIF",
    "DOCUMENT": "Document TCE (TTN)",
}


# -----------------------------------------------------------------------
# Signature structurelle (ensemble des balises + hash SHA256)
# -----------------------------------------------------------------------
def _xsd_structure_signature(xsd_path):
    """Retourne (tag_set, sha256_hex) pour un XSD.

    tag_set sert au score de similarité (Jaccard) entre XSD.
    sha256_hex sert de preuve d'audit / détection de changement exact.
    """
    root = PyET.parse(xsd_path).getroot()
    tags = {e.get("name") for e in root.findall(f".//{XS_NS}element") if e.get("name")}
    signature_text = "|".join(sorted(tags))
    sha256_hex = hashlib.sha256(signature_text.encode("utf-8")).hexdigest()
    return tags, sha256_hex


# -----------------------------------------------------------------------
# Liste des XSD disponibles (avec détection de doublons)
# -----------------------------------------------------------------------
def list_available_xsd():
    """Liste TEIF + TCE + tout .xsd présent dans le dossier fallback,
    SANS EN CACHER AUCUN.

    Un fichier dont la racine ou la structure ressemble fortement à un
    XSD déjà connu (TEIF/DOCUMENT) est affiché avec un badge
    'duplicate_of' et un score de similarité, plutôt que d'être ignoré --
    un fichier déposé doit toujours être visible quelque part, jamais
    silencieusement absorbé (cf. cas "tce - Copie.xsd").
    """
    known = [
        {"schema_key": "TEIF", "xsd_path": config.XSD_PATH_TEIF},
        {"schema_key": "DOCUMENT", "xsd_path": config.XSD_PATH_TCE},
    ]

    fallback_dir = os.path.dirname(config.XSD_PATH_TCE)
    known_paths = {os.path.abspath(x["xsd_path"]) for x in known}

    # Signatures des schémas de référence, pour détecter les doublons
    reference_sigs = {}
    for entry in known:
        try:
            tags, sha = _xsd_structure_signature(entry["xsd_path"])
            reference_sigs[entry["schema_key"]] = (tags, sha)
        except Exception:
            reference_sigs[entry["schema_key"]] = (set(), None)

    for xsd_path in glob.glob(os.path.join(fallback_dir, "*.xsd")):
        if os.path.abspath(xsd_path) in known_paths:
            continue
        try:
            root = PyET.parse(xsd_path).getroot()
            top = root.find(f"{XS_NS}element")
            root_name = top.get("name") if top is not None else None

            if not root_name:
                # Pas d'élément racine de haut niveau détectable -- très
                # probablement un fichier de définitions de types
                # (complexType/simpleType) importé par un autre XSD via
                # <xs:import>/<xs:include>, pas un schéma de document à
                # part entière (ex: isocbctypes_v1.1.xsd, oecdcbctypes_v5.0.xsd
                # qui complètent CbcXML_v2.0.xsd). On l'affiche quand même,
                # avec un statut explicite, plutôt que de le faire
                # disparaître silencieusement.
                known.append({
                    "schema_key": None,
                    "xsd_path": xsd_path,
                    "duplicate_of": None,
                    "similarity_score": None,
                    "no_root_detected": True,
                })
                continue

            tags, sha = _xsd_structure_signature(xsd_path)

            # Cherche si ce fichier est un doublon exact ou quasi-exact
            # d'un schéma déjà connu.
            duplicate_of = None
            best_score = 0.0
            for ref_key, (ref_tags, ref_sha) in reference_sigs.items():
                if ref_sha is not None and sha == ref_sha:
                    duplicate_of = ref_key
                    best_score = 1.0
                    break
                if ref_tags and tags:
                    score = len(tags & ref_tags) / len(tags | ref_tags)
                    if score > best_score:
                        best_score = score
                        if score >= 0.95:
                            duplicate_of = ref_key

            entry = {
                "schema_key": f"AUTO_{root_name}",
                "xsd_path": xsd_path,
                "duplicate_of": duplicate_of,
                "similarity_score": round(best_score, 2) if duplicate_of else None,
            }
            known.append(entry)
        except Exception:
            continue

    loader = _connect_loader()
    try:
        loader.cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = 'ETL_SCHEMA_CONFIG'"
        )
        has_config = loader.cursor.fetchone()[0] > 0
        custom_names = {}
        if has_config:
            loader.cursor.execute("SELECT schema_key, custom_root_name FROM ETL_SCHEMA_CONFIG")
            custom_names = {row[0]: row[1] for row in loader.cursor.fetchall()}
    finally:
        loader.disconnect()

    for entry in known:
        entry.setdefault("duplicate_of", None)
        entry.setdefault("similarity_score", None)
        entry.setdefault("no_root_detected", False)
        entry["xsd_filename"] = os.path.basename(entry["xsd_path"])
        entry["custom_name"] = custom_names.get(entry["schema_key"]) if entry["schema_key"] else None

        if entry["no_root_detected"]:
            entry["label"] = "Fichier de types (pas de document racine)"
            entry["change_warning"] = None
            continue

        if entry["duplicate_of"]:
            ref_label = _KNOWN_LABELS.get(entry["duplicate_of"], entry["duplicate_of"])
            entry["label"] = f"Copie de {ref_label}"
        else:
            entry["label"] = _KNOWN_LABELS.get(
                entry["schema_key"],
                entry["schema_key"].replace("AUTO_", "").title(),
            )

        # Détection de changement de version -- uniquement pertinente
        # pour les schémas connus (TEIF/DOCUMENT), pas pour les doublons
        # ou les AUTO_* qui n'ont pas de "version de référence" à suivre.
        if entry["schema_key"] in ("TEIF", "DOCUMENT"):
            try:
                entry["change_warning"] = check_schema_changed(
                    entry["schema_key"], entry["xsd_path"]
                )
            except Exception as e:
                entry["change_warning"] = None
                print(f"[schema_manager] Impossible de vérifier le hash de "
                      f"{entry['schema_key']} : {e}")
        else:
            entry["change_warning"] = None

    return known


def _parser_for(xsd_path):
    root = PyET.parse(xsd_path).getroot()
    has_named = root.find(f".//{XS_NS}complexType[@name]") is not None
    return XSDParser if has_named else XSDParserTCE


def _get_current_root_name(generator, schema_key):
    """Retourne le nom de table racine actuellement enregistré en config
    pour ce schéma (None si jamais personnalisé)."""
    generator.cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name = 'ETL_SCHEMA_CONFIG'"
    )
    if generator.cursor.fetchone()[0] == 0:
        return None
    generator.cursor.execute(
        "SELECT custom_root_name FROM ETL_SCHEMA_CONFIG WHERE schema_key = :1",
        [schema_key],
    )
    row = generator.cursor.fetchone()
    return row[0] if row else None


def _validate_name(name):
    name = name.strip().upper().replace(" ", "_")
    if not name.isidentifier() or len(name) > 30:
        raise ValueError(
            "Nom de table invalide (30 caractères max, lettres/chiffres/underscore, "
            "doit commencer par une lettre)."
        )
    return name


def _save_custom_name(generator, schema_key, custom_name, xsd_filename):
    """Enregistre le nom personnalisé en config (binds nommés -- cf. note
    dans table_generator.register_table_schema sur DPY-4009 avec MERGE)."""
    generator.cursor.execute("""
        MERGE INTO ETL_SCHEMA_CONFIG t
        USING (SELECT :schema_key AS schema_key FROM dual) src
        ON (t.schema_key = src.schema_key)
        WHEN MATCHED THEN
            UPDATE SET custom_root_name = :custom_root_name, updated_at = SYSTIMESTAMP
        WHEN NOT MATCHED THEN
            INSERT (schema_key, custom_root_name, xsd_filename)
            VALUES (:schema_key, :custom_root_name, :xsd_filename)
    """, schema_key=schema_key, custom_root_name=custom_name, xsd_filename=xsd_filename)
    generator.connection.commit()


def _save_hash(generator, schema_key, xsd_hash, xsd_filename=None, custom_root_name=None):
    """Enregistre/rafraîchit le hash de structure d'un schéma connu.

    Si aucune ligne n'existe encore pour ce schema_key, custom_root_name
    doit être fourni (contrainte NOT NULL) -- on utilise schema_key lui
    même comme valeur par défaut, purement technique, écrasée dès qu'un
    vrai renommage est fait via rename_schema_root.
    """
    generator.cursor.execute("""
        MERGE INTO ETL_SCHEMA_CONFIG t
        USING (SELECT :schema_key AS schema_key FROM dual) src
        ON (t.schema_key = src.schema_key)
        WHEN MATCHED THEN
            UPDATE SET xsd_structure_hash = :xsd_hash, updated_at = SYSTIMESTAMP
        WHEN NOT MATCHED THEN
            INSERT (schema_key, custom_root_name, xsd_filename, xsd_structure_hash)
            VALUES (:schema_key, :custom_root_name, :xsd_filename, :xsd_hash)
    """,
        schema_key=schema_key,
        xsd_hash=xsd_hash,
        custom_root_name=custom_root_name or schema_key,
        xsd_filename=xsd_filename,
    )
    generator.connection.commit()


# -----------------------------------------------------------------------
# Détection de changement de version d'un XSD connu (Problème A)
# -----------------------------------------------------------------------
def check_schema_changed(schema_key, xsd_path):
    """Compare le hash structurel actuel du XSD à celui enregistré la
    dernière fois dans ETL_SCHEMA_CONFIG.xsd_structure_hash.

    Retourne :
        None                si rien n'a changé (ou 1er passage -- on
                             enregistre alors le hash sans rien signaler)
        str (message)       si le XSD a été modifié depuis la dernière
                             fois qu'on l'a vu
    """
    _, current_hash = _xsd_structure_signature(xsd_path)
    if current_hash is None:
        return None

    generator = TableGenerator(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN,
    )
    try:
        generator.connect()
        generator.ensure_metadata_tables()

        generator.cursor.execute(
            "SELECT xsd_structure_hash FROM ETL_SCHEMA_CONFIG WHERE schema_key = :1",
            [schema_key],
        )
        row = generator.cursor.fetchone()
        stored_hash = row[0] if row else None

        if stored_hash is None:
            # Première fois qu'on voit ce schéma -> on enregistre le hash
            # de référence, rien à signaler à l'utilisateur.
            _save_hash(generator, schema_key, current_hash)
            return None

        if stored_hash != current_hash:
            return (
                f"⚠️ Le XSD de {schema_key} a changé depuis le dernier traitement "
                f"(structure différente du hash enregistré). Vérifiez si de "
                f"nouvelles colonnes doivent être ajoutées aux tables Oracle "
                f"avant de continuer à traiter des fichiers de ce type."
            )
        return None
    finally:
        generator.disconnect()


def acknowledge_schema_change(schema_key, xsd_path):
    """Appelée quand un opérateur valide manuellement un changement de XSD
    détecté (bouton 'Valider la nouvelle structure' dans /schemas) :
    met à jour le hash enregistré pour que l'alerte disparaisse."""
    _, current_hash = _xsd_structure_signature(xsd_path)
    generator = TableGenerator(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN,
    )
    try:
        generator.connect()
        generator.ensure_metadata_tables()
        current_name = _get_current_root_name(generator, schema_key)
        _save_hash(
            generator, schema_key, current_hash,
            xsd_filename=os.path.basename(xsd_path),
            custom_root_name=current_name,
        )
    finally:
        generator.disconnect()


# -----------------------------------------------------------------------
# Création des tables pour un schéma
# -----------------------------------------------------------------------
def create_tables_for_schema(schema_key):
    """
    Crée les tables Oracle manquantes pour ce schéma XSD, en respectant
    le nom personnalisé déjà enregistré pour lui s'il y en a un (ne
    RENOMME jamais une table existante -- pour ça, voir rename_schema_root).

    Si la table existe déjà et que le XSD a changé depuis, les nouvelles
    colonnes sont ajoutées automatiquement via ALTER TABLE ADD COLUMN
    (cf. TableGenerator.sync_missing_columns), plutôt que d'être ignorées.
    """
    xsd_list = {x["schema_key"]: x["xsd_path"] for x in list_available_xsd()}
    xsd_path = xsd_list.get(schema_key)
    if not xsd_path:
        raise ValueError(f"Schéma inconnu : {schema_key}")

    parser_cls = _parser_for(xsd_path)
    parser = parser_cls(xsd_path)
    tables, tag_map = parser.parse()

    default_root = next((t for t in tables if "parent_table" not in t), None)
    if default_root is None:
        raise ValueError("Impossible de trouver la table racine de ce XSD.")
    default_root_name = default_root["table_name"]

    generator = TableGenerator(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN,
    )
    try:
        generator.connect()
        generator.ensure_metadata_tables()

        # Réutilise le nom déjà personnalisé pour ce schéma s'il existe,
        # pour rester cohérent avec un renommage fait précédemment.
        current_name = _get_current_root_name(generator, schema_key)
        target_name = current_name or default_root_name

        if target_name != default_root_name:
            for table in tables:
                if table.get("parent_table") == default_root_name:
                    table["parent_table"] = target_name
                    for col in table["columns"]:
                        if f"REFERENCES {default_root_name}(" in col.get("sql_type", ""):
                            col["sql_type"] = col["sql_type"].replace(
                                f"REFERENCES {default_root_name}(", f"REFERENCES {target_name}("
                            )
            default_root["table_name"] = target_name
            if default_root_name in tag_map:
                tag_map[target_name] = tag_map.pop(default_root_name)

        generator.create_all_tables(
            tables, drop_if_exists=False,
            schema_key=schema_key, xsd_filename=os.path.basename(xsd_path),
        )

        # Le XSD vient d'être traité avec succès -> on rafraîchit le hash
        # de référence, que ce soit un premier passage ou une confirmation
        # après une alerte de changement.
        _, current_hash = _xsd_structure_signature(xsd_path)
        _save_hash(
            generator, schema_key, current_hash,
            xsd_filename=os.path.basename(xsd_path),
            custom_root_name=target_name,
        )
    finally:
        generator.disconnect()


def list_pending_suggestions():
    """Retourne les suggestions IA en attente de validation
    (ETL_SCHEMA_SUGGESTIONS.status = 'pending'), les plus récentes
    d'abord. Si une suggestion propose une ébauche de structure
    (cas is_new_type=True côté IA), elle est désérialisée depuis JSON
    dans les clés 'proposed_root_name' et 'proposed_columns'."""
    import json

    generator = TableGenerator(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN,
    )
    try:
        generator.connect()
        generator.ensure_metadata_tables()
        generator.cursor.execute("""
            SELECT id, source_file, suggested_schema_key, confidence_score,
                   justification, created_at, proposed_structure, detected_root_tag
            FROM ETL_SCHEMA_SUGGESTIONS
            WHERE status = 'pending'
            ORDER BY created_at DESC
        """)
        rows = generator.cursor.fetchall()

        results = []
        for r in rows:
            proposed_structure_raw = r[6]
            if hasattr(proposed_structure_raw, "read"):
                proposed_structure_raw = proposed_structure_raw.read()

            proposed_root_name = None
            proposed_columns = []
            if proposed_structure_raw:
                try:
                    parsed = json.loads(proposed_structure_raw)
                    proposed_root_name = parsed.get("proposed_root_name")
                    proposed_columns = parsed.get("proposed_columns", [])
                except Exception:
                    pass

            results.append({
                "id": r[0],
                "source_file": r[1],
                "suggested_schema_key": r[2],
                "confidence_score": r[3],
                "justification": r[4],
                "created_at": r[5],
                "proposed_root_name": proposed_root_name,
                "proposed_columns": proposed_columns,
                "detected_root_tag": r[7],
            })
        return results
    finally:
        generator.disconnect()


def validate_suggestion(suggestion_id):
    """Marque une suggestion comme validée.

    Si la suggestion proposait un rapprochement vers un schéma connu
    (suggested_schema_key non nul), un alias root_tag -> schema_key est
    créé dans ETL_ROOT_ALIASES : tous les PROCHAINS fichiers portant
    cette racine seront reconnus automatiquement, sans repasser par
    l'IA (cf. document_router._check_root_alias).

    Le fichier source d'origine (déplacé vers erreurs/ au moment du
    rejet initial) est aussi redéplacé automatiquement vers a_traiter/
    pour être retraité au prochain passage -- avec l'alias tout juste
    créé, il sera cette fois reconnu et traité normalement.

    Si la suggestion était "type nouveau" (suggested_schema_key = None),
    aucun alias n'est créé et le fichier n'est pas redéplacé -- il n'y a
    toujours aucun XSD capable de le traiter.
    """
    import os
    import config as cfg

    generator = TableGenerator(
        username=cfg.DB_USERNAME, password=cfg.DB_PASSWORD, dsn=cfg.DB_DSN,
    )
    try:
        generator.connect()
        generator.ensure_metadata_tables()

        generator.cursor.execute("""
            SELECT source_file, detected_root_tag, suggested_schema_key
            FROM ETL_SCHEMA_SUGGESTIONS WHERE id = :1
        """, [suggestion_id])
        row = generator.cursor.fetchone()
        if row is None:
            raise ValueError(f"Suggestion {suggestion_id} introuvable.")
        source_file, root_tag, suggested_schema_key = row

        generator.cursor.execute("""
            UPDATE ETL_SCHEMA_SUGGESTIONS
            SET status = 'validated', validated_at = SYSTIMESTAMP
            WHERE id = :1
        """, [suggestion_id])

        if suggested_schema_key and root_tag:
            generator.cursor.execute("""
                MERGE INTO ETL_ROOT_ALIASES t
                USING (SELECT :root_tag AS root_tag FROM dual) src
                ON (t.root_tag = src.root_tag)
                WHEN MATCHED THEN
                    UPDATE SET schema_key = :schema_key, source_suggestion_id = :suggestion_id
                WHEN NOT MATCHED THEN
                    INSERT (root_tag, schema_key, source_suggestion_id)
                    VALUES (:root_tag, :schema_key, :suggestion_id)
            """, root_tag=root_tag, schema_key=suggested_schema_key, suggestion_id=suggestion_id)

        generator.connection.commit()

        # Redéplace le fichier source vers a_traiter/ pour retraitement --
        # uniquement si un alias a bien été créé (sinon, aucun schéma
        # n'est capable de le traiter de toute façon).
        moved = False
        if suggested_schema_key and source_file:
            erreurs_dir = os.path.join(
                os.path.dirname(cfg.XML_A_TRAITER.rstrip("\\/")), "erreurs"
            )
            source_path = os.path.join(erreurs_dir, source_file)
            destination_path = os.path.join(cfg.XML_A_TRAITER, source_file)
            if os.path.exists(source_path) and not os.path.exists(destination_path):
                os.replace(source_path, destination_path)
                moved = True

        return {"alias_created": bool(suggested_schema_key and root_tag), "file_requeued": moved}
    finally:
        generator.disconnect()


def reject_suggestion(suggestion_id):
    """Marque une suggestion comme rejetée (l'opérateur juge que le
    rapprochement proposé par l'IA n'est pas pertinent)."""
    generator = TableGenerator(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN,
    )
    try:
        generator.connect()
        generator.cursor.execute("""
            UPDATE ETL_SCHEMA_SUGGESTIONS
            SET status = 'rejected', validated_at = SYSTIMESTAMP
            WHERE id = :1
        """, [suggestion_id])
        generator.connection.commit()
    finally:
        generator.disconnect()


def rename_schema_root(schema_key, new_name):
    """
    Renomme la table racine EXISTANTE d'un schéma déjà créé (ALTER TABLE
    RENAME -- préserve les données et les FK des tables enfants, suivies
    par Oracle en interne par OID, pas par nom). Ne crée aucune table :
    échoue explicitement si la table actuelle n'existe pas encore
    (il faut d'abord cliquer "Créer les tables").
    """
    if not new_name or not new_name.strip():
        raise ValueError("Le nouveau nom ne peut pas être vide.")
    new_name = _validate_name(new_name)

    xsd_list = {x["schema_key"]: x["xsd_path"] for x in list_available_xsd()}
    xsd_path = xsd_list.get(schema_key)
    if not xsd_path:
        raise ValueError(f"Schéma inconnu : {schema_key}")

    parser_cls = _parser_for(xsd_path)
    parser = parser_cls(xsd_path)
    tables, tag_map = parser.parse()
    default_root = next((t for t in tables if "parent_table" not in t), None)
    default_root_name = default_root["table_name"] if default_root else None

    generator = TableGenerator(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN,
    )
    try:
        generator.connect()
        generator.ensure_metadata_tables()

        current_name = _get_current_root_name(generator, schema_key) or default_root_name
        if current_name is None:
            raise ValueError("Impossible de déterminer la table racine actuelle de ce schéma.")

        if current_name == new_name:
            raise ValueError(f"La table s'appelle déjà {new_name}, rien à renommer.")

        if not generator.table_exists(current_name):
            raise ValueError(
                f"La table {current_name} n'existe pas encore dans Oracle. "
                f"Clique d'abord sur « Créer les tables » avant de renommer."
            )

        if generator.table_exists(new_name):
            raise ValueError(
                f"Impossible de renommer {current_name} en {new_name} : "
                f"une table {new_name} existe déjà. Choisis un autre nom."
            )

        print(f"[schema_manager] Renommage {current_name} -> {new_name}")
        generator.cursor.execute(f"ALTER TABLE {current_name} RENAME TO {new_name}")
        generator.connection.commit()

        # Met à jour la métadonnée (clé primaire = table_name donc UPDATE,
        # pas MERGE, pour changer la clé elle-même)
        generator.cursor.execute(
            "UPDATE ETL_SCHEMA_TABLES SET table_name = :new_name WHERE table_name = :old_name",
            new_name=new_name, old_name=current_name,
        )
        generator.connection.commit()

        _save_custom_name(generator, schema_key, new_name, os.path.basename(xsd_path))
    finally:
        generator.disconnect()