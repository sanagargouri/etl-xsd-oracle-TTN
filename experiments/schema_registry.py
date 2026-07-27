"""
schema_registry.py

Etape 1 du support multi-XSD :
- decouverte automatique des fichiers XSD presents dans data/xsd/
- detection du bon schema pour un fichier XML donne (validation complete)

La logique de nommage de tables (signatures, prefixes par schema) sera
ajoutee a l'etape suivante, une fois cette detection validee.
"""

import os
import re
import xmlschema

SCHEMA_DIR = os.path.join("data", "xsd")

# Fichiers XSD presents dans data/xsd/ mais qui ne sont PAS des formats de
# document TTN (schemas utilitaires, meta-schemas...) et qui ne doivent donc
# jamais etre proposes comme candidats par detect_schema(). A completer si
# d'autres fichiers de ce type apparaissent dans le dossier.
EXCLUDED_SCHEMAS = {"schema-definition"}

# Fichiers presents dans data/xsd/ qui ne sont PAS des formats de document
# TTN a proprement parler (schemas utilitaires/meta-schemas), donc a exclure
# des candidats de detect_schema(). Ex: "schema-definition.xsd" est le
# meta-schema qui decrit la syntaxe XSD elle-meme (equivalent de
# XMLSchema.xsd) - il valide presque n'importe quoi et fausserait la
# detection tolerante si on le laissait comme candidat.
EXCLUDED_SCHEMAS = {"schema-definition"}

# Cache des schemas deja charges et compiles (nom_schema -> objet XMLSchema11).
# Evite de reparser un XSD a chaque fichier XML traite.
_SCHEMA_CACHE = {}


def discover_schemas(schema_dir=SCHEMA_DIR):
    """
    Scanne le dossier des XSD et retourne un dict :
        { nom_schema: chemin_complet_xsd }

    Le nom du schema = nom du fichier sans extension.
    Ex: data/xsd/tce.xsd -> "tce"
        data/xsd/invoic.xsd -> "invoic"
    """
    schemas = {}
    for filename in os.listdir(schema_dir):
        if filename.lower().endswith(".xsd"):
            schema_name = os.path.splitext(filename)[0]
            if schema_name in EXCLUDED_SCHEMAS:
                continue
            schemas[schema_name] = os.path.join(schema_dir, filename)
    return schemas


def _load_schema(schema_name, xsd_path):
    """
    Charge (ou recupere depuis le cache) un schema compile en XSD 1.1.
    Les XSD TTN utilisent des fonctionnalites XSD 1.1 (xs:alternative,
    xs:assert), d'ou l'usage explicite de XMLSchema11 plutot que
    xmlschema.XMLSchema (qui reste en XSD 1.0 par defaut).
    """
    if schema_name not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[schema_name] = xmlschema.XMLSchema11(xsd_path)
    return _SCHEMA_CACHE[schema_name]


def detect_schema(xml_path, schema_dir=SCHEMA_DIR, max_errors_tolerance=5):
    """
    Essaie chaque XSD decouvert et retourne le nom du schema correspondant
    au fichier XML donne.

    1er passage : validation stricte. Si un schema valide integralement
    le document, on le retourne immediatement (cas ideal).

    2e passage (fallback tolerant) : si aucun schema ne valide a 100%,
    on retient celui qui a le MOINS d'erreurs de validation - tant que ce
    nombre reste faible (<= max_errors_tolerance). Ca absorbe les petits
    ecarts reels entre le XSD officiel et les documents TTN reels (ex:
    ordre de balises legerement different), sans jamais confondre deux
    formats de document differents (qui auraient des dizaines d'erreurs,
    pas quelques unes).

    Retourne None si aucun schema ne correspond, meme approximativement.
    """
    schemas = discover_schemas(schema_dir)
    loaded = {}

    # 1er passage : validation stricte
    for schema_name, xsd_path in schemas.items():
        try:
            schema = _load_schema(schema_name, xsd_path)
        except xmlschema.XMLSchemaException as e:
            print(f"[schema_registry] XSD ignore (impossible a charger) : {schema_name} ({e})")
            continue

        loaded[schema_name] = schema
        if schema.is_valid(xml_path):
            return schema_name

    # 2e passage : correspondance approximative (le moins d'erreurs possible)
    best_schema = None
    best_error_count = None

    for schema_name, schema in loaded.items():
        error_count = sum(1 for _ in schema.iter_errors(xml_path))
        if best_error_count is None or error_count < best_error_count:
            best_schema = schema_name
            best_error_count = error_count

    if best_schema is not None and best_error_count <= max_errors_tolerance:
        print(
            f"[schema_registry] Aucune validation stricte : schema le plus proche "
            f"retenu = {best_schema} ({best_error_count} ecart(s) mineur(s))"
        )
        return best_schema

    return None


