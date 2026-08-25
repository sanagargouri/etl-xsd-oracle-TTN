"""
xsd_xml_to_ddl.py
------------------
GENERIQUE : prend un XSD (structure) + un XML (instance reelle), et :
  1. Determine la liste des tables et de LEURS COLONNES a partir du XSD
     (structure figee par le schema, pas par ce qui existe dans tel ou
     tel XML). Une table repetee n'apparait que si elle a au moins une
     occurrence dans le XML, mais une fois qu'une table existe, TOUTES
     les colonnes prevues par le XSD pour cette table sont creees --
     meme celles absentes de ce XML precis, qui restent alors vides.
  2. Extrait aussi les VRAIES VALEURS de chaque occurrence du XML, pour pouvoir
     afficher chaque table remplie (comme un apercu tableur), sans base de donnees.
  3. Genere le DDL CREATE TABLE en texte pour les tables selectionnees.

Regle de structure (aucune balise codee en dur, purement generique) :
  - Un element qui peut se repeter (maxOccurs > 1) = une vraie table, une ligne
    par occurrence reelle dans le XML.
  - Un element complexe a occurrence unique (simple wrapper de structuration,
    tres courant en EDIFACT/TEIF) est aplati comme colonne(s) dans la table
    parente, avec un nom de colonne qui garde le chemin d'origine. Ce wrapper
    est parcouru meme s'il est absent du XML, pour que ses colonnes existent
    quand meme (vides) -- la structure suit le XSD, pas le XML.
  - Un element avec attributs + texte direct (xs:simpleContent) donne une
    colonne "VALEUR" pour le texte, plus une colonne par attribut.
  - Les cycles/recursions (un type qui se contient lui-meme) sont detectes et
    coupes proprement (pas de boucle infinie).
"""

import argparse
from lxml import etree

XSD_NS = "{http://www.w3.org/2001/XMLSchema}"

XSD_TO_SQL = {
    "string": "VARCHAR2(255)", "normalizedString": "VARCHAR2(255)", "token": "VARCHAR2(255)",
    "int": "NUMBER(10)", "integer": "NUMBER(10)", "positiveInteger": "NUMBER(10)",
    "nonNegativeInteger": "NUMBER(10)", "long": "NUMBER(19)", "short": "NUMBER(5)",
    "decimal": "NUMBER(18,4)", "double": "BINARY_DOUBLE", "float": "BINARY_FLOAT",
    "boolean": "NUMBER(1)", "date": "DATE", "dateTime": "TIMESTAMP", "time": "VARCHAR2(20)",
    "anyURI": "VARCHAR2(500)", "ID": "VARCHAR2(255)", "IDREF": "VARCHAR2(255)",
}
DEFAULT_SQL_TYPE = "VARCHAR2(255)"


def local_name(tag):
    return tag.split("}")[-1] if "}" in str(tag) else tag


def sql_type_for(xsd_type):
    if not xsd_type:
        return DEFAULT_SQL_TYPE
    return XSD_TO_SQL.get(xsd_type.split(":")[-1], DEFAULT_SQL_TYPE)


def xml_children_named(xml_elem, name):
    if xml_elem is None:
        return []
    return [c for c in xml_elem if local_name(c.tag) == name]


# ---------- Etape 1 : structure du XSD ----------

class SchemaNode:
    def __init__(self, name, repeated=False, nullable=True):
        self.name = name
        self.repeated = repeated
        self.nullable = nullable
        self.attributes = []
        self.simple_children = []
        self.table_children = []


def resolve_complex_type(root, type_name):
    if not type_name:
        return None
    local = type_name.split(":")[-1]
    for ct in root.findall(f"{XSD_NS}complexType"):
        if ct.get("name") == local:
            return ct
    return None


def is_repeated(elem):
    m = elem.get("maxOccurs", "1")
    return m == "unbounded" or (m.isdigit() and int(m) > 1)


def direct_attributes(complex_node):
    """
    Attributs REELLEMENT rattaches a cet element : enfants directs de
    complexType, ou herites via xs:extension/xs:restriction, que ce soit
    dans complexContent OU simpleContent (cas frequent : element avec
    attributs + texte direct, ex <DateText format="...">070612</DateText>).
    N'utilise PAS de recherche profonde (.//), pour ne pas remonter par erreur
    les attributs de sous-elements imbriques plus loin dans l'arbre.
    """
    attrs = list(complex_node.findall(f"{XSD_NS}attribute"))
    for content_tag in ("complexContent", "simpleContent"):
        content = complex_node.find(f"{XSD_NS}{content_tag}")
        if content is not None:
            for tag in ("extension", "restriction"):
                node = content.find(f"{XSD_NS}{tag}")
                if node is not None:
                    attrs.extend(node.findall(f"{XSD_NS}attribute"))
    return attrs


