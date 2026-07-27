"""
apply_column_comments.py

Applique les COMMENT ON COLUMN (nom complet d'origine) sur les tables
Oracle DEJA existantes, sans avoir besoin de déposer un fichier XML.

Parse les deux XSD (TEIF et TCE) pour obtenir la structure des colonnes
(avec full_name / xml_path), puis appelle TableGenerator.add_column_comments
sur chaque table -- purement documentaire, ne touche ni structure ni données.

Usage :
    python apply_column_comments.py
"""

import sys
sys.path.append("src")

from xsd_parser import XSDParser
from xsd_parser_tce import XSDParserTCE
from table_generator import TableGenerator

import config


def main():
    generator = TableGenerator(
        username=config.DB_USERNAME,
        password=config.DB_PASSWORD,
        dsn=config.DB_DSN,
    )
    generator.connect()

    try:
        print("=== Schéma TEIF ===")
        parser_teif = XSDParser(config.XSD_PATH_TEIF)
        tables_teif, _ = parser_teif.parse()
        for table in tables_teif:
            if generator.table_exists(table["table_name"]):
                generator.add_column_comments(table)
            else:
                print(f"    Table {table['table_name']} n'existe pas encore -> ignorée")

        print("\n=== Schéma DOCUMENT (TCE) ===")
        parser_tce = XSDParserTCE(config.XSD_PATH_TCE)
        tables_tce, _ = parser_tce.parse()
        for table in tables_tce:
            if generator.table_exists(table["table_name"]):
                generator.add_column_comments(table)
            else:
                print(f"    Table {table['table_name']} n'existe pas encore -> ignorée")

        print("\nTerminé.")

    finally:
        generator.disconnect()


if __name__ == "__main__":
    main()