def compute_signature(table):
    """
    Calcule la signature d'une table telle que produite par xsd_parser.py :
    liste triee de (nom_colonne, type_de_base), en ignorant la longueur/
    precision (ex: VARCHAR2(35) -> VARCHAR2) pour rester comparable avec
    ce que renvoie Oracle via get_table_signature().
    """
    signature = []
    for col in table["columns"]:
        base_type = col["sql_type"].upper().split("(")[0].strip()
        signature.append((col["name"].upper(), base_type))
    return sorted(signature)


def apply_schema_naming(tables, schema_name, generator):
    """
    Prend la liste de tables issue de xsd_parser.py pour UN schema donne,
    et retourne une NOUVELLE liste avec les noms de table corriges :

    - table avec parent_table -> toujours prefixee par le schema
      (ex: "Adresse" -> "TCE_ADRESSE")
    - table racine (sans parent_table) :
        - si aucune table Oracle du meme nom n'existe -> pas de prefixe
        - si une table du meme nom existe avec la MEME signature -> partagee
          (pas de prefixe, la table existante sera reutilisee)
        - si une table du meme nom existe avec une signature DIFFERENTE ->
          prefixee, pour eviter toute collision

    Ne modifie pas la liste "tables" recue : renvoie des copies.
    """
    prefix = schema_name.upper() + "_"
    rename_map = {}

    # 1er passage : decider du nouveau nom de chaque table
    for table in tables:
        original_name = table["table_name"]

        if "parent_table" in table:
            new_name = prefix + original_name
        else:
            if generator.table_exists(original_name):
                existing_sig = generator.get_table_signature(original_name)
                new_sig = compute_signature(table)
                if existing_sig == new_sig:
                    new_name = original_name  # partage
                else:
                    new_name = prefix + original_name  # collision -> separe
            else:
                new_name = original_name  # premiere creation, pas de conflit

        rename_map[original_name] = new_name

    # 2e passage : appliquer les nouveaux noms, y compris dans parent_table
    # ET dans le texte "REFERENCES ancien_nom(...)" ecrit par xsd_parser.py
    # dans sql_type des colonnes FK (sinon la contrainte Oracle pointerait
    # encore vers l'ancienne table non-prefixee).
    renamed_tables = []
    for table in tables:
        new_table = dict(table)
        new_table["table_name"] = rename_map[table["table_name"]]

        if "parent_table" in table:
            old_parent = table["parent_table"]
            new_parent = rename_map.get(old_parent, old_parent)
            new_table["parent_table"] = new_parent

            if old_parent != new_parent:
                new_columns = []
                for col in table["columns"]:
                    new_col = dict(col)
                    sql_type = new_col.get("sql_type", "")
                    if "REFERENCES" in sql_type:
                        new_col["sql_type"] = re.sub(
                            rf"REFERENCES\s+{re.escape(old_parent)}\b",
                            f"REFERENCES {new_parent}",
                            sql_type,
                        )
                    new_columns.append(new_col)
                new_table["columns"] = new_columns

        renamed_tables.append(new_table)

    return renamed_tables


if __name__ == "__main__":
    # Test manuel rapide, a lancer depuis la racine du projet :
    #   python schema_registry.py data/xml/a_traiter/exemple.xml
    import sys

    print("Schemas detectes dans data/xsd/ :")
    for name, path in discover_schemas().items():
        print(f"  - {name} -> {path}")

    if len(sys.argv) > 1:
        xml_test = sys.argv[1]
        result = detect_schema(xml_test)
        print(f"\nFichier teste : {xml_test}")
        print(f"Schema detecte : {result}")