MAX_DEPTH = 15


def inline_simple_type_base(elem):
    """
    Si l'element a un xs:simpleType inline avec xs:restriction base="...",
    retourne ce type de base (ex: 'xs:decimal'). Sinon None.
    """
    simple_type = elem.find(f"{XSD_NS}simpleType")
    if simple_type is None:
        return None
    restriction = simple_type.find(f"{XSD_NS}restriction")
    if restriction is None:
        return None
    return restriction.get("base")


def build_schema_node(root, elem, xsd_root_doc, ancestor_types=None, depth=0):
    if ancestor_types is None:
        ancestor_types = set()

    name = elem.get("name")
    node = SchemaNode(name, repeated=is_repeated(elem), nullable=elem.get("minOccurs", "1") == "0")

    inline = elem.find(f"{XSD_NS}complexType")
    type_attr = elem.get("type") or inline_simple_type_base(elem)
    complex_node = inline if inline is not None else resolve_complex_type(xsd_root_doc, type_attr)

    if complex_node is None:
        node.sql_type = sql_type_for(type_attr)
        return node

    type_key = type_attr.split(":")[-1] if type_attr else f"#inline:{name}"
    if type_key in ancestor_types or depth >= MAX_DEPTH:
        node.sql_type = DEFAULT_SQL_TYPE
        return node

    next_ancestors = ancestor_types | {type_key}
    node.sql_type = None

    for attr in direct_attributes(complex_node):
        a_name = attr.get("name")
        if a_name:
            node.attributes.append((a_name, sql_type_for(attr.get("type"))))

    simple_content = complex_node.find(f"{XSD_NS}simpleContent")
    if simple_content is not None:
        ext_or_res = simple_content.find(f"{XSD_NS}extension")
        if ext_or_res is None:
            ext_or_res = simple_content.find(f"{XSD_NS}restriction")
        base_type = ext_or_res.get("base") if ext_or_res is not None else None
        node.simple_children.append(("VALEUR", sql_type_for(base_type), node.nullable))
        return node

    for grp_tag in ("sequence", "all", "choice"):
        grp = complex_node.find(f"{XSD_NS}{grp_tag}")
        if grp is None:
            continue
        for child in grp:
            if local_name(child.tag) != "element":
                continue
            child_node = build_schema_node(xsd_root_doc, child, xsd_root_doc, next_ancestors, depth + 1)
            if child_node.sql_type is None:
                node.table_children.append(child_node)
            else:
                node.simple_children.append((child_node.name, child_node.sql_type, child_node.nullable))
    return node


def build_schema_tree(xsd_path):
    tree = etree.parse(xsd_path)
    root = tree.getroot()
    top_elements = root.findall(f"{XSD_NS}element")
    if not top_elements:
        raise ValueError("Aucun element racine dans ce XSD.")
    return build_schema_node(root, top_elements[0], root)


# ---------- Etape 2 : tables + LIGNES DE DONNEES REELLES ----------

class AvailableTable:
    def __init__(self, display_name, sql_name, parent=None, leaf_name=None):
        self.display_name = display_name
        self.sql_name = sql_name
        self.parent = parent
        self.leaf_name = leaf_name  # nom court (derniere balise), sans le chemin des wrappers
        self.column_types = {}   # col_name -> (sql_type, nullable)
        self.column_labels = {}  # col_name -> nom court lisible (derniere balise)
        self.rows = []           # [dict col_name -> valeur texte ou None]

    def _register(self, col_name, sql_type, nullable, leaf_label=None):
        if col_name not in self.column_types:
            self.column_types[col_name] = (sql_type, nullable)
            self.column_labels[col_name] = leaf_label or col_name

    @property
    def nb_columns(self):
        return len(self.column_types)

    def to_ddl(self, fk_column_name=None, own_pk_columns=None):
        """
        fk_column_name : colonne de la table PARENTE a utiliser comme cle de
        liaison (remplace la reference a l'ID genere du parent).
        own_pk_columns : liste de colonnes de CETTE table formant sa cle
        primaire naturelle (simple ou composite). Si fourni (non vide),
        plus d'ID_<table> genere du tout.
        """
        own_pk_columns = own_pk_columns or []
        use_natural_fk = (
            fk_column_name and self.parent is not None
            and fk_column_name in self.parent.column_types
        )
        use_natural_pk = bool(own_pk_columns)

        lines = []
        if not use_natural_pk:
            lines.append(f"ID_{self.sql_name} NUMBER GENERATED ALWAYS AS IDENTITY")

        if self.parent:
            if use_natural_fk:
                parent_col_type, _ = self.parent.column_types[fk_column_name]
                lines.append(f"{fk_column_name} {parent_col_type} NOT NULL")
            else:
                lines.append(f"ID_{self.parent.sql_name} NUMBER NOT NULL")

        for cname, (ctype, nullable) in self.column_types.items():
            forced_not_null = cname in own_pk_columns
            lines.append(f"{cname} {ctype}{'' if (nullable and not forced_not_null) else ' NOT NULL'}")

        if use_natural_pk:
            pk_cols = ", ".join(own_pk_columns)
        else:
            pk_cols = f"ID_{self.sql_name}"
        constraints = [f"CONSTRAINT PK_{self.sql_name} PRIMARY KEY ({pk_cols})"]
        if self.parent:
            ref_col = fk_column_name if use_natural_fk else f"ID_{self.parent.sql_name}"
            constraints.append(
                f"CONSTRAINT FK_{self.sql_name}_{self.parent.sql_name} "
                f"FOREIGN KEY ({ref_col}) REFERENCES {self.parent.sql_name}({ref_col})"
            )
        body = ",\n    ".join(lines + constraints)
        return f"CREATE TABLE {self.sql_name} (\n    {body}\n);"


