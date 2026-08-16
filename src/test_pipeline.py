"""
test_pipeline.py
Test end-to-end : XSD -> tables Oracle -> extraction XML -> insertion.

Usage :
    python src/test_pipeline.py data/xsd/tce.xsd data/xml/a_traiter/<un_fichier>.xml

Ne PAS lancer avec drop_if_exists=True sur une base qui contient déjà des
données réelles -- ce script est pensé pour une base de test/dev.
"""
import sys
import os

# Racine du projet = dossier parent de src/, ajoutée en plus de src/ elle-même
# pour que "import config" fonctionne quel que soit l'endroit d'où le script
# est lancé (ici : depuis la racine, via "python src\test_pipeline.py").
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)
sys.path.append(os.path.join(PROJECT_ROOT, "src"))

from xsd_parser_tce import XSDParserTCE
from xml_extractor import XMLExtractor
from table_generator import TableGenerator
from data_loader import DataLoader
import config


def main():
    if len(sys.argv) < 3:
        print("Usage: python src/test_pipeline.py <xsd> <xml>")
        sys.exit(1)

    xsd_path, xml_path = sys.argv[1], sys.argv[2]

    print("=== ÉTAPE 1 : Parsing du XSD ===")
    parser = XSDParserTCE(xsd_path)
    tables, tag_map = parser.parse()

    print("\n=== ÉTAPE 2 : Création des tables Oracle ===")
    generator = TableGenerator(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN
    )
    generator.connect()
    try:
        # drop_if_exists=True : pratique en test pour repartir propre à
        # chaque run, à retirer une fois en production.
        generator.create_all_tables(tables, drop_if_exists=True)
    finally:
        generator.disconnect()

    print("\n=== ÉTAPE 3 : Extraction du XML ===")
    extractor = XMLExtractor(xml_path, tables, tag_map)
    data = extractor.extract()
    extractor.print_summary()

    print("\n=== ÉTAPE 4 : Chargement dans Oracle ===")
    loader = DataLoader(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN
    )
    loader.connect()
    try:
        loader.load_all(tables, data)
        print("\ndocument_key_mapping :")
        for table_name, mapping in loader.document_key_mapping.items():
            print(f"  {table_name} : {mapping}")
    finally:
        loader.disconnect()

    print("\n=== TEST TERMINÉ ===")
    print("Vérifications à faire manuellement dans Oracle :")
    print("  SELECT * FROM DOCUMENT;")
    print("  SELECT * FROM ARTICLE;")
    print("  SELECT * FROM PIECES_JOINTE;")
    print("  -- Aucune colonne ID_* ne doit apparaître sauf sur les tables")
    print("  -- 'exception' documentées (ex: MINISTERE_COMMERCE_OBSERVATION)")


if __name__ == "__main__":
    main()
