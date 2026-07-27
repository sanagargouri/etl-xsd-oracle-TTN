# process_xml.py
#
# Pipeline complet et automatique multi-XSD (PROTOTYPE NON UTILISE EN
# PRODUCTION -- voir experiments/README.md) :
# 1. Detecte le schema XSD correspondant au fichier XML donne
# 2. Parse le XSD (structure des tables)
# 3. Applique le nommage multi-XSD (prefixe par schema / partage si identique)
# 4. Cree les tables Oracle si necessaire (ne touche pas a celles qui existent deja)
# 5. Extrait les donnees du XML
# 6. Charge les donnees dans Oracle
# 7. Journalise le resultat dans ETL_LOG
#
# Usage (depuis n'importe quel dossier) :
#   python experiments/process_xml.py data\xml\a_traiter\mon_fichier.xml

import sys
import os
import time

# Chemins calcules a partir de l'emplacement REEL de ce fichier (pas du
# dossier depuis lequel la commande est lancee) : robuste peu importe
# d'ou on execute le script.
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(THIS_DIR)          # dossier parent (racine du projet)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

sys.path.append(PROJECT_ROOT)  # pour "import config"
sys.path.append(SRC_DIR)       # pour "from xsd_parser import ..." etc.

from xsd_parser import XSDParser
from table_generator import TableGenerator
from xml_extractor import XMLExtractor
from data_loader import DataLoader
from schema_registry import discover_schemas, detect_schema, apply_schema_naming

import config

ORACLE_USER = config.DB_USERNAME
ORACLE_PASSWORD = config.DB_PASSWORD
ORACLE_DSN = config.DB_DSN


def process_xml(xml_path):
    start = time.time()
    nom_fichier = os.path.basename(xml_path)

    generator = TableGenerator(ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN)
    loader = DataLoader(ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN)

    try:
        generator.connect()
        loader.connect()
        loader.create_log_table()

        # 1. Detection automatique du schema
        print(f"\n=== Detection du schema pour {nom_fichier} ===")
        schema_name = detect_schema(xml_path)
        if schema_name is None:
            raise ValueError("Aucun XSD connu (data/xsd/) ne correspond a ce fichier XML")
        print(f"Schema detecte : {schema_name}")

        xsd_path = discover_schemas()[schema_name]

        # 2. Parsing du XSD
        print(f"\n=== Parsing de {xsd_path} ===")
        parser = XSDParser(xsd_path)
        tables, tag_map = parser.parse()

        # 3. Nommage multi-XSD (prefixe/partage selon la regle validee)
        renamed_tables = apply_schema_naming(tables, schema_name, generator)

        # 4. Creation des tables si necessaire
        print(f"\n=== Verification/creation des tables Oracle ===")
        generator.create_all_tables(renamed_tables, drop_if_exists=False)

        # 5. Extraction du XML (avec les noms de table deja renommes)
        print(f"\n=== Extraction de {nom_fichier} ===")
        extractor = XMLExtractor(xml_path, renamed_tables, tag_map)
        data = extractor.extract()

        # 6. Chargement dans Oracle
        print(f"\n=== Chargement dans Oracle ===")
        total_inserted = loader.load_all(renamed_tables, data)

        duree = time.time() - start
        loader.log_result(nom_fichier, "OK", lignes_chargees=total_inserted, duree_secondes=duree)
        print(f"\nTermine : {total_inserted} ligne(s) chargee(s) en {duree:.1f}s (schema: {schema_name})")

    except Exception as e:
        duree = time.time() - start
        try:
            loader.log_result(nom_fichier, "ERREUR", message_erreur=str(e), duree_secondes=duree)
        except Exception:
            pass  # si meme le log echoue (ex: connexion perdue), on ne bloque pas l'erreur d'origine
        print(f"\nErreur lors du traitement de {nom_fichier} : {e}")
        raise
    finally:
        generator.disconnect()
        loader.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python process_xml.py <chemin_vers_xml>")
        sys.exit(1)

    process_xml(sys.argv[1])