"""
web/schema_info.py

Extrait la correspondance réelle schéma XSD -> tables Oracle, en réutilisant
directement XSDParser (src/xsd_parser.py) — aucune logique de parsing
dupliquée ici, et aucune connexion Oracle nécessaire : la correspondance
est déterminée au moment du parsing du XSD, pas de l'insertion des données.
"""

import os

from src.xsd_parser import XSDParser
import config


def get_schema_summary():
    """
    Retourne un résumé de la correspondance schéma -> table, pour
    affichage sur le dashboard : la table racine et ses colonnes,
    plus le nombre total de tables générées par le XSD complet.

    Volontairement limité à la table racine (pas les 15 tables) :
    la vue complète est sur /tables-oracle, pour éviter la redondance.
    """
    parser = XSDParser(config.XSD_PATH)
    tables, tag_map = parser.parse()

    if not tables:
        return None

    # La table racine est celle qui n'a pas de parent_table (ex: TEIF)
    root = next((t for t in tables if "parent_table" not in t), tables[0])

    root_columns = []
    for col in root["columns"]:
        is_pk = "GENERATED ALWAYS AS IDENTITY" in col["sql_type"]
        root_columns.append({"name": col["name"], "is_pk": is_pk})

    return {
        "xsd_filename": os.path.basename(config.XSD_PATH),
        "root_table": root["table_name"],
        "root_columns": root_columns,
        "total_tables": len(tables),
        "child_table_count": len(tables) - 1,
    }