# xml_extractor.py
from lxml import etree
from itertools import count

class XMLExtractor:
    def __init__(self, xml_path, tables, tag_map):
        self.xml_path = xml_path
        self.tables = tables
        self.tag_map = tag_map
        self.data = {}
        self.tables_index = {t["table_name"]: t for t in tables}

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
        results = []
        for elem in root.iter():
            local_name = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local_name.lower() == tag_name.lower():
                results.append(elem)
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
        """
        Résout la valeur d'une colonne à partir d'un chemin explicite de
        balises XML (liste de noms), sans jamais découper sur '_'.
        Utilisé par le parseur TCE, dont les noms de balises contiennent
        eux-mêmes des underscores (ex: REFERENCE_TTN).
        """
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
