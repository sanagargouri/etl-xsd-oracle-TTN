# xsd_parser.py
import xml.etree.ElementTree as ET

# Namespace XSD — toutes les balises xs:* ont ce préfixe en Python
XS = "{http://www.w3.org/2001/XMLSchema}"

# Correspondance entre les contraintes XSD et les types Oracle SQL
# C'est le "dictionnaire de traduction" XSD → SQL
TYPE_MAPPING = {
    "string":  ("VARCHAR2", 255),   # par défaut si pas de maxLength
    "integer": ("NUMBER", None),
    "decimal": ("NUMBER", None),
    "boolean": ("VARCHAR2", 5),     # Oracle n'a pas de type booléen natif
    "date":    ("DATE", None),
    "dateTime":("TIMESTAMP", None),
}

class XSDParser:
    def __init__(self, xsd_path):
        """
        Charge et parse le fichier XSD.
        xsd_path : chemin vers le fichier .xsd
        """
        self.xsd_path = xsd_path
        self.tree = ET.parse(xsd_path)
        self.root = self.tree.getroot()

        # Dictionnaire des types simples définis dans le XSD
        # ex: {"DataStringType_35": "VARCHAR2(35)", "monetaryAmountType": "NUMBER(15,5)"}
        self.simple_types = {}

        # Dictionnaire des types complexes définis dans le XSD
        # ex: {"LinType": {"fields": [...], "children": [...]}}
        self.complex_types = {}

        # Résultat final : liste de tables à créer
        # ex: [{"table_name": "LIN", "columns": [...], "parent": "FACTURES"}]
        self.tables = []

    def parse(self):
        
        print(f"Analyse du XSD : {self.xsd_path}")
        self._parse_simple_types()
        print(f"  → {len(self.simple_types)} types simples trouvés")
        self._parse_complex_types()
        print(f"  → {len(self.complex_types)} types complexes trouvés")
        self._parse_root_element()   # ← ajoute cette ligne
        self._build_tables()
        print(f"  → {len(self.tables)} tables à générer")
        return self.tables

    # ---------------------------------------------------------------
    # ÉTAPE 1 : Types simples
    # ---------------------------------------------------------------
    def _parse_simple_types(self):
        """
        Parcourt tous les <xs:simpleType> du XSD et les traduit en types Oracle.
        Exemple :
            <xs:simpleType name="DataStringType_35">
                <xs:restriction base="xs:string">
                    <xs:maxLength value="35"/>
                </xs:restriction>
            </xs:simpleType>
        → self.simple_types["DataStringType_35"] = "VARCHAR2(35)"
        """
        for simple_type in self.root.findall(f".//{XS}simpleType[@name]"):
            name = simple_type.get("name")
            oracle_type = self._resolve_simple_type(simple_type)
            self.simple_types[name] = oracle_type

    def _resolve_simple_type(self, node):
        """
        Traduit un noeud <xs:simpleType> en type Oracle concret.
        """
        restriction = node.find(f"{XS}restriction")
        if restriction is None:
            return "VARCHAR2(255)"  # type par défaut si pas de restriction

        base = restriction.get("base", "xs:string").replace("xs:", "")

        # Cherche une contrainte de longueur max
        max_length = restriction.find(f"{XS}maxLength")
        if max_length is not None:
            length = int(max_length.get("value"))
            return f"VARCHAR2({length})"

        # Cherche un pattern numérique (ex: montants)
        pattern = restriction.find(f"{XS}pattern")
        if pattern is not None:
            val = pattern.get("value", "")
            # Si le pattern ressemble à un nombre (contient des chiffres et points)
            if "[0-9]" in val and ("." in val or "," in val):
                return "NUMBER(15,5)"
            if "[0-9]" in val:
                return "NUMBER"

        # Sinon, utilise la correspondance de base
        base_type, _ = TYPE_MAPPING.get(base, ("VARCHAR2", 255))
        return f"{base_type}(255)"

    # ---------------------------------------------------------------
    # ÉTAPE 2 : Types complexes
    # ---------------------------------------------------------------
    def _parse_complex_types(self):
        """
        Parcourt tous les <xs:complexType> du XSD.
        Pour chaque type complexe, extrait :
        - ses champs simples (futurs colonnes)
        - ses enfants complexes (futures tables liées ou colonnes imbriquées)
        - ses attributs XML (aussi de futurs colonnes)
        """
        for complex_type in self.root.findall(f".//{XS}complexType[@name]"):
            name = complex_type.get("name")
            self.complex_types[name] = self._extract_type_info(complex_type)
    def _parse_root_element(self):
        """
        Cherche l'élément racine du XSD (le premier xs:element enfant direct
        de xs:schema) et crée une table pour lui.
        C'est la table principale — ex: TEIF → table FACTURES
        """
        # Cherche le premier xs:element enfant direct de la racine du schéma
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

        # Colonne PK
        table["columns"].append({
            "name": f"id_{root_name.lower()}",
            "sql_type": "NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
            "nullable": False,
        })

        # Cherche le xs:complexType anonyme à l'intérieur de cet élément
        anon_type = root_elem.find(f"{XS}complexType")
        if anon_type is not None:
            # Extrait les attributs XML (ex: version, controlingAgency)
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

            # Extrait les éléments enfants directs (InvoiceHeader, InvoiceBody...)
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
        Extrait les informations d'un type complexe :
        retourne un dict {"fields": [...], "children": [...]}
        """
        fields = []    # colonnes simples (VARCHAR2, NUMBER, DATE...)
        children = []  # sous-éléments complexes (futures tables ou colonnes imbriquées)

        # Cherche les éléments dans la séquence
        for elem in node.findall(f".//{XS}element"):
            field_info = self._extract_element_info(elem)
            if field_info:
                if field_info["is_complex"]:
                    children.append(field_info)
                else:
                    fields.append(field_info)

        # Cherche aussi les attributs XML (ex: functionCode="I-62")
        for attr in node.findall(f".//{XS}attribute"):
            attr_info = self._extract_attribute_info(attr)
            if attr_info:
                fields.append(attr_info)

        return {"fields": fields, "children": children}

    def _extract_element_info(self, elem):
        """
        Extrait les infos d'un <xs:element> individuel.
        Détermine si c'est un champ simple ou un enfant complexe.
        """
        name = elem.get("name")
        type_ref = elem.get("type", "")
        min_occurs = int(elem.get("minOccurs", "1"))
        max_occurs = elem.get("maxOccurs", "1")

        if not name:
            return None

        is_list = (max_occurs == "unbounded")
        is_nullable = (min_occurs == 0)

        # Est-ce un type complexe connu ?
        is_complex = (
            type_ref in self.complex_types or
            type_ref.replace("xs:", "") not in TYPE_MAPPING
        )

        # Résolution du type Oracle si c'est un type simple
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
        """
        Extrait les infos d'un <xs:attribute>.
        Les attributs XML deviennent des colonnes dans la table.
        Exemple : <xs:attribute name="functionCode" use="required"/>
        → colonne "functioncode" VARCHAR2(255) NOT NULL
        """
        name = attr.get("name")
        use = attr.get("use", "optional")
        type_ref = attr.get("type", "")

        if not name:
            return None

        oracle_type = self._resolve_type_ref(type_ref) if type_ref else "VARCHAR2(255)"

        return {
            "name": f"attr_{name}",   # préfixe "attr_" pour distinguer des éléments
            "type_ref": type_ref,
            "oracle_type": oracle_type,
            "is_complex": False,
            "is_list": False,
            "nullable": (use != "required"),
        }

    def _resolve_type_ref(self, type_ref):
        """
        Résout une référence de type (ex: "DataStringType_35", "xs:string")
        en type Oracle concret.
        """
        # Type défini dans le XSD (nos types simples custom)
        if type_ref in self.simple_types:
            return self.simple_types[type_ref]

        # Type XSD natif (xs:string, xs:integer...)
        base = type_ref.replace("xs:", "")
        sql_type, length = TYPE_MAPPING.get(base, ("VARCHAR2", 255))
        if length:
            return f"{sql_type}({length})"
        return sql_type
    

    def _build_tables(self):
            """
            Décide quoi devient une table Oracle et quoi devient une simple colonne.
            Règle :
            - maxOccurs="unbounded" → table séparée avec FK vers la table parente
            - maxOccurs="1"         → champs absorbés (inline) dans la table parente
            """
            # On ne crée une table que pour les types complexes qui sont
            # référencés avec maxOccurs="unbounded" quelque part dans le XSD,
            # ou qui sont l'élément racine
            
            # Étape 1 : identifier quels types sont des "listes" (maxOccurs="unbounded")
            list_types = set()  # ensemble des types qui apparaissent en liste
            
            for elem in self.root.findall(f".//{XS}element"):
                max_occurs = elem.get("maxOccurs", "1")
                type_ref = elem.get("type", "")
                if max_occurs == "unbounded" and type_ref in self.complex_types:
                    list_types.add(type_ref)
            
            print(f"  → Types qui sont des listes (→ tables séparées) : {list_types}")
            
            # Étape 2 : ne créer une table que pour les types "liste"
            # + l'élément racine (TEIF dans notre cas)
            root_element = self.root.find(f"{XS}element")
            root_type_name = root_element.get("name", "ROOT").upper() if root_element is not None else "ROOT"
            
            for type_name, type_info in self.complex_types.items():
                # On crée une table seulement si :
                # - c'est un type "liste" (maxOccurs="unbounded")
                # - OU c'est le type racine
                table_name = type_name.upper().replace("TYPE", "").strip("_")
                
                if type_name in list_types or table_name == root_type_name:
                    table = {
                        "table_name": table_name,
                        "original_type": type_name,
                        "columns": [],
                        "children": type_info["children"],
                    }

                    # Colonne PK auto-générée
                    table["columns"].append({
                        "name": f"id_{table_name.lower()}",
                        "sql_type": "NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
                        "nullable": False,
                    })

                    # Colonnes issues des champs simples
                    for field in type_info["fields"]:
                        nullable_str = "" if field["nullable"] else " NOT NULL"
                        table["columns"].append({
                            "name": field["name"].lower(),
                            "sql_type": field["oracle_type"] or "VARCHAR2(255)",
                            "nullable": field["nullable"],
                            "constraint": nullable_str,
                        })

                    self.tables.append(table)


    

    


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