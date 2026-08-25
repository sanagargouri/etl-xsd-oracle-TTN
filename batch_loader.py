# batch_loader.py
import os
import sys
import time
import shutil
sys.path.append("src")

from web import ddl_oracle


def process_batch(xml_folder, loader, dossier_traites=None, dossier_erreurs=None):
    """
    Traite tous les fichiers .xml d'un dossier :
    pour chacun, utilise le schema XSD choisi manuellement au moment du
    depot (page "Deposer un fichier"), retrouve via la file d'attente
    DDL_XSD_PENDING_FILES (plus de detection automatique du type de
    document), extrait les donnees et les insere dans les tables Oracle
    deja creees pour ce schema, puis deplace le fichier vers 'traites'
    (succes) ou 'erreurs' (echec, y compris si aucun schema n'a ete
    associe a ce fichier).

    xml_folder      : dossier contenant les fichiers XML a traiter
    loader          : instance de DataLoader deja connectee a Oracle
                       (utilisee uniquement pour journaliser dans ETL_LOG)
    dossier_traites : dossier ou deplacer les fichiers traites avec succes
    dossier_erreurs : dossier ou deplacer les fichiers en echec

    Retourne un resume : liste de resultats par fichier
    (nom, statut "OK"/"ERREUR", nombre de lignes chargees ou message d'erreur)
    """
    parent = os.path.dirname(xml_folder.rstrip("\\/"))
    if dossier_traites is None:
        dossier_traites = os.path.join(parent, "traites")
    if dossier_erreurs is None:
        dossier_erreurs = os.path.join(parent, "erreurs")

    os.makedirs(dossier_traites, exist_ok=True)
    os.makedirs(dossier_erreurs, exist_ok=True)

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

        id_historique = ddl_oracle.get_queued_schema(filename)
        if id_historique is None:
            duree = round(time.time() - debut, 2)
            message = (
                "Aucun schema XSD associe a ce fichier -- redepose-le depuis "
                "'Deposer un fichier' en choisissant un schema."
            )
            print(f"    {message}")
            results.append({"fichier": filename, "statut": "ERREUR", "erreur": message})
            loader.log_result(
                nom_fichier=filename, statut="ERREUR", lignes_chargees=0,
                message_erreur=message, duree_secondes=duree,
            )
            shutil.move(xml_path, os.path.join(dossier_erreurs, filename))
            continue

        result = ddl_oracle.insert_xml_into_tables(id_historique, xml_path)
        duree = round(time.time() - debut, 2)

        if "error" in result:
            print(f"    ERREUR sur {filename} : {result['error']}")
            results.append({"fichier": filename, "statut": "ERREUR", "erreur": result["error"]})
            loader.log_result(
                nom_fichier=filename, statut="ERREUR", lignes_chargees=0,
                message_erreur=result["error"], duree_secondes=duree,
            )
            shutil.move(xml_path, os.path.join(dossier_erreurs, filename))
        else:
            print(f"    {result['inserted']} ligne(s) inseree(s)")
            results.append({
                "fichier": filename, "statut": "OK", "lignes_chargees": result["inserted"],
            })
            loader.log_result(
                nom_fichier=filename, statut="OK",
                lignes_chargees=result["inserted"], duree_secondes=duree,
            )
            shutil.move(xml_path, os.path.join(dossier_traites, filename))

        ddl_oracle.remove_queued_schema(filename)

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
    import config

    if len(sys.argv) < 2:
        print("Usage: python batch_loader.py <dossier_xml>")
        sys.exit(1)

    xml_folder = sys.argv[1]

    from src.data_loader import DataLoader

    loader = DataLoader(
        username=config.DB_USERNAME,
        password=config.DB_PASSWORD,
        dsn=config.DB_DSN,
    )

    try:
        loader.connect()
        loader.create_log_table()
        results = process_batch(xml_folder, loader)
        print_batch_summary(results)
    finally:
        loader.disconnect()