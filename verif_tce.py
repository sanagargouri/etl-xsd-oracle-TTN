"""
Script de verification TCE - compare les fichiers XML avec les tables Oracle
=============================================================================

Objectif :
  - Compter, pour chaque fichier XML TCE (tce.xsd), le nombre d'occurrences
    de chaque element repetitif (ARTICLE, PIECES_JOINTE, observations...)
  - Comparer avec le contenu reel des tables Oracle correspondantes
  - Verifier l'unicite des cles naturelles (pas de doublons / d'ecrasements)

Prerequis :
  pip install lxml oracledb

Usage :
  python verif_tce.py --xml-dir "data\\xml\\traites" --dsn "host:port/service" --user USER --password PASS

Adapte les XPATHS et les noms de colonnes ci-dessous a ton schema exact
si besoin (noms de balises, noms de colonnes en base).
"""

import argparse
import glob
import os
import sys
from lxml import etree

try:
    import oracledb
except ImportError:
    oracledb = None


# ---------------------------------------------------------------------------
# Configuration : element XML repetitif -> table Oracle + cle naturelle
# ---------------------------------------------------------------------------
# Chaque entree definit :
#   xpath        : chemin XPath vers l'element repetitif dans le XML
#   table        : nom de la table Oracle correspondante
#   key_cols     : colonnes formant la cle naturelle en base (pour le check
#                  d'unicite). Laisser None si la table utilise un ID genere
#                  (exception assumee, pas de cle naturelle a verifier).
#   xml_key_tags : tags XML (sous l'element repetitif) correspondant a la
#                  cle naturelle, dans le meme ordre que key_cols.
#                  Utilise pour reconstruire la cle depuis le XML.
# ---------------------------------------------------------------------------
TCE_MAPPING = {
    "DOCUMENT": {
        "xpath": ".",  # racine, une seule par fichier
        "table": "DOCUMENT",
        "key_cols": ["NUMERO_DOSSIER"],
        "xml_key_tags": [".//REFERENCE_TTN/NUMERO_DOSSIER"],
    },
    "ARTICLE": {
        "xpath": ".//ARTICLE",
        "table": "ARTICLE",
        "key_cols": ["NUMERO_DOSSIER", "NUMERO_ARTICLE"],
        "xml_key_tags": [".//REFERENCE_TTN/NUMERO_DOSSIER", "NUMERO_ARTICLE"],
    },
    "PIECES_JOINTE": {
        "xpath": ".//PIECES_JOINTE",
        "table": "PIECES_JOINTE",
        "key_cols": ["NUMERO_DOSSIER", "REFERENCE_BASE_IMAGE"],
        "xml_key_tags": [".//REFERENCE_TTN/NUMERO_DOSSIER", "REFERENCE_BASE_IMAGE"],
    },
    # Tables sans discriminant naturel (exception assumee) : on verifie
    # seulement le COUNT, pas l'unicite d'une cle (il n'y en a pas).
    "MINISTERE_COMMERCE_OBSERVATION": {
        "xpath": ".//MINISTERE_COMMERCE_OBSERVATION",
        "table": "MINISTERE_COMMERCE_OBSERVATION",
        "key_cols": None,
        "xml_key_tags": None,
    },
    "ORGANISME_OBSERVATION": {
        "xpath": ".//ORGANISME_OBSERVATION",
        "table": "ORGANISME_OBSERVATION",
        "key_cols": None,
        "xml_key_tags": None,
    },
    "BANQUE_OBSERVATION": {
        "xpath": ".//BANQUE_OBSERVATION",
        "table": "BANQUE_OBSERVATION",
        "key_cols": None,
        "xml_key_tags": None,
    },
    "ARTICLE_COMMERCE_OBSERVATION": {
        "xpath": ".//ARTICLE_COMMERCE_OBSERVATION",
        "table": "ARTICLE_COMMERCE_OBSERVATION",
        "key_cols": None,
        "xml_key_tags": None,
    },
    "ARTICLE_TECHNIQUE_OBSERVATION": {
        "xpath": ".//ARTICLE_TECHNIQUE_OBSERVATION",
        "table": "ARTICLE_TECHNIQUE_OBSERVATION",
        "key_cols": None,
        "xml_key_tags": None,
    },
    "ARTICLE_BANQUE_OBSERVATION": {
        "xpath": ".//ARTICLE_BANQUE_OBSERVATION",
        "table": "ARTICLE_BANQUE_OBSERVATION",
        "key_cols": None,
        "xml_key_tags": None,
    },
}


def is_tce_file(root):
    """Un fichier est considere TCE seulement s'il contient REFERENCE_TTN
    (racine du format TCE). Les fichiers TEIF (factures) n'ont pas ce tag
    et doivent etre ignores par ce script."""
    return root.find(".//REFERENCE_TTN") is not None


