# batch_loader.py
import os
import sys
import time
import shutil
sys.path.append("src")

from xsd_parser import XSDParser
from xml_extractor import XMLExtractor
from data_loader import DataLoader


def process_batch(xsd_path, xml_folder, loader, dossier_traites=None, dossier_erreurs=None):
    """
    Traite tous les fichiers .xml d'un dossier :
    pour chacun, extrait les données, les charge dans Oracle,
    puis déplace le fichier vers 'traites' (succès) ou 'erreurs' (échec).
    Ce déplacement évite de retraiter les mêmes fichiers au passage suivant
    (indispensable une fois qu'on planifie l'exécution automatique).

    xsd_path        : chemin vers le fichier XSD (parsé une seule fois)
    xml_folder      : dossier contenant les fichiers XML à traiter
    loader          : instance de DataLoader déjà connectée à Oracle
    dossier_traites : dossier où déplacer les fichiers traités avec succès
                       (par défaut : <parent de xml_folder>/traites)
    dossier_erreurs : dossier où déplacer les fichiers en échec
                       (par défaut : <parent de xml_folder>/erreurs)

    Retourne un résumé : liste de résultats par fichier
    (nom, statut "OK"/"ERREUR", nombre de lignes chargées ou message d'erreur)
    """
    # Détermine les dossiers de destination par défaut, à côté de xml_folder
    parent = os.path.dirname(xml_folder.rstrip("\\/"))
    if dossier_traites is None:
        dossier_traites = os.path.join(parent, "traites")
    if dossier_erreurs is None:
        dossier_erreurs = os.path.join(parent, "erreurs")

    os.makedirs(dossier_traites, exist_ok=True)
    os.makedirs(dossier_erreurs, exist_ok=True)

    print(f"=== Parsing du XSD (une seule fois) ===")
    parser = XSDParser(xsd_path)
    tables, tag_map = parser.parse()

    # Liste tous les fichiers .xml du dossier (insensible à la casse)
    xml_files = sorted([
        f for f in os.listdir(xml_folder)
        if f.lower().endswith(".xml")
    ])

    print(f"\n=== {len(xml_files)} fichier(s) XML à traiter dans {xml_folder} ===")

    results = []

    for filename in xml_files:
        xml_path = os.path.join(xml_folder, filename)
        print(f"\n--- Traitement de {filename} ---")

        debut = time.time()

        try:
            extractor = XMLExtractor(xml_path, tables, tag_map)
            data = extractor.extract()

            total_loaded = loader.load_all(tables, data)
            duree = round(time.time() - debut, 2)

            results.append({
                "fichier": filename,
                "statut": "OK",
                "lignes_chargees": total_loaded,
            })

            loader.log_result(
                nom_fichier=filename,
                statut="OK",
                lignes_chargees=total_loaded,
                duree_secondes=duree,
            )

            # Déplace le fichier traité pour ne pas le retraiter au prochain passage
            shutil.move(xml_path, os.path.join(dossier_traites, filename))

        except Exception as e:
            # Un fichier en erreur ne doit pas arrêter le traitement des autres
            duree = round(time.time() - debut, 2)
            print(f"    ERREUR sur {filename} : {e}")
            results.append({
                "fichier": filename,
                "statut": "ERREUR",
                "erreur": str(e),
            })

            loader.log_result(
                nom_fichier=filename,
                statut="ERREUR",
                lignes_chargees=0,
                message_erreur=str(e),
                duree_secondes=duree,
            )

            # Déplace aussi le fichier en échec, pour ne pas boucler dessus indéfiniment
            shutil.move(xml_path, os.path.join(dossier_erreurs, filename))
            continue

    return results


def print_batch_summary(results):
    """Affiche un résumé final du traitement par lot."""
    print("\n" + "=" * 50)
    print("=== RÉSUMÉ DU TRAITEMENT PAR LOT ===")
    print("=" * 50)

    ok_count = sum(1 for r in results if r["statut"] == "OK")
    error_count = sum(1 for r in results if r["statut"] == "ERREUR")
    total_lignes = sum(r.get("lignes_chargees", 0) for r in results if r["statut"] == "OK")

    for r in results:
        if r["statut"] == "OK":
            print(f"   {r['fichier']} : {r['lignes_chargees']} ligne(s) chargée(s)")
        else:
            print(f"   {r['fichier']} : ERREUR - {r['erreur']}")

    print(f"\nTotal : {ok_count} fichier(s) OK, {error_count} fichier(s) en erreur")
    print(f"Total de lignes chargées : {total_lignes}")


# ---------------------------------------------------------------
# TEST RAPIDE
# ---------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python batch_loader.py <xsd> <dossier_xml>")
        sys.exit(1)

    xsd_path = sys.argv[1]
    xml_folder = sys.argv[2]

    loader = DataLoader(
        username="sana",
        password="Oracle123",
        dsn="localhost:1521/orcl2121"
    )

    try:
        loader.connect()
        loader.create_log_table()
        results = process_batch(xsd_path, xml_folder, loader)
        print_batch_summary(results)
    finally:
        loader.disconnect()