import sys
sys.path.append("src")
from xsd_parser import XSDParser

parser = XSDParser(sys.argv[1])
tables, tag_map = parser.parse()

tables_index = {t["table_name"]: t for t in tables}

for name in ["FTXDETAIL", "LOC", "API", "ALCDETAILS"]:
    t = tables_index[name]
    original_type = t.get("original_type", "")
    xml_tag = tag_map.get(original_type, "INTROUVABLE")
    parent = t.get("parent_table", "AUCUN (racine)")
    print(f"{name:15} type={original_type:20} balise_xml={xml_tag:15} parent={parent}")
    print(f"                colonnes: {[c['name'] for c in t['columns']]}")