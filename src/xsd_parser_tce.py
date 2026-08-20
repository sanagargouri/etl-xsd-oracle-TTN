"""
xsd_parser_tce.py (as provided by user)
"""

import xml.etree.ElementTree as ET

XS = "{http://www.w3.org/2001/XMLSchema}"


class XSDParserTCE:
    ROOT_KEY_XML_PATH = ["REFERENCE_TTN", "NUMERO_DOSSIER"]
    ROOT_KEY_COLUMN_NAME = "numero_dossier"

    NATURAL_DISCRIMINANTS = {
        "ARTICLE": "numero_article",
        "PIECES_JOINTE": "reference_base_image",
    }

    def __init__(self, xsd_path):
        self.xsd_path = xsd_path
        self.tree = ET.parse(xsd_path)
        self.root = self.tree.getroot()
        self.tables = []
        self.tag_map = {}
        self.used_table_names = set()
        self.root_key_sql_type = None

    def parse(self):
        print(f"Analyse du XSD (mode TCE) : {self.xsd_path}")

        root_elem = self.root.find(f"{XS}element")
        if root_elem is None:
            raise ValueError(f"Aucun élément racine trouvé dans {self.xsd_path}")

        root_name = root_elem.get("name", "ROOT")
        print(f"  → Élément racine trouvé : {root_name}")

        self._build_table(
            elem=root_elem,
            table_name=root_name.upper(),
            parent_table=None,
            xml_tag=root_name,
            is_root=True,
        )

        print(f"  → {len(self.tables)} table(s) à générer (mode TCE)")
        print(f"  → {len(self.tag_map)} correspondance(s) type→balise trouvées")

        return self.tables, self.tag_map

    def _build_table(self, elem, table_name, parent_table, xml_tag, is_root=False):
        table_name = self._register_table_name(table_name, parent_table)

        table = {
            "table_name": table_name,
            "original_type": table_name,
            "xml_element_name": xml_tag.upper(),
            "columns": [],
            "children": [],
        }

        self.tag_map[table_name] = xml_tag

        used_col_names = set()

        self._flatten_columns_only(elem, table, table_name, prefix="", used_col_names=used_col_names)

        if is_root:
            self._promote_root_key(table)
        else:
            self._add_natural_or_fallback_key(table, parent_table)

        self.tables.append(table)

        self._build_child_tables(elem, table_name)

        return table_name

    def _flatten_columns_only(self, elem, table, table_name, prefix, used_col_names, xml_path=None):
        if xml_path is None:
            xml_path = []

        for child in self._get_children(elem):
            name = child.get("name")
            if not name:
                continue

            max_occurs = child.get("maxOccurs", "1")
            min_occurs = child.get("minOccurs", "1")
            nullable = (min_occurs == "0")
            grandchildren = self._get_children(child)

            if max_occurs == "unbounded":
                continue
            elif grandchildren:
                new_prefix = f"{prefix}{name.lower()}_"
                self._flatten_columns_only(
                    child, table, table_name, new_prefix, used_col_names,
                    xml_path=xml_path + [name],
                )
            else:
                col_name = self._truncate_name(f"{prefix}{name.lower()}", used_col_names)
                table["columns"].append({
                    "name": col_name,
                    "sql_type": self._infer_type(child),
                    "nullable": nullable,
                    "xml_path": xml_path + [name],
                })

    def _build_child_tables(self, elem, parent_table_name):
        for child in self._get_children(elem):
            name = child.get("name")
            if not name:
                continue
            max_occurs = child.get("maxOccurs", "1")
            grandchildren = self._get_children(child)

            if max_occurs == "unbounded":
                self._build_table(
                    elem=child,
                    table_name=name.upper(),
                    parent_table=parent_table_name,
                    xml_tag=name,
                )
            elif grandchildren:
                self._build_child_tables(child, parent_table_name)

    def _promote_root_key(self, table):
        key_col = None
        for col in table["columns"]:
            if col.get("xml_path") == self.ROOT_KEY_XML_PATH:
                key_col = col
                break

        if key_col is None:
            raise ValueError(
                f"NUMERO_DOSSIER introuvable via le chemin {self.ROOT_KEY_XML_PATH} "
                f"-- impossible de définir la clé naturelle de {table['table_name']}. "
                f"Vérifier que le XSD contient bien REFERENCE_TTN > NUMERO_DOSSIER."
            )

        base_type = key_col["sql_type"]
        key_col["name"] = self.ROOT_KEY_COLUMN_NAME
        # PAS de "PRIMARY KEY" inline ici : la contrainte est déclarée au
        # niveau table via table["primary_key"] ci-dessous, et
        # table_generator.generate_ddl en fait un CONSTRAINT PK_xxx.
        # Déclarer les deux provoque ORA-02260 (une seule clé primaire
        # possible par table) à la création.
        key_col["sql_type"] = base_type
        key_col["nullable"] = False
        self.root_key_sql_type = base_type
        table["primary_key"] = [self.ROOT_KEY_COLUMN_NAME]

    def _add_natural_or_fallback_key(self, table, parent_table):
        table_name = table["table_name"]
        fk_type = self.root_key_sql_type or "VARCHAR2(105)"

        parent_table_def = None
        parent_primary_key = None
        for t in self.tables:
            if t["table_name"] == parent_table:
                parent_table_def = t
                parent_primary_key = t.get("primary_key")
                break

        if parent_primary_key and len(parent_primary_key) > 1:
            print(f"    {table_name} → parent {parent_table} a clé composite {parent_primary_key}")

            for i, pk_col in enumerate(parent_primary_key):
                pk_col_type = fk_type
                if parent_table_def:
                    for col in parent_table_def["columns"]:
                        if col["name"] == pk_col:
                            # On ne garde que le TYPE brut : la clause
                            # REFERENCES du parent ne doit pas être recopiée.
                            # Sinon la table hérite d'une FK simple vers la
                            # table racine -- avec en plus son nom d'AVANT un
                            # éventuel renommage (/schemas), d'où un
                            # ORA-00942 -- alors que la vraie liaison est
                            # déjà assurée par la contrainte FK composite
                            # ajoutée plus bas.
                            base_type = col["sql_type"].replace("PRIMARY KEY", "").strip()
                            if "REFERENCES" in base_type:
                                base_type = base_type.split("REFERENCES")[0].strip()
                            pk_col_type = base_type
                            break

                table["columns"].insert(i, {
                    "name": pk_col,
                    "sql_type": pk_col_type,
                    "nullable": False,
                    "is_propagated_key": True,
                })

            fk_cols = ", ".join(parent_primary_key)
            table["columns"].append({
                "name": f"__fk_constraint_{parent_table}",
                "sql_type": f"CONSTRAINT FK_{table_name}_{parent_table} FOREIGN KEY ({fk_cols}) REFERENCES {parent_table}({fk_cols})",
                "nullable": True,
                "is_fk_constraint": True,
            })
        else:
            table["columns"].insert(0, {
                "name": self.ROOT_KEY_COLUMN_NAME,
                "sql_type": f"{fk_type} REFERENCES {parent_table}({self.ROOT_KEY_COLUMN_NAME})",
                "nullable": False,
                "is_propagated_key": True,
            })

        discriminant_name = self.NATURAL_DISCRIMINANTS.get(table["xml_element_name"])
        discriminant_col = None
        if discriminant_name:
            for col in table["columns"]:
                if col["name"] == discriminant_name:
                    discriminant_col = col
                    break

        if discriminant_col:
            if parent_primary_key and len(parent_primary_key) > 1:
                table["primary_key"] = parent_primary_key + [discriminant_name]
            else:
                table["primary_key"] = [self.ROOT_KEY_COLUMN_NAME, discriminant_name]

            table["parent_table"] = parent_table
            table["parent_key_column"] = parent_primary_key if parent_primary_key else self.ROOT_KEY_COLUMN_NAME
        else:
            print(f"    {table_name} : aucun discriminant naturel connu dans le XSD "
                  f"-- ID généré conservé par exception documentée")
            table["columns"].append({
                "name": f"id_{table_name.lower()}",
                "sql_type": "NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
                "nullable": False,
            })
            table["parent_table"] = parent_table
            table["parent_key_column"] = parent_primary_key if parent_primary_key else self.ROOT_KEY_COLUMN_NAME

    def _get_children(self, elem):
        ct = elem.find(f"{XS}complexType")
        if ct is None:
            return []
        container = ct.find(f"{XS}all")
        if container is None:
            container = ct.find(f"{XS}sequence")
        if container is None:
            return []
        return container.findall(f"{XS}element")

    def _infer_type(self, elem):
        SAFETY_MARGIN_FACTOR = 3
        SAFETY_MARGIN_MIN = 50
        MAX_VARCHAR2 = 4000

        simple_type = elem.find(f"{XS}simpleType")
        declared_length = None
        if simple_type is not None:
            restriction = simple_type.find(f"{XS}restriction")
            if restriction is not None:
                max_length = restriction.find(f"{XS}maxLength")
                if max_length is not None:
                    declared_length = int(max_length.get("value", "255"))
                else:
                    length_elem = restriction.find(f"{XS}length")
                    if length_elem is not None:
                        declared_length = int(length_elem.get("value", "255"))

        if declared_length is None:
            return "VARCHAR2(500)"

        safe_length = max(
            declared_length + SAFETY_MARGIN_MIN,
            declared_length * SAFETY_MARGIN_FACTOR,
        )
        safe_length = min(safe_length, MAX_VARCHAR2)
        return f"VARCHAR2({safe_length})"

    def _register_table_name(self, base_name, parent_table):
        candidate = self._truncate_name(base_name.replace("-", "_"))

        if candidate not in self.used_table_names:
            self.used_table_names.add(candidate)
            return candidate

        if parent_table:
            candidate2 = self._truncate_name(f"{parent_table}_{base_name}")
            if candidate2 not in self.used_table_names:
                self.used_table_names.add(candidate2)
                return candidate2

        counter = 2
        candidate3 = candidate
        while candidate3 in self.used_table_names:
            suffix = f"_{counter}"
            candidate3 = self._truncate_name(candidate[: 30 - len(suffix)] + suffix)
            counter += 1
        self.used_table_names.add(candidate3)
        return candidate3

    def _truncate_name(self, name, used_names=None, max_length=30):
        if len(name) <= max_length:
            truncated = name
        else:
            parts = name.split("_")
            if len(parts) >= 2:
                result_parts = [parts[-1]]
                current_length = len(parts[-1])

                prefix = parts[0]
                if current_length + len(prefix) + 1 <= max_length:
                    result_parts.insert(0, prefix)
                    current_length += len(prefix) + 1

                for part in reversed(parts[1:-1]):
                    if current_length + len(part) + 1 <= max_length:
                        result_parts.insert(-1, part)
                        current_length += len(part) + 1
                    else:
                        break

                truncated = "_".join(result_parts)
            else:
                truncated = name[:max_length]

        if used_names is None:
            return truncated

        original = truncated
        counter = 1
        while truncated in used_names:
            suffix = f"_{counter}"
            truncated = original[: max_length - len(suffix)] + suffix
            counter += 1
        used_names.add(truncated)
        return truncated