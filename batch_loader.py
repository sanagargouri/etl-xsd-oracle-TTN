# batch_loader.py
import os
import sys
sys.path.append("src")

from xsd_parser import XSDParser
from xml_extractor import XMLExtractor
from data_loader import DataLoader


def process_batch(xsd_path, xml_folder, loader):
    """
    Traite tous les fichiers .xml d'un dossier :
    pour chacun, extrait les données puis les charge dans Oracle.

    xsd_path   : chemin vers le fichier XSD (parsé une seule fois)
    xml_folder : dossier contenant les fichiers XML à traiter
    loader     : instance de DataLoader déjà connectée à Oracle

    Retourne un résumé : liste de résultats par fichier
    (nom, statut "OK"/"ERREUR", nombre de lignes chargées ou message d'erreur)
    """
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

        try:
            extractor = XMLExtractor(xml_path, tables, tag_map)
            data = extractor.extract()

            total_loaded = loader.load_all(tables, data)

            results.append({
                "fichier": filename,
                "statut": "OK",
                "lignes_chargees": total_loaded,
            })

        except Exception as e:
            # Un fichier en erreur ne doit pas arrêter le traitement des autres
            print(f"    ERREUR sur {filename} : {e}")
            results.append({
                "fichier": filename,
                "statut": "ERREUR",
                "erreur": str(e),
            })
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
        results = process_batch(xsd_path, xml_folder, loader)
        print_batch_summary(results)
    finally:
        loader.disconnect()