def count_xml_elements(xml_dir):
    """Compte, pour chaque fichier XML TCE du dossier (les autres formats,
    ex. TEIF, sont ignores), le nombre d'occurrences de chaque element
    mappe. Retourne un total par table + le detail par fichier pour debug."""
    totals = {name: 0 for name in TCE_MAPPING}
    per_file = {}
    skipped = []

    files = sorted(glob.glob(os.path.join(xml_dir, "*.xml")))
    if not files:
        print(f"[!] Aucun fichier .xml trouve dans {xml_dir}")
        sys.exit(1)

    for filepath in files:
        try:
            tree = etree.parse(filepath)
        except Exception as e:
            print(f"[ERREUR] Parsing impossible pour {filepath} : {e}")
            continue

        root = tree.getroot()

        if not is_tce_file(root):
            skipped.append(filepath)
            continue

        per_file[filepath] = {}
        for name, cfg in TCE_MAPPING.items():
            if name == "DOCUMENT":
                count = 1  # une racine par fichier
            else:
                count = len(root.findall(cfg["xpath"]))
            totals[name] += count
            per_file[filepath][name] = count

    if skipped:
        print(f"[*] {len(skipped)} fichier(s) ignore(s) (non-TCE, ex. TEIF) :")
        for f in skipped:
            print(f"    - {os.path.basename(f)}")

    return totals, per_file


def get_oracle_counts(dsn, user, password):
    """Retourne, pour chaque table mappee, le COUNT(*) et le COUNT(DISTINCT cle)."""
    if oracledb is None:
        print("[!] Le module 'oracledb' n'est pas installe (pip install oracledb).")
        sys.exit(1)

    results = {}
    with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
        with conn.cursor() as cur:
            for name, cfg in TCE_MAPPING.items():
                table = cfg["table"]
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    total = cur.fetchone()[0]
                except Exception as e:
                    print(f"[ERREUR] Impossible de lire {table} : {e}")
                    results[name] = {"count": None, "distinct": None}
                    continue

                distinct = None
                if cfg["key_cols"]:
                    cols = ", ".join(cfg["key_cols"])
                    cur.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {table})")
                    distinct = cur.fetchone()[0]

                results[name] = {"count": total, "distinct": distinct}

    return results


def print_report(xml_totals, oracle_counts):
    print("\n" + "=" * 78)
    print(f"{'Table':<32}{'XML':>8}{'Oracle':>10}{'Distinct':>12}{'Statut':>14}")
    print("=" * 78)

    all_ok = True
    for name in TCE_MAPPING:
        xml_count = xml_totals.get(name, 0)
        oracle = oracle_counts.get(name, {})
        db_count = oracle.get("count")
        db_distinct = oracle.get("distinct")

        if db_count is None:
            status = "ERREUR LECTURE"
            all_ok = False
        elif xml_count != db_count:
            status = "ECART COUNT"
            all_ok = False
        elif db_distinct is not None and db_distinct != db_count:
            status = "DOUBLONS"
            all_ok = False
        else:
            status = "OK"

        distinct_str = str(db_distinct) if db_distinct is not None else "-"
        print(f"{name:<32}{xml_count:>8}{str(db_count):>10}{distinct_str:>12}{status:>14}")

    print("=" * 78)
    if all_ok:
        print("Toutes les tables correspondent aux XML. Aucun ecart detecte.\n")
    else:
        print("Des ecarts ont ete detectes - voir le detail ci-dessus.\n")

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Verification TCE : XML vs tables Oracle")
    parser.add_argument("--xml-dir", required=True, help="Dossier contenant les fichiers XML TCE")
    parser.add_argument("--dsn", required=True, help="DSN Oracle, ex: host:port/service_name")
    parser.add_argument("--user", required=True, help="Utilisateur Oracle")
    parser.add_argument("--password", required=True, help="Mot de passe Oracle")
    parser.add_argument("--detail", action="store_true", help="Afficher le detail par fichier XML")
    args = parser.parse_args()

    print(f"[*] Analyse des fichiers XML dans : {args.xml_dir}")
    xml_totals, per_file = count_xml_elements(args.xml_dir)

    if args.detail:
        print("\n--- Detail par fichier ---")
        for filepath, counts in per_file.items():
            print(f"\n{os.path.basename(filepath)}")
            for name, c in counts.items():
                if c:
                    print(f"    {name}: {c}")

    print(f"[*] Connexion a Oracle ({args.dsn})...")
    oracle_counts = get_oracle_counts(args.dsn, args.user, args.password)

    ok = print_report(xml_totals, oracle_counts)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
