# test_naming.py
#
# Script de test : verifie que apply_schema_naming() donne les bons noms
# de table pour le schema INVOIC, SANS RIEN CREER dans Oracle
# (uniquement des lectures : table_exists / get_table_signature).
#
# A placer a la racine du projet, a cote de schema_registry.py et table_generator.py
# Lancer avec : python test_naming.py

import sys
sys.path.append("src")

from xsd_parser import XSDParser
from table_generator import TableGenerator
from schema_registry import apply_schema_naming

# --- Connexion Oracle ---
generator = TableGenerator(
    username="sana",
    password="Oracle123",
    dsn="localhost:1521/orcl2121"
)
generator.connect()

# --- Parsing du XSD INVOIC ---
print("\n=== Parsing du XSD INVOIC ===")
parser = XSDParser("data/xsd/facture_INVOIC_V1.8.8_withoutSig.xsd")
tables, tag_map = parser.parse()
print(f"{len(tables)} tables trouvees dans le XSD")

# --- Application de la regle de nommage ---
print("\n=== Resultat du nommage (schema = 'invoic') ===")
renamed = apply_schema_naming(tables, "invoic", generator)

for t in renamed:
    parent = t.get("parent_table", "(aucun)")
    print(f"{t['table_name']:<40}  <- parent: {parent}")

generator.disconnect()

print("\nTermine. Verifie ci-dessus :")
print("  - les tables AVEC parent doivent commencer par INVOIC_")
print("  - les tables SANS parent (racines) n'ont pas de prefixe")
