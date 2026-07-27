# create_invoic_tables.py
#
# Cree reellement les tables INVOIC dans Oracle, avec le nommage
# multi-XSD (prefixe INVOIC_ pour les tables avec parent, partage
# ou prefixage automatique pour les tables racines).
#
# A placer a la racine du projet.
# Lancer avec : python create_invoic_tables.py

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

# --- Application de la regle de nommage multi-XSD ---
print("\n=== Nommage (schema = 'invoic') ===")
renamed = apply_schema_naming(tables, "invoic", generator)

for t in renamed:
    parent = t.get("parent_table", "(aucun)")
    print(f"{t['table_name']:<40}  <- parent: {parent}")

# --- Creation reelle dans Oracle ---
# drop_if_exists=False : par securite, on ne touche pas aux tables
# deja existantes (ni les anciennes tables sans prefixe, ni d'anciens
# essais INVOIC_* si tu relances le script plusieurs fois).
print("\n=== Creation des tables dans Oracle ===")
created, skipped, errors = generator.create_all_tables(renamed, drop_if_exists=False)

generator.disconnect()

print(f"\nResultat final : {created} creees, {skipped} deja existantes, {errors} erreurs")
