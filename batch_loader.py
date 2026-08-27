# batch_loader.py
import os
import sys
import time
import shutil
sys.path.append("src")

from web import ddl_oracle


def _traiter_un_fichier(filename, xml_folder, loader, dossier_traites, dossier_erreurs, results):
    """
    Traite un seul fichier XML : recupere son schema en attente, insere
    ses donnees, journalise le resultat et deplace le fichier vers
    'traites' (succes) ou 'erreurs' (echec). Ajoute le resultat a `results`.
    """
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
        return

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


def process_batch(xml_folder, loader, dates_par_type=None,
                   dossier_traites=None, dossier_erreurs=None):
    """
    Traite tous les fichiers .xml d'un dossier, groupes par type de schema
    (TITRE/TCEAP et TCE en priorite, avec synchronisation LIASSE.DOSSIER
    avant chacun, en utilisant une periode de dates propre a chaque type ;
    les autres schemas ensuite, sans synchronisation).

    xml_folder      : dossier contenant les fichiers XML a traiter
    loader          : instance de DataLoader deja connectee a Oracle
                       (utilisee uniquement pour journaliser dans ETL_LOG)
    dates_par_type  : dict optionnel { "TITRE": (date_debut, date_fin),
                                        "TCE": (date_debut, date_fin) }
                       une periode differente possible pour chaque schema ;
                       un type absent du dict (ou dict absent) = pas de
                       synchronisation LIASSE pour ce type.
    dossier_traites : dossier ou deplacer les fichiers traites avec succes
    dossier_erreurs : dossier ou deplacer les fichiers en echec

    Retourne un resume : liste de resultats par fichier
    (nom, statut "OK"/"ERREUR", nombre de lignes chargees ou message d'erreur)
    """
    dates_par_type = dates_par_type or {}

    parent = os.path.dirname(xml_folder.rstrip("\\/"))
    if dossier_traites is None:
        dossier_traites = os.path.join(parent, "traites")
    if dossier_erreurs is None:
        dossier_erreurs = os.path.join(parent, "erreurs")

    os.makedirs(dossier_traites, exist_ok=True)
    os.makedirs(dossier_erreurs, exist_ok=True)

    all_xml_files = sorted([
        f for f in os.listdir(xml_folder)
        if f.lower().endswith(".xml")
    ])

    print(f"\n=== {len(all_xml_files)} fichier(s) XML à traiter dans {xml_folder} ===")

    results = []
    fichiers_geres = set()

    for root_name in ddl_oracle.SCHEMA_TYPES:
        print(f"\n=== Traitement du type {root_name} ===")

        periode = dates_par_type.get(root_name)
        if periode:
            date_debut, date_fin = periode
            if date_debut and date_fin:
                ddl_oracle.sync_dossiers_liasse_pour_type(root_name, date_debut, date_fin)

        fichiers_du_type = [
            f for f in all_xml_files
            if ddl_oracle.get_queued_root_name(f) == root_name
        ]
        print(f"{len(fichiers_du_type)} fichier(s) XML pour {root_name}")

        for filename in fichiers_du_type:
            _traiter_un_fichier(filename, xml_folder, loader, dossier_traites, dossier_erreurs, results)
            fichiers_geres.add(filename)

    fichiers_restants = [f for f in all_xml_files if f not in fichiers_geres]
    if fichiers_restants:
        print(f"\n=== Traitement des autres schemas ({len(fichiers_restants)} fichier(s)) ===")
        for filename in fichiers_restants:
            _traiter_un_fichier(filename, xml_folder, loader, dossier_traites, dossier_erreurs, results)

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