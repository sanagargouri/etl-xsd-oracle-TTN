"""
web/table_browser.py

Lecture générique du contenu des tables Oracle générées par le pipeline,
pour /tables-oracle. Générique : aucune colonne ni aucun nom de table
n'est écrit en dur — tout est détecté dynamiquement (user_tables,
cursor.description), pour que ça marche avec n'importe quel XSD.

Deux sources de schémas connus :
  - ETL_SCHEMA_TABLES : ancien mécanisme.
  - DDL_XSD_HISTORIQUE / DDL_XSD_TABLE_CONFIG : mécanisme générique du
    Générateur DDL.

Toutes les fonctions acceptent un `loader` optionnel : si fourni,
connexion réutilisée (partagée sur toute une requête HTTP) sans être
fermée ici ; sinon, connexion/déconnexion propre à l'appel.
"""

import config
from src.data_loader import DataLoader

PAGE_SIZE = 25

_TECHNICAL_TABLES = (
    "ETL_LOG", "ETL_SCHEMA_TABLES", "ETL_SCHEMA_CONFIG",
    "DDL_XSD_HISTORIQUE", "DDL_XSD_TABLE_CONFIG", "DDL_XSD_PENDING_FILES",
    "SCHEMA_TYPE_CONFIG", "SCHEMA_COLUMN_MAPPING", "TRACE_EXECUTION",
    "STAT_DOSSIER",
)


def connect():
    """Connexion vers le schema SANA -- exposee pour partage entre appels."""
    loader = DataLoader(
        username=config.DB_USERNAME,
        password=config.DB_PASSWORD,
        dsn=config.DB_DSN,
    )
    loader.connect()
    return loader


def _has_table(loader, table_name):
    loader.cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name = :1", [table_name]
    )
    return loader.cursor.fetchone()[0] > 0


_KNOWN_LABELS = {
    "TEIF": "Facture TEIF",
    "DOCUMENT": "Document TCE (TTN)",
}


def list_schemas(loader=None):
    """
    Liste les schémas connus pour peupler la liste déroulante de
    /tables-oracle, en fusionnant ETL_SCHEMA_TABLES et DDL_XSD_HISTORIQUE.
    loader : connexion déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect()
    try:
        schemas = []

        if _has_table(loader, "ETL_SCHEMA_TABLES"):
            loader.cursor.execute(
                "SELECT DISTINCT schema_key, xsd_filename FROM ETL_SCHEMA_TABLES "
                "ORDER BY schema_key"
            )
            for r in loader.cursor.fetchall():
                schemas.append({
                    "schema_key": r[0],
                    "xsd_filename": r[1],
                    "label": _KNOWN_LABELS.get(r[0], r[0].replace("AUTO_", "").title()),
                })

        if _has_table(loader, "DDL_XSD_HISTORIQUE"):
            loader.cursor.execute(
                "SELECT id_historique, xsd_filename, root_name FROM DDL_XSD_HISTORIQUE "
                "ORDER BY date_creation DESC"
            )
            for id_historique, xsd_filename, root_name in loader.cursor.fetchall():
                schemas.append({
                    "schema_key": f"ddl:{id_historique}",
                    "xsd_filename": xsd_filename,
                    "label": root_name,
                })

        return schemas
    finally:
        if own_connection:
            loader.disconnect()


def list_tables(schema_key=None, loader=None):
    """
    Liste les tables Oracle avec leur nombre de lignes.

    Optimisation : au lieu d'un SELECT COUNT(*) par table (N+1 requêtes,
    cause principale de la lenteur), une seule requête sur
    all_tables/user_tab_statistics récupère TOUTES les tables filtrées
    d'un coup, puis les comptages exacts sont faits en une passe. Un
    COUNT(*) reste nécessaire par table pour un nombre exact (les stats
    Oracle peuvent être approximatives/périmées), mais on ne refait plus
    le SELECT COUNT(*) FROM user_tables de _has_table() pour chacune --
    la liste de noms valides est chargée une fois en un set, vérifiée en
    mémoire.

    loader : connexion déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect()
    try:
        if schema_key and schema_key.startswith("ddl:"):
            id_historique = schema_key.split(":", 1)[1]
            loader.cursor.execute(
                "SELECT table_name FROM DDL_XSD_TABLE_CONFIG WHERE id_historique = :1 "
                "ORDER BY ordre",
                [id_historique],
            )
            table_names = [row[0] for row in loader.cursor.fetchall()]
        elif schema_key and _has_table(loader, "ETL_SCHEMA_TABLES"):
            loader.cursor.execute(
                "SELECT table_name FROM ETL_SCHEMA_TABLES WHERE schema_key = :1 "
                "ORDER BY table_name",
                [schema_key],
            )
            table_names = [row[0] for row in loader.cursor.fetchall()]
        else:
            placeholders = ", ".join(f"'{t}'" for t in _TECHNICAL_TABLES)
            loader.cursor.execute(
                f"SELECT table_name FROM user_tables "
                f"WHERE table_name NOT IN ({placeholders}) ORDER BY table_name"
            )
            table_names = [row[0] for row in loader.cursor.fetchall()]

        if not table_names:
            return []

        # Recupere en une seule requete l'ensemble des tables reellement
        # existantes (au lieu d'un _has_table() par table), pour filtrer
        # les entrees de metadonnees dont la table a ete supprimee.
        loader.cursor.execute("SELECT table_name FROM user_tables")
        existing = {row[0] for row in loader.cursor.fetchall()}

        tables = []
        for name in table_names:
            if name not in existing:
                continue
            loader.cursor.execute(f"SELECT COUNT(*) FROM {name}")
            count = loader.cursor.fetchone()[0]
            tables.append({"table_name": name, "row_count": count})
        return tables
    finally:
        if own_connection:
            loader.disconnect()


