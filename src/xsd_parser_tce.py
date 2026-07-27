# xsd_parser_tce.py
"""
Parseur XSD dédié aux schémas de type "tce.xsd" (TTN - messages génériques
DOCUMENT / TYPE_DOCUMENT = Z06, Z13, Z19, Z76, ...).

Contrairement à facture_INVOIC (TEIF), ce type de XSD n'utilise AUCUN
xs:complexType nommé : chaque élément est décrit "en ligne", imbriqué
directement dans son parent via <xs:complexType><xs:all>...</xs:all></xs:complexType>
(ou parfois <xs:sequence>). Il n'y a donc pas de "types" à réutiliser :
un seul élément racine <DOCUMENT> qui contient tout, récursivement.

Stratégie :
- Un élément devient une TABLE séparée (avec FK vers son parent) si et
  seulement si maxOccurs="unbounded" (ex: ARTICLE, PIECES_JOINTE,
  MINISTERE_COMMERCE_OBSERVATION...).
- Sinon, si l'élément a lui-même des enfants (c'est un groupe, ex:
  REFERENCE_TTN, ROUTAGE, EXPORTATEUR...), ses champs sont "aplatis"
  dans la table courante avec un préfixe (ex: exportateur_raison_sociale),
  exactement comme _flatten_type le fait déjà pour TEIF.
- Sinon, c'est une colonne simple.

La sortie (tables, tag_map) a exactement le même format que xsd_parser.py,
donc xml_extractor.py / table_generator.py / data_loader.py n'ont RIEN à
changer pour consommer ce parseur.
"""

import xml.etree.ElementTree as ET

XS = "{http://www.w3.org/2001/XMLSchema}"


