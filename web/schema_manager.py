"""
web/schema_manager.py

Page "Gérer les schémas" : liste les schémas déjà créés via le
Générateur DDL (table DDL_XSD_HISTORIQUE / DDL_XSD_TABLE_CONFIG,
mécanisme générique xsd_xml_to_ddl.py + ddl_oracle.py), et permet de
renommer la table racine d'un schéma déjà créé -- en cascade sur toutes
ses tables filles, puisque leur nom est construit à partir du préfixe de
la racine dans ce mécanisme.

La création de tables ne se fait plus depuis cette page : le mécanisme
générique a toujours besoin d'un exemple de XML (pas seulement d'un XSD)
pour déterminer la structure exacte des tables -- c'est le rôle du
Générateur DDL. Cette page ne fait donc plus que lister et renommer les
schémas déjà créés.
"""

import re

from web import ddl_oracle


def _validate_name(name):
    name = name.strip().upper().replace(" ", "_")
    if not re.match(r"^[A-Z][A-Z0-9_]*$", name) or len(name) > 30:
        raise ValueError(
            "Nom de table invalide (30 caractères max, lettres/chiffres/underscore, "
            "doit commencer par une lettre)."
        )
    return name


def list_schemas():
    """
    Liste les schémas créés via le Générateur DDL (DDL_XSD_HISTORIQUE),
    avec leurs tables (DDL_XSD_TABLE_CONFIG) et si elles existent encore
    réellement dans Oracle.
    """
    loader = ddl_oracle.connect()
    try:
        ddl_oracle.ensure_meta_tables(loader)
        loader.cursor.execute(
            "SELECT id_historique, xsd_filename, root_name, date_creation "
            "FROM DDL_XSD_HISTORIQUE ORDER BY date_creation DESC"
        )
        historiques = loader.cursor.fetchall()

        schemas = []
        for id_historique, xsd_filename, root_name, date_creation in historiques:
            loader.cursor.execute(
                "SELECT table_name, parent_table FROM DDL_XSD_TABLE_CONFIG "
                "WHERE id_historique = :1 ORDER BY ordre",
                [id_historique],
            )
            table_rows = loader.cursor.fetchall()
            table_names = [r[0] for r in table_rows]
            root_table = next((r[0] for r in table_rows if r[1] is None), None)

            missing = []
            for table_name in table_names:
                loader.cursor.execute(
                    "SELECT COUNT(*) FROM user_tables WHERE table_name = :1", [table_name]
                )
                if loader.cursor.fetchone()[0] == 0:
                    missing.append(table_name)

            schemas.append({
                "id_historique": id_historique,
                "xsd_filename": xsd_filename,
                "root_name": root_name,
                "root_table": root_table,
                "date_creation": date_creation,
                "table_names": table_names,
                "missing_tables": missing,
            })
        return schemas
    finally:
        loader.disconnect()


def rename_schema_root(id_historique, new_root_name):
    """
    Renomme la table racine d'un schéma créé via le Générateur DDL, ET
    propage le renommage à TOUTES ses tables filles -- car dans le
    mécanisme générique de l'app2, le nom de chaque table fille est
    construit à partir du préfixe de la racine (ex: TITRE, TITRE_ARTICLE,
    TITRE_PIECES_JOINTE...). Renommer uniquement la racine casserait le
    lien avec ses filles au prochain dépôt XML pour ce schéma.
    """
    new_root_name = _validate_name(new_root_name)

    loader = ddl_oracle.connect()
    try:
        ddl_oracle.ensure_meta_tables(loader)

        loader.cursor.execute(
            "SELECT table_name, parent_table FROM DDL_XSD_TABLE_CONFIG "
            "WHERE id_historique = :1 ORDER BY ordre",
            [id_historique],
        )
        rows = loader.cursor.fetchall()
        if not rows:
            raise ValueError("Schéma introuvable.")

        root_row = next((r for r in rows if r[1] is None), None)
        if root_row is None:
            raise ValueError("Table racine introuvable pour ce schéma.")
        old_prefix = root_row[0]

        if old_prefix == new_root_name:
            raise ValueError(f"La table s'appelle déjà {new_root_name}, rien à renommer.")

        # Vérifie que toutes les tables existent encore avant de commencer
        for table_name, _ in rows:
            loader.cursor.execute(
                "SELECT COUNT(*) FROM user_tables WHERE table_name = :1", [table_name]
            )
            if loader.cursor.fetchone()[0] == 0:
                raise ValueError(
                    f"La table {table_name} n'existe plus dans Oracle -- "
                    f"impossible de renommer ce schéma tant qu'il manque des tables."
                )

        # Calcule les nouveaux noms (même suffixe, préfixe racine remplacé)
        # et vérifie qu'aucun ne collisionne avec une table existante hors
        # de ce schéma.
        rename_pairs = []
        for table_name, _ in rows:
            new_name = new_root_name + table_name[len(old_prefix):]
            if new_name != table_name:
                loader.cursor.execute(
                    "SELECT COUNT(*) FROM user_tables WHERE table_name = :1", [new_name]
                )
                if loader.cursor.fetchone()[0] > 0:
                    raise ValueError(
                        f"Impossible de renommer {table_name} en {new_name} : "
                        f"une table {new_name} existe déjà."
                    )
            rename_pairs.append((table_name, new_name))

        for old_name, new_name in rename_pairs:
            if old_name != new_name:
                loader.cursor.execute(f"ALTER TABLE {old_name} RENAME TO {new_name}")

        # Met à jour les métadonnées (DDL_XSD_TABLE_CONFIG + DDL_XSD_HISTORIQUE)
        for old_name, new_name in rename_pairs:
            if old_name != new_name:
                loader.cursor.execute(
                    "UPDATE DDL_XSD_TABLE_CONFIG SET table_name = :1 "
                    "WHERE id_historique = :2 AND table_name = :3",
                    [new_name, id_historique, old_name],
                )
                loader.cursor.execute(
                    "UPDATE DDL_XSD_TABLE_CONFIG SET parent_table = :1 "
                    "WHERE id_historique = :2 AND parent_table = :3",
                    [new_name, id_historique, old_name],
                )

        loader.cursor.execute(
            "UPDATE DDL_XSD_HISTORIQUE SET root_name = :1 WHERE id_historique = :2",
            [new_root_name, id_historique],
        )

        loader.connection.commit()
    finally:
        loader.disconnect()