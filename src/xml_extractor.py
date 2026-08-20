from lxml import etree
from itertools import count

class XMLExtractor:
    def __init__(self, xml_path, tables, tag_map):
        self.xml_path = xml_path
        self.tables = tables
        self.tag_map = tag_map
        self.data = {}
        self.tables_index = {t["table_name"]: t for t in tables}
        # Balises XML qui correspondent à une table NON racine (ex: ARTICLE,
        # PIECES_JOINTE, MINISTERE_COMMERCE_OBSERVATION...). Utilisées comme
        # "frontières" par _find_elements : on ne descend jamais dedans en
        # cherchant une autre balise, pour éviter qu'une balise imbriquée
        # dans un ARTICLE ne soit remontée par erreur au niveau DOCUMENT
        # (cas réel : MINISTERE_COMMERCE_OBSERVATION existe à la fois sous
        # DOCUMENT et sous ARTICLE dans tce.xsd).
        self.boundary_tags = {
            tag_map.get(t.get("original_type", ""))
            for t in tables
            if t.get("parent_table") and tag_map.get(t.get("original_type", ""))
        }

    def extract(self):
        print(f"\nExtraction du XML : {self.xml_path}")
        tree = etree.parse(self.xml_path)
        root = tree.getroot()
        for table in self.tables:
            self.data[table["table_name"]] = []
        self.id_counters = {}
        root_table = self._find_root_table()
        if root_table:
            self._extract_element_recursive(root, root_table, parent_local_id=None)
        for table in self.tables:
            n = len(self.data[table["table_name"]])
            print(f"  → {table['table_name']} : {n} ligne(s) extraite(s)")
        return self.data

    def _find_root_table(self):
        for table in self.tables:
            if "parent_table" not in table:
                return table
        return None

    def _next_local_id(self, table_name):
        if table_name not in self.id_counters:
            self.id_counters[table_name] = count(1)
        return next(self.id_counters[table_name])

    def _extract_element_recursive(self, elem, table, parent_local_id):
        row = self._extract_attributes(elem, table)
        for col in table["columns"]:
            col_name = col["name"]
            if col_name.startswith("id_") or col_name.startswith("attr_"):
                continue
            if col.get("is_fk_constraint"):
                continue
            if "xml_path" in col:
                value = self._find_value_by_path(elem, col["xml_path"])
            else:
                value = self._find_column_value(elem, col_name)
            if value is not None:
                row[col_name] = value

        local_id = self._next_local_id(table["table_name"])
        row["_local_id"] = local_id

        if parent_local_id is not None:
            row["_parent_local_id"] = parent_local_id

            parent_table_name = table.get("parent_table")
            if parent_table_name and parent_table_name in self.data:
                parent_rows = self.data[parent_table_name]
                parent_row = None

                for p_row in parent_rows:
                    if p_row.get("_local_id") == parent_local_id:
                        parent_row = p_row
                        break

                if parent_row:
                    parent_table_def = self.tables_index.get(parent_table_name)
                    if parent_table_def:
                        parent_pk = parent_table_def.get("primary_key", [])
                        for pk_col in parent_pk:
                            if pk_col in parent_row and pk_col not in row:
                                row[pk_col] = parent_row[pk_col]

        self.data[table["table_name"]].append(row)

        child_tables = [t for t in self.tables if t.get("parent_table") == table["table_name"]]
        for child_table in child_tables:
            original_type = child_table.get("original_type", "")
            xml_tag = self.tag_map.get(original_type, "")
            if not xml_tag:
                continue
            child_elements = self._find_elements(elem, xml_tag)
            for child_elem in child_elements:
                self._extract_element_recursive(child_elem, child_table, parent_local_id=local_id)
        return local_id

    def _extract_attributes(self, elem, table):
        row = {}
        for col in table["columns"]:
            col_name = col["name"]
            if col_name.startswith("attr_"):
                attr_name = col_name[5:]
                for xml_attr, val in elem.attrib.items():
                    if xml_attr.lower() == attr_name.lower():
                        row[col_name] = val
                        break
        return row

    def _find_elements(self, root, tag_name):
        """
        Cherche récursivement les descendants de `root` portant la balise
        `tag_name`, SANS descendre à l'intérieur d'un descendant qui est
        lui-même une instance d'une autre table (self.boundary_tags).

        Nécessaire car de simples conteneurs pluriels (ex: <ARTICLES>,
        <MINISTERE_COMMERCE_OBSERVATIONS>) ne sont pas des tables et
        doivent être traversés, alors que <ARTICLE> (qui EST une table)
        ne doit jamais être traversé ici : ses propres balises enfant
        (potentiellement de même nom qu'une balise recherchée au niveau
        DOCUMENT) sont gérées par l'appel récursif dédié à cet ARTICLE.
        """
        results = []

        def walk(elem):
            for child in elem:
                local_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if local_name.lower() == tag_name.lower():
                    results.append(child)
                    continue
                if local_name in self.boundary_tags:
                    # Frontière d'une autre table -> gérée par sa propre
                    # récursion, on ne descend pas dedans ici.
                    continue
                walk(child)

        walk(root)
        return results

    def _find_column_value(self, elem, col_name):
        parts = col_name.split("_")
        if len(parts) == 1:
            child = self._find_child(elem, parts[0])
            if child is not None:
                return child.text
        else:
            current = elem
            for part in parts[:-1]:
                child = self._find_child(current, part)
                if child is None:
                    return None
                current = child
            last_child = self._find_child(current, parts[-1])
            if last_child is not None:
                return last_child.text
        return None

    def _find_value_by_path(self, elem, xml_path):
        current = elem
        for tag in xml_path:
            child = self._find_child(current, tag)
            if child is None:
                return None
            current = child
        return current.text

    def _find_child(self, elem, tag_name):
        for child in elem:
            local_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local_name.lower() == tag_name.lower():
                return child
        return None

    def print_summary(self):
        print("\n=== RÉSUMÉ EXTRACTION ===")
        for table_name, rows in self.data.items():
            if rows:
                print(f"\nTable {table_name} ({len(rows)} ligne(s)) :")
                for i, row in enumerate(rows[:2]):
                    print(f"  Ligne {i+1} : {row}")