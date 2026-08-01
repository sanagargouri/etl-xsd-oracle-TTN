# batch_loader.py
import os
import sys
import time
import shutil
sys.path.append("src")

from document_router import DocumentRouter, UnknownDocumentTypeError
from table_generator import TableGenerator
from xml_extractor import XMLExtractor
from data_loader import DataLoader


def _load_custom_root_names(loader):
    """
    Lit ETL_SCHEMA_CONFIG pour connaître les noms personnalisés déjà
    choisis via la page "Gérer les schémas" (/schemas). Retourne {} si
    la table n'existe pas encore (aucune personnalisation n'a jamais
    été faite) -- comportement par défaut inchangé dans ce cas.
    """
    try:
        loader.cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = 'ETL_SCHEMA_CONFIG'"
        )
        if loader.cursor.fetchone()[0] == 0:
            return {}
        loader.cursor.execute("SELECT schema_key, custom_root_name FROM ETL_SCHEMA_CONFIG")
        return {row[0]: row[1] for row in loader.cursor.fetchall()}
    except Exception as e:
        print(f"[batch_loader] Avertissement : impossible de lire "
              f"ETL_SCHEMA_CONFIG ({e}) -> noms automatiques utilisés")
        return {}


def process_batch(xsd_teif_path, xsd_tce_path, xml_folder, loader,
                   dossier_traites=None, dossier_erreurs=None):
    """
    Traite tous les fichiers .xml d'un dossier :
    pour chacun, DÉTECTE automatiquement le type de document (TEIF ou
    DOCUMENT/TCE) à partir de sa racine XML, extrait les données selon
    le XSD correspondant, les charge dans Oracle, puis déplace le fichier
    vers 'traites' (succès) ou 'erreurs' (échec).

    xsd_teif_path   : chemin vers le XSD de la facture TEIF
    xsd_tce_path    : chemin vers tce.xsd (messages génériques DOCUMENT)
    xml_folder      : dossier contenant les fichiers XML à traiter
    loader          : instance de DataLoader déjà connectée à Oracle
    dossier_traites : dossier où déplacer les fichiers traités avec succès
    dossier_erreurs : dossier où déplacer les fichiers en échec

    Retourne un résumé : liste de résultats par fichier
    (nom, statut "OK"/"ERREUR", nombre de lignes chargées ou message d'erreur)
    """
    parent = os.path.dirname(xml_folder.rstrip("\\/"))
    if dossier_traites is None:
        dossier_traites = os.path.join(parent, "traites")
    if dossier_erreurs is None:
        dossier_erreurs = os.path.join(parent, "erreurs")

    os.makedirs(dossier_traites, exist_ok=True)
    os.makedirs(dossier_erreurs, exist_ok=True)

    # Charge les noms de table racine personnalisés (page /schemas) avant
    # de router quoi que ce soit, pour que le dépôt automatique respecte
    # exactement les mêmes noms que ceux choisis manuellement.
    custom_root_names = _load_custom_root_names(loader)
    router = DocumentRouter(
        xsd_teif_path=xsd_teif_path,
        xsd_tce_path=xsd_tce_path,
        custom_root_names=custom_root_names,
    )

    # TableGenerator réutilise les mêmes identifiants Oracle que le loader,
    # pour créer les tables manquantes à la volée (une seule fois par schéma).
    table_generator = TableGenerator(
        username=loader.username, password=loader.password, dsn=loader.dsn
    )
    table_generator.connection = loader.connection
    table_generator.cursor = loader.cursor
    schemas_ensured = set()

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
            schema_key, tables, tag_map = router.resolve(xml_path)
            print(f"    Type détecté : {schema_key}")

            # Crée les tables Oracle manquantes pour ce schéma, une seule
            # fois (les appels suivants sont des no-op silencieux car
            # create_table() ignore les tables déjà existantes).
            if schema_key not in schemas_ensured:
                if schema_key == "TEIF":
                    xsd_used = xsd_teif_path
                elif schema_key == "DOCUMENT":
                    xsd_used = xsd_tce_path
                else:
                    xsd_used = None  # cas fallback AUTO_*, nom de fichier non essentiel ici

                table_generator.create_all_tables(
                    tables, drop_if_exists=False,
                    schema_key=schema_key,
                    xsd_filename=os.path.basename(xsd_used) if xsd_used else schema_key,
                )
                schemas_ensured.add(schema_key)

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

            shutil.move(xml_path, os.path.join(dossier_traites, filename))

        except UnknownDocumentTypeError as e:
            duree = round(time.time() - debut, 2)
            print(f"    Type de document non reconnu sur {filename} : {e}")
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
            shutil.move(xml_path, os.path.join(dossier_erreurs, filename))
            continue

        except Exception as e:
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
    import config

    if len(sys.argv) < 4:
        print("Usage: python batch_loader.py <xsd_teif> <xsd_tce> <dossier_xml>")
        sys.exit(1)

    xsd_teif_path = sys.argv[1]
    xsd_tce_path = sys.argv[2]
    xml_folder = sys.argv[3]

    loader = DataLoader(
        username=config.DB_USERNAME,
        password=config.DB_PASSWORD,
        dsn=config.DB_DSN,
    )

    try:
        loader.connect()
        loader.create_log_table()
        results = process_batch(xsd_teif_path, xsd_tce_path, xml_folder, loader)
        print_batch_summary(results)
    finally:
        loader.disconnect()