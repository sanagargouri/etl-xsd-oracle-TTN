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
    # ------------------------------------------------------------------
    # CLÉS NATURELLES (même principe que xsd_parser_tce.py)
    # ------------------------------------------------------------------
    # Clé de la table racine TEIF : DocumentIdentifier = le numéro de
    # facture, situé dans le XSD à InvoiceBody(BodyType) > Bgm(BgmType) >
    # DocumentIdentifier. Il est déclaré obligatoire et nillable="false",
    # donc toujours présent (contrairement à NUMERO_DOSSIER côté TCE).
    ROOT_KEY_COLUMN_NAME = "document_identifier"
    ROOT_KEY_XML_PATH = ["InvoiceBody", "Bgm", "DocumentIdentifier"]

    # Discriminant naturel par type XSD répété, à l'intérieur d'une même
    # facture. Une table absente de ce dict n'a pas (encore) de
    # discriminant naturel identifié et garde son ID généré, exactement
    # comme les tables *_OBSERVATION côté TCE -- exception assumée.
    NATURAL_DISCRIMINANTS = {
        "LinType": "itemidentifier",   # ItemIdentifier = numéro de ligne

        # Les trois suivants reposent sur un attribut OBLIGATOIRE
        # (use="required") dont le XSD énumère les valeurs possibles :
        # c'est un code de rôle, pas une donnée libre. Un même rôle n'a
        # de sens qu'une fois par facture (un seul vendeur I-62, un seul
        # acheteur I-64, une seule date d'émission I-31...).
        # ATTENTION : justification sémantique tirée du XSD, vérifiée sur
        # une seule facture réelle (facture_001) faute d'autres exemples.
        # Si un jour un ORA-00001 apparaît sur l'une de ces tables, c'est
        # que l'hypothèse est fausse pour ce cas : retirer la ligne
        # correspondante ici suffit à revenir à l'ID généré.
        "PartDetailType": "attr_functioncode",       # I-62 vendeur, I-64 acheteur
        "PartDetailTestType": "attr_functioncode",
        "DtmDetailType": "attr_functioncode",        # I-31 émission, I-32 échéance...
        "PytSegType": "pyt_paymenttearmstypecode",   # I-114, I-115...

        # Enfants de PARTDETAIL -- possibles seulement depuis que
        # PARTDETAIL a lui-même une clé naturelle composite.
        # Reference : NotNullDataStringType_200, obligatoire, et
        # constaté unique à l'intérieur de chaque partenaire.
        "RefGrpType": "reference",

        # PAS de clé naturelle pour :
        #  - LocType : son discriminant naturel serait functionCode, mais
        #    la colonne porte le MÊME NOM (attr_functioncode) que la clé
        #    héritée de son parent PARTDETAIL. Les deux fusionneraient en
        #    une seule colonne et une valeur écraserait l'autre. Il
        #    faudrait d'abord préfixer les colonnes propres pour lever
        #    l'ambiguïté.
        #  - AdressesType : maxOccurs unbounded et champs libres
        #    optionnels (rue, ville...) -- rien d'unique garanti.
        #  - CtaGrpType : seul champ distinctif = numéro de téléphone,
        #    optionnel ; ContactIdentifier se répète (3x "TTN").
        #  - MoaDetailsType : montant répété (doublon constaté).
        #  - TaxDetailsType, AlcDetailsType, FtxDetailType, ApiType :
        #    aucun discriminant obligatoire identifié.
        # Ces tables gardent un ID généré : exception assumée, faute de
        # clé naturelle disponible dans le XSD.
    }

    def __init__(self, xsd_path):
        self.xsd_path = xsd_path
        self.tree = ET.parse(xsd_path)
        self.root = self.tree.getroot()
        self.simple_types = {}
        self.complex_types = {}
        self.tables = []
        self.root_key_sql_type = None
        self.root_table_name = None

    def parse(self):
        print(f"Analyse du XSD : {self.xsd_path}")
        self._parse_simple_types()
        print(f"  → {len(self.simple_types)} types simples trouvés")
        self._parse_complex_types()
        print(f"  → {len(self.complex_types)} types complexes trouvés")
        self._parse_root_element()
        self._build_tables()
        print(f"  → {len(self.tables)} tables à générer")

        self.tag_map = self._build_tag_map()
        print(f"  → {len(self.tag_map)} correspondances type→balise trouvées")

        return self.tables, self.tag_map

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

    # ------------------------------------------------------------------
    # Clé naturelle de la racine
    # ------------------------------------------------------------------
    def _resolve_root_key_type(self):
        """
        Retourne le type Oracle de DocumentIdentifier tel que déclaré dans
        BgmType, ou None si le XSD ne le contient pas (schéma modifié).
        Dans ce dernier cas, _parse_root_element retombe explicitement sur
        l'ancien ID généré plutôt que de planter en silence.
        """
        bgm = self.complex_types.get("BgmType")
        if not bgm:
            return None
        for field in bgm["fields"]:
            if field["name"] == "DocumentIdentifier":
                return field["oracle_type"] or "VARCHAR2(70)"
        return None

    def _parse_root_element(self):
        root_elem = self.root.find(f"{XS}element")
        if root_elem is None:
            print("  → Aucun élément racine trouvé")
            return
        root_name = root_elem.get("name", "ROOT")
        self.root_table_name = root_name.upper()
        print(f"  → Élément racine trouvé : {root_name}")
        table = {
            "table_name": root_name.upper(),
            "original_type": root_name,
            "columns": [],
            "children": [],
        }

        # Clé naturelle au lieu d'un ID auto-incrémenté. xml_path est
        # fourni explicitement (comme côté TCE) pour que xml_extractor
        # résolve la valeur via le vrai chemin de balises, sans redécouper
        # le nom de colonne sur "_".
        root_key_type = self._resolve_root_key_type()
        if root_key_type:
            self.root_key_sql_type = root_key_type
            table["columns"].append({
                "name": self.ROOT_KEY_COLUMN_NAME,
                # Pas de "PRIMARY KEY" inline : la contrainte est posée au
                # niveau table via table["primary_key"] juste après (sinon
                # ORA-02260 : une seule clé primaire par table).
                "sql_type": root_key_type,
                "nullable": False,
                "xml_path": list(self.ROOT_KEY_XML_PATH),
                "full_name": "_".join(self.ROOT_KEY_XML_PATH),
            })
            table["primary_key"] = [self.ROOT_KEY_COLUMN_NAME]
            print(f"  → Clé naturelle racine : {self.ROOT_KEY_COLUMN_NAME} "
                  f"({' > '.join(self.ROOT_KEY_XML_PATH)})")
        else:
            print("  → DocumentIdentifier introuvable dans le XSD "
                  "-- ID généré conservé par exception")
            self.root_key_sql_type = None
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
                        "full_name": f"attr_{attr_name}",
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
        fields = []
        children = []

        simple_content = node.find(f"{XS}simpleContent")
        if simple_content is not None:
            extension = simple_content.find(f"{XS}extension")
            if extension is not None:
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
                for attr in extension.findall(f"{XS}attribute"):
                    attr_info = self._extract_attribute_info(attr)
                    if attr_info:
                        fields.append(attr_info)
            return {"fields": fields, "children": children}

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
        name = elem.get("name")
        type_ref = elem.get("type", "")
        min_occurs = int(elem.get("minOccurs", "1"))
        max_occurs = elem.get("maxOccurs", "1")

        if not name:
            return None

        is_list = (max_occurs == "unbounded")
        is_nullable = (min_occurs == 0)

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
        """
        Aplati récursivement un type complexe en une liste de champs.
        Chaque champ porte aussi 'full_name' : le nom complet AVANT
        troncature, utilisé par table_generator.py pour documenter via
        COMMENT ON COLUMN la colonne Oracle réellement créée.
        """
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
            f = field.copy()
            f["full_name"] = field["name"]
            flat_fields.append(f)
        for child in type_info["children"]:
            if not child["is_list"]:
                child_fields = self._flatten_type(child["type_ref"], visited.copy())
                for cf in child_fields:
                    prefixed_field = cf.copy()
                    prefixed_field["name"] = f"{child['name'].lower()}_{cf['name']}"
                    prefixed_field["full_name"] = f"{child['name']}_{cf.get('full_name', cf['name'])}"
                    flat_fields.append(prefixed_field)
        return flat_fields

    def _find_parent_relationships(self, list_types, table_names):
        direct_parent = {}

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

        root_name = None
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

                if parent in list_types or parent_table_name in table_names:
                    found_parent = parent
                    break

                if parent == root_name:
                    found_parent = root_name
                    break

                current = parent

            if found_parent is None and type_name != root_name:
                found_parent = root_name

            if found_parent:
                parent_map[type_name] = found_parent

        return parent_map

    def _truncate_name(self, name, max_length=30, used_names=None):
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
            truncated = original[:max_length - len(suffix)] + suffix
            counter += 1

        used_names.add(truncated)
        return truncated

    # ------------------------------------------------------------------
    # Attribution des clés (naturelles quand c'est possible)
    # ------------------------------------------------------------------
    def _assign_keys(self, table, type_name, parent_table_name):
        """
        Attribue à `table` sa clé de liaison vers son parent, puis sa clé
        primaire. Trois cas, dans cet ordre :

        1. Parent à clé naturelle SIMPLE (TEIF) -> FK = document_identifier
           (une vraie donnée, propagée par le loader).
        2. Parent à clé naturelle COMPOSITE (LIN) -> FK = toutes les
           colonnes de cette clé + une contrainte FK composite (même
           mécanisme que xsd_parser_tce.py, marquée is_fk_constraint pour
           que table_generator/data_loader la traitent comme une contrainte
           et non comme une colonne).
        3. Parent sans clé naturelle -> ancien mécanisme numérique
           (id_parent_fk REFERENCES parent(id_parent)), inchangé.

        Ensuite, si la table a un discriminant naturel connu
        (NATURAL_DISCRIMINANTS) -> clé primaire composite 100% naturelle.
        Sinon -> ID généré conservé, exception assumée et documentée.
        """
        parent_def = None
        parent_pk = None
        if parent_table_name:
            for t in self.tables:
                if t["table_name"] == parent_table_name:
                    parent_def = t
                    parent_pk = t.get("primary_key")
                    break

        # --- 1/2/3 : colonnes de liaison vers le parent ---
        if parent_table_name and parent_pk and len(parent_pk) > 1:
            for i, pk_col in enumerate(parent_pk):
                pk_type = "VARCHAR2(255)"
                for c in parent_def["columns"]:
                    if c["name"] == pk_col:
                        pk_type = c["sql_type"].replace("PRIMARY KEY", "").strip()
                        if "REFERENCES" in pk_type:
                            pk_type = pk_type.split("REFERENCES")[0].strip()
                        break
                if not any(c["name"] == pk_col for c in table["columns"]):
                    table["columns"].insert(i, {
                        "name": pk_col,
                        "sql_type": pk_type,
                        "nullable": False,
                        "is_propagated_key": True,
                    })
            fk_cols = ", ".join(parent_pk)
            table["columns"].append({
                "name": f"__fk_constraint_{parent_table_name}",
                "sql_type": (f"CONSTRAINT FK_{table['table_name']}_{parent_table_name} "
                             f"FOREIGN KEY ({fk_cols}) "
                             f"REFERENCES {parent_table_name}({fk_cols})")[:4000],
                "nullable": True,
                "is_fk_constraint": True,
            })
            table["parent_table"] = parent_table_name

        elif parent_table_name and parent_pk and len(parent_pk) == 1:
            key_col = parent_pk[0]
            key_type = "VARCHAR2(255)"
            for c in parent_def["columns"]:
                if c["name"] == key_col:
                    key_type = c["sql_type"].replace("PRIMARY KEY", "").strip()
                    if "REFERENCES" in key_type:
                        key_type = key_type.split("REFERENCES")[0].strip()
                    break
            if not any(c["name"] == key_col for c in table["columns"]):
                table["columns"].insert(0, {
                    "name": key_col,
                    "sql_type": f"{key_type} REFERENCES {parent_table_name}({key_col})",
                    "nullable": False,
                    "is_propagated_key": True,
                })
            table["parent_table"] = parent_table_name

        elif parent_table_name:
            # Parent resté sur un ID généré : comportement d'origine.
            fk_name = self._truncate_name(f"id_{parent_table_name.lower()}_fk")
            table["columns"].append({
                "name": fk_name,
                "sql_type": (f"NUMBER REFERENCES {parent_table_name}"
                             f"(id_{parent_table_name.lower()})"),
                "nullable": False,
            })
            table["parent_table"] = parent_table_name

        # --- Clé primaire de CETTE table ---
        discriminant = self.NATURAL_DISCRIMINANTS.get(type_name)
        has_discriminant = discriminant and any(
            c["name"] == discriminant for c in table["columns"]
        )

        if has_discriminant and parent_pk:
            table["primary_key"] = list(parent_pk) + [discriminant]
        else:
            if discriminant and not has_discriminant:
                print(f"    {table['table_name']} : discriminant '{discriminant}' "
                      f"absent des colonnes -- ID généré conservé")
            table["columns"].append({
                "name": f"id_{table['table_name'].lower()}",
                "sql_type": "NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
                "nullable": False,
            })

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

        table_names = set()
        for tn, _ in self.complex_types.items():
            tname = tn.upper().replace("TYPE", "").strip("_")
            if tn in list_types:
                table_names.add(tname)
        table_names.add("TEIF")

        parent_map = self._find_parent_relationships(list_types, table_names)
        print(f"  → Relations parent-enfant : {parent_map}")

        root_element = self.root.find(f"{XS}element")
        root_type_name = root_element.get("name", "ROOT").upper() if root_element is not None else "ROOT"

        # Types à transformer en tables, ordonnés parent AVANT enfant :
        # _assign_keys a besoin de la primary_key du parent (déjà dans
        # self.tables) pour décider du type de FK à poser. L'ordre du dict
        # des complexTypes ne le garantit pas -- on le calcule.
        candidates = [
            tn for tn in self.complex_types
            if tn in list_types
            or tn.upper().replace("TYPE", "").strip("_") == root_type_name
        ]

        def depth(type_name, seen=None):
            if seen is None:
                seen = set()
            if type_name in seen:
                return 0
            seen.add(type_name)
            parent = parent_map.get(type_name)
            if not parent or parent not in candidates:
                return 0
            return 1 + depth(parent, seen)

        candidates.sort(key=depth)

        for type_name in candidates:
            type_info = self.complex_types[type_name]
            table_name = type_name.upper().replace("TYPE", "").strip("_")
            table = {
                "table_name": table_name,
                "original_type": type_name,
                "columns": [],
                "children": type_info["children"],
            }

            flat_fields = self._flatten_type(type_name)
            for field in flat_fields:
                nullable_str = "" if field["nullable"] else " NOT NULL"
                col_name = self._truncate_name(field["name"].lower())
                table["columns"].append({
                    "name": col_name,
                    "sql_type": field["oracle_type"] or "VARCHAR2(255)",
                    "nullable": field["nullable"],
                    "constraint": nullable_str,
                    "full_name": field.get("full_name", field["name"]),
                })

            parent_type = parent_map.get(type_name)
            parent_table_name = None
            if parent_type:
                parent_table_name = parent_type.upper().replace("TYPE", "").strip("_")

            self._assign_keys(table, type_name, parent_table_name)
            self.tables.append(table)

    def _build_tag_map(self):
        tag_map = {}

        for elem in self.root.findall(f".//{XS}element"):
            type_ref = elem.get("type", "")
            elem_name = elem.get("name", "")

            if type_ref and elem_name and type_ref in self.complex_types:
                if type_ref not in tag_map:
                    tag_map[type_ref] = elem_name

            for alt in elem.findall(f"{XS}alternative"):
                alt_type = alt.get("type", "")
                if alt_type and elem_name and alt_type in self.complex_types:
                    if alt_type not in tag_map:
                        tag_map[alt_type] = elem_name

        return tag_map


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python xsd_parser.py <chemin_vers_xsd>")
        sys.exit(1)
    parser = XSDParser(sys.argv[1])
    tables, tag_map = parser.parse()
    print("\n=== RÉSULTAT ===")
    for table in tables:
        parent = table.get("parent_table", "(racine)")
        pk = table.get("primary_key", "(ID généré)")
        print(f"\nTable : {table['table_name']}  (parent: {parent})  PK: {pk}")
        for col in table['columns']:
            print(f"  - {col['name']} : {col['sql_type']}")