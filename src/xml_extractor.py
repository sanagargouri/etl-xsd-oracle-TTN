# xml_extractor.py
from lxml import etree

class XMLExtractor:
    def __init__(self, xml_path, tables, tag_map):
        """
        xml_path : chemin vers le fichier XML à lire
        tables   : liste de tables produite par xsd_parser.py
        """
        self.xml_path = xml_path
        self.tables = tables
        self.tag_map = tag_map 
        self.data = {}  # {nom_table: [liste de lignes]}

        # Construit un index des tables par nom pour accès rapide
        self.tables_index = {t["table_name"]: t for t in tables}

    def extract(self):
        """
        Point d'entrée principal.
        Lit le XML et extrait les données pour chaque table.
        """
        print(f"\nExtraction du XML : {self.xml_path}")

        # Parse le fichier XML avec lxml
        tree = etree.parse(self.xml_path)
        root = tree.getroot()

        # Initialise le dictionnaire de données
        for table in self.tables:
            self.data[table["table_name"]] = []

        # Extrait les données de l'élément racine (TEIF)
        root_table = self._find_root_table()
        if root_table:
            root_row = self._extract_attributes(root, root_table)
            self.data[root_table["table_name"]].append(root_row)
            print(f"  → {root_table['table_name']} : 1 ligne extraite")

        # Extrait les données de chaque table enfant
        for table in self.tables:
            if "parent_table" in table:
                rows = self._extract_table_data(root, table)
                self.data[table["table_name"]] = rows
                print(f"  → {table['table_name']} : {len(rows)} ligne(s) extraite(s)")

        return self.data

    def _find_root_table(self):
        """Trouve la table racine (sans parent)."""
        for table in self.tables:
            if "parent_table" not in table:
                return table
        return None

    def _extract_attributes(self, elem, table):
        """
        Extrait les attributs XML d'un élément
        et les mappe aux colonnes de la table.
        
        Ex: <TEIF version="1.8.8"> → {attr_version: "1.8.8"}
        """
        row = {}
        for col in table["columns"]:
            col_name = col["name"]
            # Les colonnes d'attributs commencent par "attr_"
            if col_name.startswith("attr_"):
                attr_name = col_name[5:]  # supprime le préfixe "attr_"
                # Cherche l'attribut dans l'élément XML
                # (insensible à la casse)
                for xml_attr, val in elem.attrib.items():
                    if xml_attr.lower() == attr_name.lower():
                        row[col_name] = val
                        break
        return row

    def _find_elements(self, root, tag_name):
        """
        Cherche tous les éléments avec un nom de balise donné
        dans tout le XML (insensible à la casse et aux namespaces).
        """
        results = []
        # Parcourt tout l'arbre XML
        for elem in root.iter():
            # Supprime le namespace si présent (ex: {http://...}TagName)
            local_name = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local_name.lower() == tag_name.lower():
                results.append(elem)
        return results

    def _extract_table_data(self, root, table):
        """
        Extrait toutes les lignes de données pour une table donnée.
        Utilise tag_map pour trouver le bon nom de balise XML.
        """
        rows = []
        original_type = table.get("original_type", "")

        # Utilise tag_map pour trouver le vrai nom de balise XML
        xml_tag = self.tag_map.get(original_type, "")
        if not xml_tag:
            return rows

        # Cherche tous les éléments correspondants dans le XML
        elements = self._find_elements(root, xml_tag)

        for elem in elements:
            row = {}

            # Extrait les attributs XML
            for xml_attr, val in elem.attrib.items():
                col_name = f"attr_{xml_attr.lower()}"
                if col_name in [c["name"] for c in table["columns"]]:
                    row[col_name] = val

            # Extrait les valeurs des sous-éléments
            for col in table["columns"]:
                col_name = col["name"]
                if col_name.startswith("id_") or col_name.startswith("attr_"):
                    continue
                value = self._find_column_value(elem, col_name)
                if value is not None:
                    row[col_name] = value

            if row:
                rows.append(row)

        return rows

    def _find_column_value(self, elem, col_name):
        """
        Cherche la valeur d'une colonne dans un élément XML.
        Le nom de la colonne peut être un chemin préfixé
        ex: "linimd_itemcode" → cherche ItemCode dans LinImd
        """
        # Décompose le nom de la colonne en segments
        # ex: "linimd_itemcode" → ["linimd", "itemcode"]
        parts = col_name.split("_")

        if len(parts) == 1:
            # Colonne simple → cherche directement
            child = self._find_child(elem, parts[0])
            if child is not None:
                return child.text
        else:
            # Colonne préfixée → navigue dans les sous-éléments
            current = elem
            for i, part in enumerate(parts[:-1]):
                child = self._find_child(current, part)
                if child is None:
                    return None
                current = child
            # Dernier segment → valeur
            last_child = self._find_child(current, parts[-1])
            if last_child is not None:
                return last_child.text

        return None

    def _find_child(self, elem, tag_name):
        """
        Cherche un enfant direct d'un élément par nom de balise
        (insensible à la casse).
        """
        for child in elem:
            local_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local_name.lower() == tag_name.lower():
                return child
        return None

    def print_summary(self):
        """Affiche un résumé des données extraites."""
        print("\n=== RÉSUMÉ EXTRACTION ===")
        for table_name, rows in self.data.items():
            if rows:
                print(f"\nTable {table_name} ({len(rows)} ligne(s)) :")
                for i, row in enumerate(rows[:2]):  # affiche max 2 lignes
                    print(f"  Ligne {i+1} : {row}")
                if len(rows) > 2:
                    print(f"  ... et {len(rows)-2} autre(s) ligne(s)")


# ---------------------------------------------------------------
# TEST RAPIDE
# ---------------------------------------------------------------
if __name__ == "__main__":
    import sys
    sys.path.append("src")
    from xsd_parser import XSDParser

    if len(sys.argv) < 3:
        print("Usage: python src/xml_extractor.py <xsd> <xml>")
        sys.exit(1)

    # Étape 1 : parser le XSD
    print("=== ÉTAPE 1 : Parsing du XSD ===")
    parser = XSDParser(sys.argv[1])
    tables, tag_map = parser.parse()  # ← récupère aussi tag_map

    # Étape 2 : extraire les données du XML
    print("\n=== ÉTAPE 2 : Extraction du XML ===")
    extractor = XMLExtractor(sys.argv[2], tables, tag_map)  # ← passe tag_map
    data = extractor.extract()
    extractor.print_summary()