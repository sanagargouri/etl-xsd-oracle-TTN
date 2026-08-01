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
"""

import os
import sys
import glob

sys.path.append("src")

import config
from src.data_loader import DataLoader
from table_generator import TableGenerator
from xsd_parser import XSDParser
from xsd_parser_tce import XSDParserTCE


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


def list_available_xsd():
    """Liste TEIF + TCE + tout .xsd présent dans le dossier fallback non
    encore utilisé comme TEIF/TCE, avec le nom personnalisé actuel s'il existe.

    Le dédoublonnage se fait sur le NOM DE LA RACINE XML (pas seulement le
    chemin de fichier) : si une copie du XSD TEIF ou DOCUMENT traîne dans
    le dossier fallback (data/xsd/) sous un autre nom de fichier, elle est
    ignorée plutôt que de créer un doublon "AUTO_TEIF"/"AUTO_DOCUMENT" --
    ces racines sont déjà couvertes par les entrées connues.
    """
    known = [
        {"schema_key": "TEIF", "xsd_path": config.XSD_PATH_TEIF},
        {"schema_key": "DOCUMENT", "xsd_path": config.XSD_PATH_TCE},
    ]

    fallback_dir = os.path.dirname(config.XSD_PATH_TCE)
    known_paths = {os.path.abspath(x["xsd_path"]) for x in known}
    known_roots = {"TEIF", "DOCUMENT"}  # racines déjà couvertes par 'known'

    import xml.etree.ElementTree as PyET
    XS = "{http://www.w3.org/2001/XMLSchema}"

    for xsd_path in glob.glob(os.path.join(fallback_dir, "*.xsd")):
        if os.path.abspath(xsd_path) in known_paths:
            continue
        try:
            root = PyET.parse(xsd_path).getroot()
            top = root.find(f"{XS}element")
            root_name = top.get("name") if top is not None else None
            if not root_name:
                continue
            if root_name in known_roots:
                # Copie redondante d'un XSD déjà connu -> on l'ignore pour
                # éviter un doublon confus dans la liste.
                continue
            known.append({
                "schema_key": f"AUTO_{root_name}",
                "xsd_path": xsd_path,
            })
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
        entry["xsd_filename"] = os.path.basename(entry["xsd_path"])
        entry["custom_name"] = custom_names.get(entry["schema_key"])
        entry["label"] = _KNOWN_LABELS.get(
            entry["schema_key"],
            entry["schema_key"].replace("AUTO_", "").title(),
        )

    return known


def _parser_for(xsd_path):
    import xml.etree.ElementTree as PyET
    XS = "{http://www.w3.org/2001/XMLSchema}"
    root = PyET.parse(xsd_path).getroot()
    has_named = root.find(f".//{XS}complexType[@name]") is not None
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


def create_tables_for_schema(schema_key):
    """
    Crée les tables Oracle manquantes pour ce schéma XSD, en respectant
    le nom personnalisé déjà enregistré pour lui s'il y en a un (ne
    RENOMME jamais une table existante -- pour ça, voir rename_schema_root).
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