class XSDParserTCE:
    def __init__(self, xsd_path):
        self.xsd_path = xsd_path
        self.tree = ET.parse(xsd_path)
        self.root = self.tree.getroot()
        self.tables = []
        self.tag_map = {}          # {table_name: nom_de_balise_xml_original}
        self.used_table_names = set()

    # -----------------------------------------------------------------
    # Point d'entrée public — même contrat que XSDParser.parse()
    # -----------------------------------------------------------------
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
        )

        print(f"  → {len(self.tables)} table(s) à générer (mode TCE)")
        print(f"  → {len(self.tag_map)} correspondance(s) type→balise trouvées")

        return self.tables, self.tag_map

    # -----------------------------------------------------------------
    # Construction récursive d'une table
    # -----------------------------------------------------------------
    def _build_table(self, elem, table_name, parent_table, xml_tag):
        table_name = self._register_table_name(table_name, parent_table)

        table = {
            "table_name": table_name,
            "original_type": table_name,   # sert de clé dans tag_map (cf. xml_extractor)
            "columns": [],
            "children": [],
        }

        # Colonne PK
        table["columns"].append({
            "name": f"id_{table_name.lower()}",
            "sql_type": "NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
            "nullable": False,
        })

        # Colonne FK si cette table a un parent
        if parent_table:
            fk_name = self._truncate_name(f"id_{parent_table.lower()}_fk")
            table["columns"].append({
                "name": fk_name,
                "sql_type": f"NUMBER REFERENCES {parent_table}(id_{parent_table.lower()})",
                "nullable": False,
            })
            table["parent_table"] = parent_table

        self.tag_map[table_name] = xml_tag

        used_col_names = {c["name"] for c in table["columns"]}
        self._flatten_children(elem, table, table_name, prefix="", used_col_names=used_col_names)

        self.tables.append(table)
        return table_name

    def _flatten_children(self, elem, table, table_name, prefix, used_col_names, xml_path=None):
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
                # Devient une table enfant à part entière (avec FK)
                self._build_table(
                    elem=child,
                    table_name=name.upper(),
                    parent_table=table_name,
                    xml_tag=name,
                )
            elif grandchildren:
                # Groupe non répété -> on aplatit dans la table courante.
                # IMPORTANT : contrairement à TEIF, les balises TCE contiennent
                # déjà des underscores (ex: REFERENCE_TTN, PAYS_ORIGINE), donc
                # on ne peut pas retrouver le chemin XML en redécoupant le nom
                # de colonne sur "_" (ambiguïté). On garde donc le vrai chemin
                # de balises XML (xml_path) à côté du nom de colonne SQL.
                new_prefix = f"{prefix}{name.lower()}_"
                self._flatten_children(
                    child, table, table_name, new_prefix, used_col_names,
                    xml_path=xml_path + [name],
                )
            else:
                # Colonne simple (feuille)
                col_name = self._truncate_name(f"{prefix}{name.lower()}", used_col_names)
                table["columns"].append({
                    "name": col_name,
                    "sql_type": self._infer_type(child),
                    "nullable": nullable,
                    "xml_path": xml_path + [name],   # chemin réel des balises XML
                })

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------
    def _get_children(self, elem):
        """
        Retourne les <xs:element> enfants DIRECTS d'un élément, en passant
        par <xs:complexType><xs:all|xs:sequence>. Ignore tout le reste
        (attributs, annotations...).
        """
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
        """
        Détermine le type Oracle d'une colonne feuille.
        Les dates TTN (ex: 20260722) et les codes numériques (ex: NUMERO_COMPTE)
        sont volontairement gardés en VARCHAR2 : ce sont des identifiants /
        codes, pas des valeurs arithmétiques, et certains ont des zéros non
        significatifs à préserver.

        IMPORTANT : les données réelles TTN dépassent parfois la longueur
        déclarée dans le XSD (constaté en pratique : NUMERO_MESSAGE déclaré
        maxLength=14 mais valeur réelle de 23 caractères). Le XSD n'est donc
        pas une garantie fiable à 100% -> on applique une marge de sécurité
        généreuse plutôt que la longueur exacte, pour éviter des échecs
        d'insertion (ORA-12899) sur des données par ailleurs valides.
        """
        SAFETY_MARGIN_FACTOR = 3   # multiplie la longueur déclarée
        SAFETY_MARGIN_MIN = 50     # marge plancher même pour les champs courts
        MAX_VARCHAR2 = 4000        # limite Oracle standard (hors CLOB)

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
        """
        Évite les collisions de noms de table : certains éléments répétés
        (ex: MINISTERE_COMMERCE_OBSERVATION, BANQUE_OBSERVATION) apparaissent
        à la fois au niveau DOCUMENT et au niveau ARTICLE. En cas de collision,
        préfixe avec le nom du parent.
        """
        candidate = self._truncate_name(base_name.replace("-", "_"))

        if candidate not in self.used_table_names:
            self.used_table_names.add(candidate)
            return candidate

        if parent_table:
            candidate2 = self._truncate_name(f"{parent_table}_{base_name}")
            if candidate2 not in self.used_table_names:
                self.used_table_names.add(candidate2)
                return candidate2

        # Dernier recours : suffixe numérique
        counter = 2
        candidate3 = candidate
        while candidate3 in self.used_table_names:
            suffix = f"_{counter}"
            candidate3 = self._truncate_name(candidate[: 30 - len(suffix)] + suffix)
            counter += 1
        self.used_table_names.add(candidate3)
        return candidate3

    def _truncate_name(self, name, used_names=None, max_length=30):
        """
        Identique à la logique de xsd_parser.py : raccourcit un nom trop
        long en gardant le suffixe (nom du champ final) intact.
        """
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


# ---------------------------------------------------------------
# TEST RAPIDE
# ---------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import sys
    if len(sys.argv) < 2:
        print("Usage: python xsd_parser_tce.py <chemin_vers_tce.xsd>")
        sys.exit(1)
    parser = XSDParserTCE(sys.argv[1])
    tables, tag_map = parser.parse()
    print("\n=== RÉSULTAT ===")
    for table in tables:
        parent = table.get("parent_table", "(racine)")
        print(f"\nTable : {table['table_name']}  (parent: {parent})")
        for col in table["columns"]:
            print(f"  - {col['name']} : {col['sql_type']}")