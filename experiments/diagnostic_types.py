"""
diagnostic_types.py

Script de diagnostic : pour chaque type complexe du XSD, calcule
des métriques structurelles afin de comprendre ce qui distingue
les types actuellement dans `forced_tables` des autres.

Usage :
    python diagnostic_types.py <chemin_vers_xsd>
"""
import xml.etree.ElementTree as ET
import sys

XS = "{http://www.w3.org/2001/XMLSchema}"

FORCED_TABLES = {
    "MoaDetailsType",
    "TaxDetailsType",
    "AlcDetailsType",
    "PytSegType",
    "CtaGrpType",
    "RefGrpType",
    "AdressesType",
    "LinType",
}


def analyze(xsd_path):
    tree = ET.parse(xsd_path)
    root = tree.getroot()

    complex_types = root.findall(f".//{XS}complexType[@name]")
    type_names = {ct.get("name") for ct in complex_types}

    # 1) Combien de fois chaque type est référencé comme enfant (type="...")
    reference_count = {name: 0 for name in type_names}
    for elem in root.findall(f".//{XS}element"):
        type_ref = elem.get("type", "")
        if type_ref in reference_count:
            reference_count[type_ref] += 1
        for alt in elem.findall(f"{XS}alternative"):
            alt_type = alt.get("type", "")
            if alt_type in reference_count:
                reference_count[alt_type] += 1

    # 2) Pour chaque type complexe : nb de champs directs, nb d'enfants
    #    complexes imbriqués, présence de xs:simpleContent, maxOccurs=unbounded
    rows = []
    for ct in complex_types:
        name = ct.get("name")

        # is_list : au moins un endroit où ce type est référencé avec
        # maxOccurs="unbounded"
        is_list = False
        for elem in root.findall(f".//{XS}element"):
            if elem.get("type", "") == name and elem.get("maxOccurs", "1") == "unbounded":
                is_list = True

        simple_content = ct.find(f"{XS}simpleContent") is not None

        direct_fields = 0
        nested_complex_children = 0
        for elem in ct.findall(f".//{XS}element"):
            elem_type = elem.get("type", "")
            direct_fields += 1
            if elem_type in type_names:
                nested_complex_children += 1

        rows.append({
            "name": name,
            "forced": name in FORCED_TABLES,
            "is_list": is_list,
            "ref_count": reference_count.get(name, 0),
            "direct_fields": direct_fields,
            "nested_complex_children": nested_complex_children,
            "simple_content": simple_content,
        })

    return rows


def print_report(rows):
    header = f"{'TYPE':32} {'FORCED':7} {'LIST':5} {'REFS':5} {'FIELDS':7} {'NESTED':7} {'SIMPLECONTENT':13}"
    print(header)
    print("-" * len(header))

    # Trie : forced d'abord, puis par nb de références décroissant
    rows_sorted = sorted(rows, key=lambda r: (not r["forced"], -r["ref_count"]))

    for r in rows_sorted:
        print(
            f"{r['name']:32} "
            f"{str(r['forced']):7} "
            f"{str(r['is_list']):5} "
            f"{r['ref_count']:5} "
            f"{r['direct_fields']:7} "
            f"{r['nested_complex_children']:7} "
            f"{str(r['simple_content']):13}"
        )

    print("\n=== Moyennes ===")
    forced_rows = [r for r in rows if r["forced"] and not r["is_list"]]
    other_rows = [r for r in rows if not r["forced"] and not r["is_list"]]

    def avg(lst, key):
        return sum(r[key] for r in lst) / len(lst) if lst else 0

    print(f"Types forcés (hors listes)   : n={len(forced_rows)}  "
          f"refs_moy={avg(forced_rows,'ref_count'):.1f}  "
          f"fields_moy={avg(forced_rows,'direct_fields'):.1f}  "
          f"nested_moy={avg(forced_rows,'nested_complex_children'):.1f}")
    print(f"Autres types (hors listes)   : n={len(other_rows)}  "
          f"refs_moy={avg(other_rows,'ref_count'):.1f}  "
          f"fields_moy={avg(other_rows,'direct_fields'):.1f}  "
          f"nested_moy={avg(other_rows,'nested_complex_children'):.1f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python diagnostic_types.py <chemin_vers_xsd>")
        sys.exit(1)

    rows = analyze(sys.argv[1])
    print_report(rows)
