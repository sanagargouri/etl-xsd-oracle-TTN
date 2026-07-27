"""
web/schema_info.py

Extrait la correspondance réelle schéma XSD -> tables Oracle, en réutilisant
directement XSDParser / XSDParserTCE (src/) — aucune logique de parsing
dupliquée ici, et aucune connexion Oracle nécessaire : la correspondance
est déterminée au moment du parsing du XSD, pas de l'insertion des données.

Depuis l'ajout de la détection automatique de schéma (TEIF vs DOCUMENT/TCE),
il y a désormais DEUX schémas à résumer, pas un seul -> get_schema_summary()
retourne une LISTE de résumés (un par schéma) au lieu d'un unique dict.
"""

import os

from src.xsd_parser import XSDParser
from src.xsd_parser_tce import XSDParserTCE
import config


def _summarize_one(xsd_path, parser_cls, label):
    """
    Construit le résumé d'un seul schéma XSD : sa table racine et ses
    colonnes, plus le nombre total de tables générées.
    """
    parser = parser_cls(xsd_path)
    tables, tag_map = parser.parse()

    if not tables:
        return None

    root = next((t for t in tables if "parent_table" not in t), tables[0])

    root_columns = []
    for col in root["columns"]:
        is_pk = "GENERATED ALWAYS AS IDENTITY" in col["sql_type"]
        root_columns.append({"name": col["name"], "is_pk": is_pk})

    return {
        "label": label,
        "xsd_filename": os.path.basename(xsd_path),
        "root_table": root["table_name"],
        "root_columns": root_columns,
        "total_tables": len(tables),
        "child_table_count": len(tables) - 1,
    }


def get_schema_summary():
    """
    Retourne une LISTE de résumés (un par schéma connu : TEIF et
    DOCUMENT/TCE), pour affichage sur le dashboard.

    IMPORTANT : cette fonction retournait auparavant un dict unique
    (un seul schéma). Elle retourne maintenant une liste de 0, 1 ou 2
    dicts (un XSD manquant/en erreur est simplement omis plutôt que de
    faire planter tout le dashboard) -> le template doit itérer dessus
    plutôt que d'accéder directement à des clés comme avant.
    """
    summaries = []

    try:
        s = _summarize_one(config.XSD_PATH_TEIF, XSDParser, "Facture TEIF")
        if s:
            summaries.append(s)
    except Exception as e:
        print(f"[schema_info] Erreur lors du résumé du schéma TEIF : {e}")

    try:
        s = _summarize_one(config.XSD_PATH_TCE, XSDParserTCE, "Document TCE (TTN)")
        if s:
            summaries.append(s)
    except Exception as e:
        print(f"[schema_info] Erreur lors du résumé du schéma TCE : {e}")

    return summaries