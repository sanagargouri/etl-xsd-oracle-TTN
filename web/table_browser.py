"""
web/table_browser.py

Lecture générique du contenu des tables Oracle générées par le pipeline,
pour /tables-oracle. Connexion Oracle courte à chaque appel, comme les
autres modules de lecture (stats.py, schema_info.py).

Générique : aucune colonne ni aucun nom de table n'est écrit en dur —
tout est détecté dynamiquement (user_tables, cursor.description),
pour que ça marche avec n'importe quel XSD, pas seulement les factures.
"""

import config
from src.data_loader import DataLoader

PAGE_SIZE = 25


def _connect():
    loader = DataLoader(
        username=config.DB_USERNAME,
        password=config.DB_PASSWORD,
        dsn=config.DB_DSN,
    )
    loader.connect()
    return loader


def list_tables():
    """
    Liste les tables réellement présentes dans Oracle, avec leur nombre
    de lignes. Exclut ETL_LOG (table technique de journalisation, pas
    une table métier générée depuis le XSD).
    """
    loader = _connect()
    try:
        loader.cursor.execute(
            "SELECT table_name FROM user_tables "
            "WHERE table_name != 'ETL_LOG' ORDER BY table_name"
        )
        table_names = [row[0] for row in loader.cursor.fetchall()]

        tables = []
        for name in table_names:
            # Nom de table validé juste au-dessus (vient de user_tables,
            # pas d'une entrée utilisateur) avant d'être inséré dans le SQL.
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