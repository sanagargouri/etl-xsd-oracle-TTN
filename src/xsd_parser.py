# xsd_parser.py
import xml.etree.ElementTree as ET

XS = "{http://www.w3.org/2001/XMLSchema}"

TYPE_MAPPING = {
    "string":  ("VARCHAR2", 255),
    "integer": ("NUMBER", None),
    "decimal": ("NUMBER", None),
    "boolean": ("VARCHAR2", 5),
    "date":    ("DATE", None),
    "dateTime":("TIMESTAMP", None),
}

class XSDParser:
    def __init__(self, xsd_path):
        self.xsd_path = xsd_path
        self.tree = ET.parse(xsd_path)
        self.root = self.tree.getroot()
        self.simple_types = {}
        self.complex_types = {}
        self.tables = []

    def parse(self):
        print(f"Analyse du XSD : {self.xsd_path}")
        self._parse_simple_types()
        print(f"  → {len(self.simple_types)} types simples trouvés")
        self._parse_complex_types()
        print(f"  → {len(self.complex_types)} types complexes trouvés")
        self._parse_root_element()
        self._build_tables()
        print(f"  → {len(self.tables)} tables à générer")
        return self.tables

    def _parse_simple_types(self):
        for simple_type in self.root.findall(f".//{XS}simpleType[@name]"):
            name = simple_type.get("name")
            oracle_type = self._resolve_simple_type(simple_type)
            self.simple_types[name] = oracle_type

    def _resolve_simple_type(self, node):
        restriction = node.find(f"{XS}restriction")
        if restriction is None:
            return "VARCHAR2(255)"
        base = restriction.get("base", "xs:string").replace("xs:", "")
        max_length = restriction.find(f"{XS}maxLength")
        if max_length is not None:
            length = int(max_length.get("value"))
            return f"VARCHAR2({length})"
        pattern = restriction.find(f"{XS}pattern")
        if pattern is not None:
            val = pattern.get("value", "")
            if "[0-9]" in val and ("." in val or "," in val):
                return "NUMBER(15,5)"
            if "[0-9]" in val:
                return "NUMBER"
        base_type, _ = TYPE_MAPPING.get(base, ("VARCHAR2", 255))
        return f"{base_type}(255)"

    def _parse_complex_types(self):
        for complex_type in self.root.findall(f".//{XS}complexType[@name]"):
            name = complex_type.get("name")
            self.complex_types[name] = self._extract_type_info(complex_type)

    def _parse_root_element(self):
        root_elem = self.root.find(f"{XS}element")
        if root_elem is None:
            print("  → Aucun élément racine trouvé")
            return
        root_name = root_elem.get("name", "ROOT")
        print(f"  → Élément racine trouvé : {root_name}")
        table = {
            "table_name": root_name.upper(),
            "original_type": root_name,
            "columns": [],
            "children": [],
        }
        table["columns"].append({
            "name": f"id_{root_name.lower()}",
            "sql_type": "NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
            "nullable": False,
        })
        anon_type = root_elem.find(f"{XS}complexType")
        if anon_type is not None:
            for attr in anon_type.findall(f".//{XS}attribute"):
                attr_name = attr.get("name")
                use = attr.get("use", "optional")
                if attr_name:
                    table["columns"].append({
                        "name": f"attr_{attr_name.lower()}",
                        "sql_type": "VARCHAR2(255)",
                        "nullable": (use != "required"),
                        "constraint": "" if use != "required" else " NOT NULL",
                    })
            sequence = anon_type.find(f"{XS}sequence")
            if sequence is not None:
                for child_elem in sequence.findall(f"{XS}element"):
                    child_name = child_elem.get("name", "")
                    child_type = child_elem.get("type", "")
                    min_occurs = int(child_elem.get("minOccurs", "1"))
                    if child_name:
                        table["children"].append({
                            "name": child_name,
                            "type_ref": child_type,
                            "nullable": (min_occurs == 0),
                            "is_list": False,
                        })
        self.tables.append(table)
        print(f"  → Table racine créée : {root_name.upper()}")

    def _extract_type_info(self, node):
        """
        Extrait les informations d'un type complexe.
        Gère deux cas :
        - xs:sequence : sous-éléments classiques
        - xs:simpleContent : contenu textuel + attributs
        ex: <Amount currencyIdentifier="TND">2.540</Amount>
        """
        fields = []
        children = []

        # Cas 1 : xs:simpleContent (contenu textuel + attributs)
        # ex: Amount, LocType, DtmDetailType...
        simple_content = node.find(f"{XS}simpleContent")
        if simple_content is not None:
            extension = simple_content.find(f"{XS}extension")
            if extension is not None:
                # Le contenu textuel devient une colonne "value"
                base_type = extension.get("base", "xs:string")
                oracle_type = self._resolve_type_ref(base_type)
                fields.append({
                    "name": "value",
                    "type_ref": base_type,
                    "oracle_type": oracle_type,
                    "is_complex": False,
                    "is_list": False,
                    "nullable": True,
                })
                # Les attributs de l'extension deviennent aussi des colonnes
                for attr in extension.findall(f"{XS}attribute"):
                    attr_info = self._extract_attribute_info(attr)
                    if attr_info:
                        fields.append(attr_info)
            return {"fields": fields, "children": children}

        # Cas 2 : xs:sequence (sous-éléments classiques) — comportement d'origine
        for elem in node.findall(f".//{XS}element"):
            field_info = self._extract_element_info(elem)
            if field_info:
                if field_info["is_complex"]:
                    children.append(field_info)
                else:
                    fields.append(field_info)

        for attr in node.findall(f".//{XS}attribute"):
            attr_info = self._extract_attribute_info(attr)
            if attr_info:
                fields.append(attr_info)

        return {"fields": fields, "children": children}


    def _extract_element_info(self, elem):
        """
        Extrait les infos d'un <xs:element> individuel.
        Gère aussi les types anonymes avec xs:simpleContent.
        """
        name = elem.get("name")
        type_ref = elem.get("type", "")
        min_occurs = int(elem.get("minOccurs", "1"))
        max_occurs = elem.get("maxOccurs", "1")

        if not name:
            return None

        is_list = (max_occurs == "unbounded")
        is_nullable = (min_occurs == 0)

        # Cas spécial : type anonyme avec xs:simpleContent
        # ex: <xs:element name="Amount"><xs:complexType><xs:simpleContent>...
        anon_complex = elem.find(f"{XS}complexType")
        if anon_complex is not None:
            simple_content = anon_complex.find(f"{XS}simpleContent")
            if simple_content is not None:
                extension = simple_content.find(f"{XS}extension")
                if extension is not None:
                    base_type = extension.get("base", "xs:string")
                    oracle_type = self._resolve_type_ref(base_type)
                    return {
                        "name": name,
                        "type_ref": base_type,
                        "oracle_type": oracle_type,
                        "is_complex": False,
                        "is_list": is_list,
                        "nullable": is_nullable or is_list,
                    }

        # Cas normal : type référencé explicitement
        is_complex = (
            type_ref in self.complex_types or
            type_ref.replace("xs:", "") not in TYPE_MAPPING
        )

        oracle_type = None
        if not is_complex:
            oracle_type = self._resolve_type_ref(type_ref)
        elif type_ref in self.simple_types:
            oracle_type = self.simple_types[type_ref]
            is_complex = False

        return {
            "name": name,
            "type_ref": type_ref,
            "oracle_type": oracle_type,
            "is_complex": is_complex,
            "is_list": is_list,
            "nullable": is_nullable or is_list,
        }

    def _extract_attribute_info(self, attr):
        name = attr.get("name")
        use = attr.get("use", "optional")
        type_ref = attr.get("type", "")
        if not name:
            return None
        oracle_type = self._resolve_type_ref(type_ref) if type_ref else "VARCHAR2(255)"
        return {
            "name": f"attr_{name}",
            "type_ref": type_ref,
            "oracle_type": oracle_type,
            "is_complex": False,
            "is_list": False,
            "nullable": (use != "required"),
        }

    def _resolve_type_ref(self, type_ref):
        if type_ref in self.simple_types:
            return self.simple_types[type_ref]
        base = type_ref.replace("xs:", "")
        sql_type, length = TYPE_MAPPING.get(base, ("VARCHAR2", 255))
        if length:
            return f"{sql_type}({length})"
        return sql_type

    def _flatten_type(self, type_name, visited=None):
        if visited is None:
            visited = set()
        if type_name in visited:
            return []
        visited.add(type_name)
        if type_name not in self.complex_types:
            return []
        type_info = self.complex_types[type_name]
        flat_fields = []
        for field in type_info["fields"]:
            flat_fields.append(field)
        for child in type_info["children"]:
            if not child["is_list"]:
                child_fields = self._flatten_type(child["type_ref"], visited.copy())
                for cf in child_fields:
                    prefixed_field = cf.copy()
                    prefixed_field["name"] = f"{child['name'].lower()}_{cf['name']}"
                    flat_fields.append(prefixed_field)
        return flat_fields
    
    def _find_parent_relationships(self, list_types, table_names):
        """
        Pour chaque type qui devient une table, trouve son ancêtre
        le plus proche qui est AUSSI une table.
        Si aucun ancêtre-table n'est trouvé, pointe vers TEIF (table racine).
        """
        # Étape 1 : construire la relation parent direct pour TOUS les types
        direct_parent = {}

        # Depuis les types complexes nommés
        for complex_type in self.root.findall(f".//{XS}complexType[@name]"):
            parent_name = complex_type.get("name")
            for elem in complex_type.findall(f".//{XS}element"):
                type_ref = elem.get("type", "")
                if type_ref and type_ref not in direct_parent:
                    direct_parent[type_ref] = parent_name
                for alt in elem.findall(f"{XS}alternative"):
                    alt_type = alt.get("type", "")
                    if alt_type and alt_type not in direct_parent:
                        direct_parent[alt_type] = parent_name

        # Depuis l'élément racine (TEIF) — ses enfants directs pointent vers TEIF
        root_elem = self.root.find(f"{XS}element")
        if root_elem is not None:
            root_name = root_elem.get("name", "ROOT")
            anon_type = root_elem.find(f"{XS}complexType")
            if anon_type is not None:
                sequence = anon_type.find(f"{XS}sequence")
                if sequence is not None:
                    for child_elem in sequence.findall(f"{XS}element"):
                        child_type = child_elem.get("type", "")
                        if child_type and child_type not in direct_parent:
                            direct_parent[child_type] = root_name

        # Étape 2 : pour chaque type-table, remonter jusqu'au
        # premier ancêtre qui est aussi une table
        parent_map = {}

        for type_name in list_types:
            current = type_name
            visited = set()
            found_parent = None

            while current in direct_parent:
                parent = direct_parent[current]
                if parent in visited:
                    break
                visited.add(parent)

                parent_table_name = parent.upper().replace("TYPE", "").strip("_")

                # Est-ce que ce parent est lui-même une table ?
                if parent in list_types or parent_table_name in table_names:
                    found_parent = parent
                    break

                # Est-ce que c'est la racine TEIF ?
                if parent == root_name:
                    found_parent = root_name
                    break

                current = parent

            # Si on n'a pas trouvé de parent-table, on pointe vers TEIF par défaut
            if found_parent is None and type_name != root_name:
                found_parent = root_name

            if found_parent:
                parent_map[type_name] = found_parent

        return parent_map

    def _build_tables(self):
        list_types = set()
        forced_tables = {
            "MoaDetailsType",
            "TaxDetailsType",
            "AlcDetailsType",
            "PytSegType",
            "CtaGrpType",
            "RefGrpType",
            "AdressesType",
            "LinType",
        }
        list_types.update(forced_tables)
        

        for elem in self.root.findall(f".//{XS}element"):
            max_occurs = elem.get("maxOccurs", "1")
            if max_occurs == "unbounded":
                type_ref = elem.get("type", "")
                if type_ref in self.complex_types:
                    list_types.add(type_ref)
                for alt in elem.findall(f"{XS}alternative"):
                    alt_type = alt.get("type", "")
                    if alt_type in self.complex_types:
                        list_types.add(alt_type)

        print(f"  → Types qui sont des listes : {list_types}")

        # Noms des tables qui seront créées
        table_names = set()
        for tn, _ in self.complex_types.items():
            tname = tn.upper().replace("TYPE", "").strip("_")
            if tn in list_types:
                table_names.add(tname)
        table_names.add("TEIF")  # ajoute la table racine

        # Construire le dictionnaire de parenté
        parent_map = self._find_parent_relationships(list_types, table_names)
        print(f"  → Relations parent-enfant : {parent_map}")

        root_element = self.root.find(f"{XS}element")
        root_type_name = root_element.get("name", "ROOT").upper() if root_element is not None else "ROOT"

        for type_name, type_info in self.complex_types.items():
            table_name = type_name.upper().replace("TYPE", "").strip("_")
            if type_name in list_types or table_name == root_type_name:
                table = {
                    "table_name": table_name,
                    "original_type": type_name,
                    "columns": [],
                    "children": type_info["children"],
                }
                table["columns"].append({
                    "name": f"id_{table_name.lower()}",
                    "sql_type": "NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
                    "nullable": False,
                })
                # Colonne FK vers la table parente (si elle existe)
                if type_name in parent_map:
                    parent_type = parent_map[type_name]
                    parent_table = parent_type.upper().replace("TYPE", "").strip("_")
                    table["columns"].append({
                        "name": f"id_{parent_table.lower()}_fk",
                        "sql_type": f"NUMBER REFERENCES {parent_table}(id_{parent_table.lower()})",
                        "nullable": False,
                    })
                    table["parent_table"] = parent_table
                flat_fields = self._flatten_type(type_name)
                for field in flat_fields:
                    nullable_str = "" if field["nullable"] else " NOT NULL"
                    table["columns"].append({
                        "name": field["name"].lower(),
                        "sql_type": field["oracle_type"] or "VARCHAR2(255)",
                        "nullable": field["nullable"],
                        "constraint": nullable_str,
                    })
                self.tables.append(table)  # ← ligne manquante ajoutée


# ---------------------------------------------------------------
# TEST RAPIDE
# ---------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python xsd_parser.py <chemin_vers_xsd>")
        sys.exit(1)
    parser = XSDParser(sys.argv[1])
    tables = parser.parse()
    print("\n=== RÉSULTAT ===")
    for table in tables:
        print(f"\nTable : {table['table_name']}")
        for col in table['columns']:
            print(f"  - {col['name']} : {col['sql_type']}")