def get_table_page(table_name, page=1, loader=None):
    """
    Retourne une page de lignes pour une table donnée.
    loader : connexion déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect()
    try:
        loader.cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :1",
            [table_name.upper()],
        )
        if loader.cursor.fetchone()[0] == 0:
            return None

        page = max(1, page)
        offset = (page - 1) * PAGE_SIZE

        loader.cursor.execute(
            f"SELECT * FROM {table_name} "
            f"OFFSET :1 ROWS FETCH NEXT :2 ROWS ONLY",
            [offset, PAGE_SIZE],
        )
        columns = [col[0] for col in loader.cursor.description]
        rows = loader.cursor.fetchall()

        loader.cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        total_rows = loader.cursor.fetchone()[0]
        total_pages = max(1, -(-total_rows // PAGE_SIZE))

        return {
            "table_name": table_name,
            "columns": columns,
            "rows": rows,
            "total_rows": total_rows,
            "page": page,
            "total_pages": total_pages,
        }
    finally:
        if own_connection:
            loader.disconnect()


def get_table_comments(table_name, loader=None):
    """
    Retourne les commentaires de colonnes. {} si aucun.
    loader : connexion déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect()
    try:
        loader.cursor.execute(
            "SELECT column_name, comments FROM user_col_comments "
            "WHERE table_name = :1 AND comments IS NOT NULL "
            "ORDER BY column_name",
            [table_name.upper()],
        )
        return {row[0]: row[1] for row in loader.cursor.fetchall()}
    finally:
        if own_connection:
            loader.disconnect()


def get_all_rows_for_export(table_name, loader=None):
    """
    Retourne TOUTES les lignes d'une table pour export CSV/INSERT.
    loader : connexion déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect()
    try:
        loader.cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :1",
            [table_name.upper()],
        )
        if loader.cursor.fetchone()[0] == 0:
            return None

        loader.cursor.execute(f"SELECT * FROM {table_name}")
        columns = [col[0] for col in loader.cursor.description]
        rows = loader.cursor.fetchall()
        return {"table_name": table_name, "columns": columns, "rows": rows}
    finally:
        if own_connection:
            loader.disconnect()


def build_csv_export(table_name):
    """Génère le contenu CSV (texte) de toute la table."""
    import csv
    import io

    data = get_all_rows_for_export(table_name)
    if data is None:
        return None

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(data["columns"])
    for row in data["rows"]:
        writer.writerow(["" if v is None else v for v in row])
    return buffer.getvalue()


def build_insert_export(table_name):
    """Génère des instructions INSERT INTO prêtes à coller/exécuter ailleurs."""
    import datetime

    data = get_all_rows_for_export(table_name)
    if data is None:
        return None

    columns_sql = ", ".join(data["columns"])
    lines = [f"-- Export de {data['table_name']} -- {len(data['rows'])} ligne(s)\n"]

    for row in data["rows"]:
        values_sql = []
        for value in row:
            if value is None:
                values_sql.append("NULL")
            elif isinstance(value, (int, float)):
                values_sql.append(str(value))
            elif isinstance(value, (datetime.date, datetime.datetime)):
                values_sql.append(f"TO_DATE('{value.strftime('%Y-%m-%d %H:%M:%S')}', 'YYYY-MM-DD HH24:MI:SS')")
            else:
                escaped = str(value).replace("'", "''")
                values_sql.append(f"'{escaped}'")
        lines.append(
            f"INSERT INTO {data['table_name']} ({columns_sql}) VALUES "
            f"({', '.join(values_sql)});"
        )

    return "\n".join(lines)


def get_table_ddl(table_name, loader=None):
    """
    Requête CREATE TABLE copiable, via DBMS_METADATA.GET_DDL.
    Cette fonction reste intrinsèquement lente côté Oracle (GET_DDL fait
    son propre travail interne indépendamment du nombre de connexions
    Python) -- le partage de connexion réduit le coût de connexion/
    déconnexion, mais pas le temps de calcul du DDL lui-même.
    loader : connexion déjà ouverte à réutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect()
    try:
        loader.cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :1",
            [table_name.upper()],
        )
        if loader.cursor.fetchone()[0] == 0:
            return None

        loader.cursor.execute(
            "SELECT DBMS_METADATA.GET_DDL('TABLE', :1) FROM dual",
            [table_name.upper()],
        )
        row = loader.cursor.fetchone()
        if row is None:
            return None
        ddl = row[0]
        ddl_text = ddl.read() if hasattr(ddl, "read") else str(ddl)
        return ddl_text.strip()
    finally:
        if own_connection:
            loader.disconnect()