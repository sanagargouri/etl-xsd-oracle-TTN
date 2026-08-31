"""
web/schema_info.py

Résume, pour le dashboard, chaque schéma créé via le Générateur DDL /
Déposer un fichier : son nom (racine) et la liste de ses tables Oracle.

Lit directement DDL_XSD_HISTORIQUE / DDL_XSD_TABLE_CONFIG (ddl_oracle.py)
-- plus de dépendance à un XSD fixe (TEIF/TCE) : tout schéma créé
apparaît ici automatiquement.

Accepte un `loader` optionnel : si fourni (connexion déjà ouverte,
ex. depuis la route dashboard), elle est réutilisée et n'est pas fermée
ici. Si absent, comportement inchangé : connexion/déconnexion propre.
"""

from web import ddl_oracle


def get_schema_summary(loader=None):
    """
    Retourne une liste de résumés, un par schéma créé :
        {"label": <nom de la racine>, "xsd_filename": ..., "tables": [...]}
    Un schéma dont les tables auraient été supprimées depuis reste
    affiché (avec la liste des noms attendus), pour rester visible plutôt
    que de disparaître silencieusement.

    loader : connexion Oracle déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = ddl_oracle.connect()

    summaries = []
    try:
        ddl_oracle.ensure_meta_tables(loader)
        for schema in ddl_oracle.list_historique(loader=loader):
            loader.cursor.execute(
                "SELECT table_name FROM DDL_XSD_TABLE_CONFIG "
                "WHERE id_historique = :1 ORDER BY ordre",
                [schema["id_historique"]],
            )
            table_names = [row[0] for row in loader.cursor.fetchall()]

            summaries.append({
                "label": schema["root_name"],
                "xsd_filename": schema["xsd_filename"],
                "tables": table_names,
            })
    except Exception as e:
        print(f"[schema_info] Erreur lors du résumé des schémas : {e}")
    finally:
        if own_connection:
            loader.disconnect()

    return summaries