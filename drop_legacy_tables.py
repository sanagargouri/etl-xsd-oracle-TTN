"""
drop_legacy_tables.py

Identifie et supprime les tables Oracle qui ne correspondent PLUS au
pipeline actuel -- sans deviner leurs noms, mais en les COMPARANT
réellement à ce que le pipeline produit aujourd'hui.

Méthode :
1. Parse les deux XSD actuels (TEIF et TCE) avec XSDParser / XSDParserTCE
   -- exactement comme le fait batch_loader.py -- pour obtenir la liste
   EXACTE des noms de table que le pipeline produit réellement aujourd'hui.
2. Compare cette liste à ce qui existe VRAIMENT dans Oracle (user_tables).
3. Toute table Oracle absente de cette liste calculée est une candidate
   à la suppression (ancien essai, ancienne convention de nommage...).
4. Par sécurité supplémentaire, les tables candidates contenant des
   données (row_count > 0) ne sont PAS supprimées automatiquement --
   elles sont juste signalées, au cas où elles auraient une autre raison
   d'être là.
5. Confirmation explicite obligatoire (taper SUPPRIMER) avant tout DROP.

Usage :
    python drop_legacy_tables.py
    python drop_legacy_tables.py --include-non-empty   (force aussi les
                                                          tables non vides,
                                                          à utiliser en
                                                          connaissance de
                                                          cause seulement)
"""

import sys
sys.path.append("src")

import config
import oracledb
from xsd_parser import XSDParser
from xsd_parser_tce import XSDParserTCE


def compute_legitimate_table_names():
    """
    Reproduit exactement ce que fait document_router.py au démarrage :
    parse les deux XSD connus et retourne l'ensemble des noms de table
    que le pipeline actuel est censé produire.
    """
    names = set()

    parser_teif = XSDParser(config.XSD_PATH_TEIF)
    tables_teif, _ = parser_teif.parse()
    names.update(t["table_name"] for t in tables_teif)

    parser_tce = XSDParserTCE(config.XSD_PATH_TCE)
    tables_tce, _ = parser_tce.parse()
    names.update(t["table_name"] for t in tables_tce)

    names.add("ETL_LOG")  # table technique, toujours légitime

    return names


def main():
    include_non_empty = "--include-non-empty" in sys.argv

    print("=== Calcul des tables légitimes (parsing des XSD actuels) ===")
    legitimate = compute_legitimate_table_names()
    print(f"  {len(legitimate)} table(s) légitime(s) selon le pipeline actuel : "
          f"{sorted(legitimate)}\n")

    conn = oracledb.connect(
        user=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN
    )
    cur = conn.cursor()

    cur.execute("SELECT table_name FROM user_tables ORDER BY table_name")
    all_tables = [row[0] for row in cur.fetchall()]

    candidates = [t for t in all_tables if t not in legitimate]

    if not candidates:
        print("Aucune table candidate : toutes les tables présentes dans "
              "Oracle correspondent au pipeline actuel. Rien à supprimer.")
        conn.close()
        return

    print(f"=== {len(candidates)} table(s) NE correspondant PAS au pipeline "
          f"actuel ===\n")

    to_drop = []
    to_skip = []

    for table_name in candidates:
        cur.execute(f"SELECT COUNT(*) FROM {table_name}")
        row_count = cur.fetchone()[0]

        if row_count == 0:
            print(f"  [VIDE]      {table_name} (0 ligne) -> candidate à la suppression")
            to_drop.append(table_name)
        elif include_non_empty:
            print(f"  [NON VIDE]  {table_name} ({row_count} ligne(s)) -> "
                  f"sera supprimée quand même (--include-non-empty actif)")
            to_drop.append(table_name)
        else:
            print(f"  [NON VIDE]  {table_name} ({row_count} ligne(s)) -> "
                  f"IGNORÉE par sécurité : elle contient des données et "
                  f"pourrait avoir une autre utilité. Relancez avec "
                  f"--include-non-empty si vous êtes sûr de vouloir la "
                  f"supprimer quand même.")
            to_skip.append(table_name)

    if not to_drop:
        print("\nAucune table à supprimer automatiquement (toutes celles "
              "restantes contiennent des données et ont été ignorées par "
              "sécurité).")
        conn.close()
        return

    print(f"\n{len(to_drop)} table(s) seront DÉFINITIVEMENT supprimée(s), "
          f"{len(to_skip)} ignorée(s) par sécurité.")
    confirmation = input("\nTapez SUPPRIMER en toutes lettres pour confirmer : ")

    if confirmation.strip() != "SUPPRIMER":
        print("Annulé. Aucune table n'a été supprimée.")
        conn.close()
        return

    for table_name in to_drop:
        try:
            cur.execute(f"DROP TABLE {table_name} CASCADE CONSTRAINTS")
            conn.commit()
            print(f"  Supprimée : {table_name}")
        except oracledb.Error as e:
            print(f"  Erreur lors de la suppression de {table_name} : {e}")

    print("\nTerminé.")
    conn.close()


if __name__ == "__main__":
    main()