def _process_row(schema_node, xml_elem, display_prefix, sql_prefix, parent_table, parent_row_pk,
                  tables_by_name, order, leaf_name=None):
    table = tables_by_name.get(sql_prefix)
    if table is None:
        table = AvailableTable(display_prefix, sql_prefix, parent=parent_table, leaf_name=leaf_name)
        tables_by_name[sql_prefix] = table
        order.append(sql_prefix)

    row_pk = len(table.rows) + 1
    row = {"ID": row_pk}
    if parent_table is not None:
        row["ID_PARENT"] = parent_row_pk

    _collect_row(schema_node, xml_elem, [], table, row, display_prefix, sql_prefix,
                 tables_by_name, order, row_pk)

    table.rows.append(row)
    return row_pk


def _collect_row(schema_node, xml_elem, path, table, row, display_prefix, sql_prefix,
                  tables_by_name, order, row_pk):
    # La structure (quelles colonnes existent) suit toujours le XSD : on
    # enregistre chaque colonne prevue par le schema meme si sa valeur est
    # absente de ce XML precis (xml_elem peut alors valoir None plus bas
    # pour les wrappers non-repetes). Seule la VALEUR dans row[] depend
    # de ce qui est reellement present dans le XML.
    for a_name, a_type in schema_node.attributes:
        col_name = "_".join(path + [a_name]).upper()
        table._register(col_name, a_type, True, leaf_label=a_name.upper())
        val = xml_elem.get(a_name) if xml_elem is not None else None
        if val is not None:
            row[col_name] = val

    for c_name, c_type, c_nullable in schema_node.simple_children:
        col_name = "_".join(path + [c_name]).upper()
        table._register(col_name, c_type, c_nullable, leaf_label=c_name.upper())
        if c_name == "VALEUR":
            val = xml_elem.text.strip() if (xml_elem is not None and xml_elem.text) else None
        else:
            child = xml_children_named(xml_elem, c_name)
            val = child[0].text.strip() if (child and child[0].text) else None
        if val is not None and val != "":
            row[col_name] = val

    for child_schema in schema_node.table_children:
        matches = xml_children_named(xml_elem, child_schema.name)
        if child_schema.repeated:
            # Une vraie sous-table n'est creee que s'il y a au moins une
            # occurrence dans ce XML -- on ne peut pas afficher/creer une
            # table dont on ne sait pas combien de lignes elle aurait.
            if not matches:
                continue
            path_part = "_".join(path).lower() + "_" if path else ""
            path_part_sql = "_".join(path).upper() + "_" if path else ""
            child_display = f"{display_prefix}.{path_part}{child_schema.name.lower()}"
            child_sql = f"{sql_prefix}_{path_part_sql}{child_schema.name.upper()}"
            for m in matches:
                _process_row(child_schema, m, child_display, child_sql, table, row_pk,
                             tables_by_name, order, leaf_name=child_schema.name.upper())
        else:
            # Wrapper a occurrence unique : on parcourt ses colonnes meme
            # s'il est absent du XML (child_xml=None), pour que la
            # structure de la table suive le XSD et pas seulement ce XML.
            child_xml = matches[0] if matches else None
            _collect_row(child_schema, child_xml, path + [child_schema.name], table, row,
                         display_prefix, sql_prefix, tables_by_name, order, row_pk)


