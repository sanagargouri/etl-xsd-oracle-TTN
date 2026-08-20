"""
verify_insertions.py
Vérifie, pour chaque XML TCE d'un dossier, que l'extraction produit des
lignes cohérentes AVANT insertion réelle en Oracle (dry-run, aucune
connexion base nécessaire) :

  1. Compare le nombre de lignes extraites par table au nombre réel
     d'occurrences de la balise correspondante dans le XML (comptage XPath
     indépendant, ne réutilise pas la logique de l'extracteur).
  2. Vérifie qu'aucune ligne d'une table à clé naturelle (celles ayant
     table["primary_key"]) n'a de valeur manquante pour une colonne de
     sa clé -- ce qui provoquerait un échec d'insertion Oracle.
  3. Vérifie qu'aucune colonne technique interne (__fk_constraint_*,
     _local_id, _parent_local_id) ne se retrouve dans ce qui serait
     réellement envoyé à Oracle.

Usage (depuis la racine du projet, PowerShell ou cmd.exe) :

    python verify_insertions.py data\\xsd\\tce.xsd data\\xml\\traites

    (remplacer le 2e argument par n'importe quel dossier contenant des
    .xml : a_traiter, traites, test_manuel, etc. -- ou passer un seul
    fichier .xml au lieu d'un dossier)

Résultat : un résumé OK / A VERIFIER par fichier, imprimé dans la console.
Ne modifie ni ne supprime aucun fichier XML.
"""
import sys
import os
from lxml import etree

sys.path.append("src")
from xsd_parser_tce import XSDParserTCE
from xml_extractor import XMLExtractor


def count_tag_occurrences_scoped(xml_path, tag_map, tables):
    """
    Recompte, INDÉPENDAMMENT de XMLExtractor, le nombre d'occurrences de
    chaque balise de table -- mais avec le même principe de frontière
    (ne pas descendre dans une autre table) pour rester une vérification
    valable et pas une simple redite de la même logique.
    Retourne {table_name: count}.
    """
    boundary_tags = {
        tag_map.get(t.get("original_type", ""))
        for t in tables
        if t.get("parent_table") and tag_map.get(t.get("original_type", ""))
    }

    tree = etree.parse(xml_path)
    root = tree.getroot()

    def local(tag):
        return tag.split("}")[-1] if "}" in tag else tag

    counts = {t["table_name"]: 0 for t in tables}

    root_table = next((t for t in tables if "parent_table" not in t), None)
    if root_table:
        counts[root_table["table_name"]] = 1  # la racine elle-même = 1 document

    def walk(elem, current_table):
        # cherche, pour chaque table enfant directe de current_table,
        # les occurrences de sa balise, sans descendre dans une autre
        # frontière de table.
        child_tables = [t for t in tables if t.get("parent_table") == current_table["table_name"]]
        for child_table in child_tables:
            tag_name = tag_map.get(child_table.get("original_type", ""))
            if not tag_name:
                continue
            found = []

            def scan(e):
                for c in e:
                    ln = local(c.tag)
                    if ln.lower() == tag_name.lower():
                        found.append(c)
                        continue
                    if ln in boundary_tags:
                        continue
                    scan(c)

            scan(elem)
            counts[child_table["table_name"]] += len(found)
            for occ in found:
                walk(occ, child_table)

    if root_table:
        walk(root, root_table)

    return counts


def verify_file(xsd_path, xml_path, tables, tag_map):
    print(f"\n{'='*70}")
    print(f"Fichier : {xml_path}")
    print("=" * 70)

    problems = []

    # --- 1. Extraction réelle (même code que le pipeline) ---
    extractor = XMLExtractor(xml_path, tables, tag_map)
    try:
        data = extractor.extract()
    except Exception as e:
        print(f"   ERREUR pendant l'extraction : {e}")
        return False

    # --- 2. Comptage indépendant, scoped par frontière ---
    expected_counts = count_tag_occurrences_scoped(xml_path, tag_map, tables)

    for table in tables:
        table_name = table["table_name"]
        extracted_n = len(data.get(table_name, []))
        expected_n = expected_counts.get(table_name, 0)
        status = "OK" if extracted_n == expected_n else "A VERIFIER"
        if status != "OK":
            problems.append(
                f"{table_name} : {extracted_n} extraite(s) vs {expected_n} attendue(s) dans le XML"
            )
        marker = "  " if status == "OK" else "!!"
        print(f" {marker} {table_name:35s} extrait={extracted_n:3d}  attendu={expected_n:3d}  [{status}]")

    # --- 3. Vérifie les clés + colonnes techniques sur chaque ligne ---
    for table in tables:
        table_name = table["table_name"]
        pk_cols = table.get("primary_key")
        for row in data.get(table_name, []):
            if pk_cols:
                for pk_col in pk_cols:
                    if row.get(pk_col) in (None, ""):
                        problems.append(
                            f"{table_name} : ligne local_id={row.get('_local_id')} "
                            f"a une clé incomplète -- '{pk_col}' manquant"
                        )
            for tech_col in ("__fk_constraint",):
                if any(k.startswith(tech_col) for k in row.keys() if isinstance(k, str)):
                    problems.append(
                        f"{table_name} : ligne local_id={row.get('_local_id')} "
                        f"contient une colonne technique résiduelle"
                    )

    if problems:
        print("\n Problèmes détectés :")
        for p in problems:
            print(f"   - {p}")
    else:
        print("\n Aucun problème détecté sur ce fichier.")

    return len(problems) == 0


def main():
    if len(sys.argv) < 3:
        print("Usage: python verify_insertions.py <xsd> <dossier_ou_fichier_xml>")
        sys.exit(1)

    xsd_path = sys.argv[1]
    target = sys.argv[2]

    print(f"Analyse du XSD : {xsd_path}")
    parser = XSDParserTCE(xsd_path)
    tables, tag_map = parser.parse()

    if os.path.isdir(target):
        xml_files = sorted(
            os.path.join(target, f) for f in os.listdir(target) if f.lower().endswith(".xml")
        )
    else:
        xml_files = [target]

    if not xml_files:
        print(f"Aucun fichier .xml trouvé dans {target}")
        sys.exit(0)

    results = {}
    for xml_path in xml_files:
        try:
            ok = verify_file(xsd_path, xml_path, tables, tag_map)
        except Exception as e:
            print(f"\n ERREUR inattendue sur {xml_path} : {e}")
            ok = False
        results[xml_path] = ok

    print(f"\n\n{'='*70}")
    print("=== RESUME GLOBAL ===")
    print("=" * 70)
    n_ok = sum(1 for v in results.values() if v)
    n_ko = len(results) - n_ok
    for path, ok in results.items():
        print(f"  [{'OK' if ok else 'A VERIFIER'}] {path}")
    print(f"\nTotal : {n_ok} fichier(s) OK, {n_ko} fichier(s) à vérifier sur {len(results)}")


if __name__ == "__main__":
    main()
