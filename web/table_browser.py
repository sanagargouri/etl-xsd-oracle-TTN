"""
web/table_browser.py

Lecture générique du contenu des tables Oracle générées par le pipeline,
pour /tables-oracle. Connexion Oracle courte à chaque appel, comme les
autres modules de lecture (stats.py, schema_info.py).

Générique : aucune colonne ni aucun nom de table n'est écrit en dur —
tout est détecté dynamiquement (user_tables, cursor.description),
pour que ça marche avec n'importe quel XSD, pas seulement les factures.

Deux sources de schémas connus :
  - ETL_SCHEMA_TABLES : ancien mécanisme (TableGenerator / "Gérer les
    schémas" historique).
  - DDL_XSD_HISTORIQUE / DDL_XSD_TABLE_CONFIG : mécanisme générique du
    Générateur DDL / "Déposer un fichier" (ddl_oracle.py).
Les deux sont fusionnées ici pour que le filtre par schéma fonctionne
quelle que soit l'origine des tables.
"""

import config
from src.data_loader import DataLoader

PAGE_SIZE = 25

_TECHNICAL_TABLES = (
    "ETL_LOG", "ETL_SCHEMA_TABLES", "ETL_SCHEMA_CONFIG",
    "DDL_XSD_HISTORIQUE", "DDL_XSD_TABLE_CONFIG", "DDL_XSD_PENDING_FILES",
)


def _connect():
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


def list_schemas():
    """
    Liste les schémas connus pour peupler la liste déroulante de
    /tables-oracle, en fusionnant les deux sources :
      - ETL_SCHEMA_TABLES (ancien mécanisme, schema_key libre type
        "TEIF"/"DOCUMENT"/"AUTO_...")
      - DDL_XSD_HISTORIQUE (Générateur DDL / Déposer un fichier),
        identifié par un schema_key préfixé "ddl:<id_historique>" pour
        ne jamais entrer en collision avec ceux de ETL_SCHEMA_TABLES.
    """
    loader = _connect()
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
        loader.disconnect()


def list_tables(schema_key=None):
    """
    Liste les tables Oracle avec leur nombre de lignes.
    Si schema_key est fourni : filtre soit via ETL_SCHEMA_TABLES (ancien
    mécanisme), soit via DDL_XSD_TABLE_CONFIG si schema_key commence par
    "ddl:" (Générateur DDL / Déposer un fichier). Sinon, comportement
    historique : toutes les tables (hors tables techniques).
    """
    loader = _connect()
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

        tables = []
        for name in table_names:
            # Nom de table validé juste au-dessus (vient de user_tables ou
            # d'une table de métadonnées, pas d'une entrée utilisateur
            # directe) avant d'être inséré dans le SQL. Une table listée
            # en metadonnées mais supprimée entre-temps est ignorée
            # plutôt que de faire planter la page.
            if not _has_table(loader, name):
                continue
            loader.cursor.execute(f"SELECT COUNT(*) FROM {name}")
            count = loader.cursor.fetchone()[0]
            tables.append({"table_name": name, "row_count": count})
        return tables
    finally:
        loader.disconnect()


def get_table_page(table_name, page=1):
    """
    Retourne une page de lignes pour une table donnée, avec ses colonnes
    détectées dynamiquement via cursor.description.

    table_name est revalidé contre la vraie liste des tables Oracle avant
    d'être inséré dans le SQL (les noms de table ne peuvent pas être des
    bind variables en Oracle) — évite toute injection SQL si quelqu'un
    manipule le paramètre ?table= dans l'URL.
    """
    loader = _connect()
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
        total_pages = max(1, -(-total_rows // PAGE_SIZE))  # division entière arrondie au-dessus

        return {
            "table_name": table_name,
            "columns": columns,
            "rows": rows,
            "total_rows": total_rows,
            "page": page,
            "total_pages": total_pages,
        }
    finally:
        loader.disconnect()


def get_table_comments(table_name):
    """
    Retourne les commentaires de colonnes (ajoutés par add_column_comments
    dans table_generator.py pour documenter le chemin XML d'origine des
    colonnes tronquées à 30 caractères). {} si aucun commentaire.
    """
    loader = _connect()
    try:
        loader.cursor.execute(
            "SELECT column_name, comments FROM user_col_comments "
            "WHERE table_name = :1 AND comments IS NOT NULL "
            "ORDER BY column_name",
            [table_name.upper()],
        )
        return {row[0]: row[1] for row in loader.cursor.fetchall()}
    finally:
        loader.disconnect()


def get_all_rows_for_export(table_name):
    """
    Retourne TOUTES les lignes d'une table (pas paginé, contrairement à
    get_table_page) pour export CSV/INSERT. table_name revalidé contre
    user_tables avant insertion SQL, comme les autres fonctions du module.
    """
    loader = _connect()
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
    """
    Génère des instructions INSERT INTO prêtes à coller/exécuter ailleurs,
    une par ligne. Échappe les apostrophes SQL, laisse les nombres nus,
    met NULL pour les valeurs absentes, formate les dates au format
    Oracle TO_DATE explicite (évite toute ambiguïté de format régional).
    """
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


def get_table_ddl(table_name):
    """
    Requête CREATE TABLE copiable, affichée à côté de chaque table dans
    /tables-oracle, pour permettre de recréer la même structure ailleurs.
    Utilise DBMS_METADATA.GET_DDL -> reflète toujours la structure réelle
    actuelle de la table (même si modifiée après sa création initiale),
    pas une version mise en cache/générée par le parseur.

    table_name est revalidé contre user_tables avant insertion dans le
    SQL, pour les mêmes raisons de sécurité que get_table_page().
    """
    loader = _connect()
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
        loader.disconnect()