def simplify_column_names(table):
    """
    Remplace le nom de chaque colonne par son nom court (derniere balise),
    SAUF si deux colonnes differentes de CETTE MEME table partagent le meme
    nom court (ex: deux "CODE" a des endroits differents) -> dans ce cas,
    le nom long (avec le chemin des wrappers) est conserve pour cette colonne.
    """
    from collections import Counter
    label_counts = Counter(table.column_labels.get(c, c) for c in table.column_types)
    rename_map = {}
    for col in list(table.column_types.keys()):
        label = table.column_labels.get(col, col)
        if label_counts[label] == 1 and label != col:
            rename_map[col] = label

    if not rename_map:
        return

    table.column_types = {
        rename_map.get(col, col): val for col, val in table.column_types.items()
    }
    table.column_labels = {
        rename_map.get(col, col): label for col, label in table.column_labels.items()
    }
    for row in table.rows:
        for old_name, new_name in rename_map.items():
            if old_name in row:
                row[new_name] = row.pop(old_name)


def get_available_tables(xsd_path, xml_path, root_name):
    schema_root = build_schema_tree(xsd_path)
    xml_tree = etree.parse(xml_path)
    xml_root = xml_tree.getroot()

    xml_root_tag = local_name(xml_root.tag)
    if xml_root_tag != schema_root.name:
        raise ValueError(
            f"Le XML et le XSD ne correspondent pas : le XSD attend une balise "
            f"racine <{schema_root.name}>, mais le XML fourni commence par "
            f"<{xml_root_tag}>. Verifie que tu as depose le bon couple XSD+XML."
        )

    tables_by_name = {}
    order = []
    _process_row(schema_root, xml_root, root_name.lower(), root_name.upper(), None, None,
                 tables_by_name, order)
    tables = [tables_by_name[name] for name in order]

    # Simplification des noms : "racine.wrapper1_wrapper2_article" -> "racine.article"
    # SAUF si deux tables differentes partagent le meme nom court (ex: deux
    # "DateText" a des endroits differents) -> on garde alors le nom long
    # (deja construit avec le chemin complet) pour lever l'ambiguite.
    from collections import Counter
    leaf_counts = Counter(t.leaf_name for t in tables[1:] if t.leaf_name)
    for t in tables[1:]:
        if t.leaf_name and leaf_counts[t.leaf_name] == 1:
            t.sql_name = f"{t.parent.sql_name}_{t.leaf_name}"
            t.display_name = f"{t.parent.display_name}.{t.leaf_name.lower()}"
        # sinon (ambigu) : on garde le nom long deja construit par _process_row,
        # qui inclut le chemin des wrappers pour eviter toute collision

    for t in tables:
        simplify_column_names(t)

    racine = tables[0]
    if racine.nb_columns == 0 and len(tables) == 1:
        raise ValueError(
            "Aucune donnee n'a pu etre extraite de ce XML avec ce XSD, meme si "
            "la balise racine correspond. Verifie que ce XML est bien une "
            "instance valide de ce schema (structure interne, espaces de noms, "
            "ou version du XSD peut-etre differente)."
        )

    return tables


def main():
    parser = argparse.ArgumentParser(description="Genere des CREATE TABLE + apercu des donnees, a partir d'un XSD + XML.")
    parser.add_argument("xsd_file")
    parser.add_argument("xml_file")
    parser.add_argument("--root-name", required=True)
    parser.add_argument("--select", help="Indices separes par virgule des tables a creer (ex: 1,2). Sans cette option: liste seulement.")
    args = parser.parse_args()

    tables = get_available_tables(args.xsd_file, args.xml_file, args.root_name)

    if not args.select:
        print("Tables disponibles (a cocher) :")
        for i, t in enumerate(tables, start=1):
            print(f"  [{i}] {t.display_name}  ({t.nb_columns} colonnes, {len(t.rows)} lignes)")
        return

    chosen_idx = {int(x.strip()) for x in args.select.split(",")}
    chosen = [t for i, t in enumerate(tables, start=1) if i in chosen_idx]

    for t in chosen:
        print(f"\n=== {t.sql_name} ({len(t.rows)} lignes) ===")
        cols = ["ID"] + (["ID_PARENT"] if t.parent else []) + list(t.column_types.keys())
        print(" | ".join(cols))
        for row in t.rows:
            print(" | ".join(str(row.get(c, "")) for c in cols))

    print("\n\n".join(t.to_ddl() for t in chosen))


if __name__ == "__main__":
    main()