"""
web/ddl_oracle.py

Partie Oracle du generateur DDL (ex-app2). Separee de ddl_generator.py
pour garder la logique pure de generation du DDL (xsd_xml_to_ddl.py)
totalement independante de la base de donnees, comme demande.

Deux connexions Oracle distinctes :
  - connect() : schema SANA (destination -- tables generees, tables
    techniques de l'appli).
  - connect_liasse() : schema LIASSE (source -- lecture de DOSSIER
    uniquement, table referencee sans prefixe puisque la connexion se
    fait directement en tant qu'utilisateur liasse).

Les fonctions de LECTURE acceptent un parametre optionnel `loader` : si
fourni, il est reutilise et n'est PAS ferme ici (permet de partager une
seule connexion Oracle sur toute une requete HTTP, ex. le dashboard qui
ouvre un seul loader SANA et un seul loader LIASSE au lieu d'une
connexion par fonction). Si absent, comportement inchange :
connexion/deconnexion propre a l'appel.

IMPORTANT : la colonne ACTIVITE de DOSSIER (schema LIASSE) contient des
valeurs avec des espaces parasites en fin de chaine (ex: '5.2  ' au lieu
de '5.2', verifie via LENGTH() -- 5 caracteres au lieu de 3). Toutes les
comparaisons sur ACTIVITE utilisent donc TRIM() pour eviter de rejeter a
tort des lignes qui matchent semantiquement.
"""

import os
import re
import time

import config
from src.data_loader import DataLoader
from src.xsd_xml_to_ddl import get_available_tables

XSD_STORE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "xsd_ddl")
os.makedirs(XSD_STORE_DIR, exist_ok=True)


def _friendly_insert_error(exc, table_name):
    """
    Traduit une erreur Oracle courante en message clair pour le Journal
    des depots, au lieu du texte brut ORA-xxxxx.
    """
    text = str(exc)
    match = re.search(r'ORA-01400.*?"[^"]*"\."[^"]*"\."([^"]+)"', text)
    if match:
        column_name = match.group(1)
        return (
            f"Le champ {column_name} est vide dans ce XML mais NOT NULL dans la "
            f"table {table_name} -- soit le XSD le declare obligatoire (minOccurs > 0), "
            f"soit il a ete choisi comme cle (primaire ou de liaison) lors de la "
            f"creation de la table. Insertion refusee. Verifie le XML, ou recree la "
            f"table via le Generateur DDL en changeant ce choix si besoin."
        )
    return f"Erreur d'insertion dans {table_name} : {exc}"


def connect():
    """Connexion vers le schema SANA (destination)."""
    loader = DataLoader(
        username=config.DB_USERNAME,
        password=config.DB_PASSWORD,
        dsn=config.DB_DSN,
    )
    loader.connect()
    return loader


def connect_liasse():
    """Connexion vers le schema LIASSE (source), utilisee pour lire DOSSIER."""
    loader = DataLoader(
        username=config.LIASSE_DB_USERNAME,
        password=config.LIASSE_DB_PASSWORD,
        dsn=config.LIASSE_DB_DSN,
    )
    loader.connect()
    return loader


def ensure_meta_tables(loader):
    """Cree les tables techniques d'historique/config si elles n'existent pas encore."""
    loader.cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name = 'DDL_XSD_HISTORIQUE'"
    )
    if loader.cursor.fetchone()[0] == 0:
        loader.cursor.execute("""
            CREATE TABLE DDL_XSD_HISTORIQUE (
                id_historique NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                xsd_filename VARCHAR2(255) NOT NULL,
                xsd_stored_path VARCHAR2(500) NOT NULL,
                root_name VARCHAR2(255) NOT NULL,
                date_creation TIMESTAMP DEFAULT SYSTIMESTAMP
            )
        """)

    loader.cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name = 'DDL_XSD_TABLE_CONFIG'"
    )
    if loader.cursor.fetchone()[0] == 0:
        loader.cursor.execute("""
            CREATE TABLE DDL_XSD_TABLE_CONFIG (
                id_historique NUMBER NOT NULL,
                table_name VARCHAR2(128) NOT NULL,
                parent_table VARCHAR2(128),
                fk_column VARCHAR2(128),
                pk_columns VARCHAR2(500),
                ordre NUMBER NOT NULL,
                CONSTRAINT PK_DDL_TABLE_CONFIG PRIMARY KEY (id_historique, table_name),
                CONSTRAINT FK_DDL_TABLE_CONFIG FOREIGN KEY (id_historique)
                    REFERENCES DDL_XSD_HISTORIQUE(id_historique)
            )
        """)

    loader.cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name = 'DDL_XSD_PENDING_FILES'"
    )
    if loader.cursor.fetchone()[0] == 0:
        loader.cursor.execute("""
            CREATE TABLE DDL_XSD_PENDING_FILES (
                filename VARCHAR2(500) PRIMARY KEY,
                id_historique NUMBER NOT NULL,
                date_depot TIMESTAMP DEFAULT SYSTIMESTAMP,
                CONSTRAINT FK_DDL_PENDING_HIST FOREIGN KEY (id_historique)
                    REFERENCES DDL_XSD_HISTORIQUE(id_historique)
            )
        """)

    loader.cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name = 'SCHEMA_TYPE_CONFIG'"
    )
    if loader.cursor.fetchone()[0] == 0:
        loader.cursor.execute("""
            CREATE TABLE SCHEMA_TYPE_CONFIG (
                root_name              VARCHAR2(255) PRIMARY KEY,
                code_type_dossier      VARCHAR2(50) NOT NULL,
                colonne_numero_demande VARCHAR2(128),
                activite                VARCHAR2(100),
                date_debut              DATE,
                date_fin                 DATE,
                derniere_synchro        TIMESTAMP
            )
        """)

    loader.cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name = 'SCHEMA_COLUMN_MAPPING'"
    )
    if loader.cursor.fetchone()[0] == 0:
        loader.cursor.execute("""
            CREATE TABLE SCHEMA_COLUMN_MAPPING (
                root_name      VARCHAR2(255) NOT NULL,
                source_column  VARCHAR2(128) NOT NULL,
                target_column  VARCHAR2(128) NOT NULL,
                CONSTRAINT PK_SCHEMA_COLUMN_MAPPING PRIMARY KEY (root_name, source_column),
                CONSTRAINT FK_SCHEMA_COLUMN_MAPPING FOREIGN KEY (root_name)
                    REFERENCES SCHEMA_TYPE_CONFIG(root_name)
            )
        """)

    loader.connection.commit()


def store_xsd_permanently(xsd_file_storage, root_name):
    """Sauvegarde une copie durable du XSD depose."""
    import uuid
    safe_name = f"{uuid.uuid4().hex}_{xsd_file_storage.filename}"
    dest = os.path.join(XSD_STORE_DIR, safe_name)
    xsd_file_storage.save(dest)
    return dest


def create_tables_and_record(loader, xsd_filename, xsd_stored_path, root_name,
                              selected_tables_with_config):
    """Execute le CREATE TABLE de chaque table selectionnee, puis enregistre l'historique."""
    ensure_meta_tables(loader)

    id_var = loader.cursor.var(int)
    loader.cursor.execute(
        "INSERT INTO DDL_XSD_HISTORIQUE (xsd_filename, xsd_stored_path, root_name) "
        "VALUES (:1, :2, :3) RETURNING id_historique INTO :4",
        [xsd_filename, xsd_stored_path, root_name, id_var]
    )
    id_historique = id_var.getvalue()[0]

    for ordre, table_obj, ddl_sql, fk_column_name, own_pk_columns, parent_name in selected_tables_with_config:
        loader.cursor.execute(ddl_sql.rstrip(";"))
        loader.cursor.execute(
            "INSERT INTO DDL_XSD_TABLE_CONFIG "
            "(id_historique, table_name, parent_table, fk_column, pk_columns, ordre) "
            "VALUES (:1, :2, :3, :4, :5, :6)",
            [
                id_historique,
                table_obj.sql_name,
                parent_name,
                fk_column_name,
                ",".join(own_pk_columns) if own_pk_columns else None,
                ordre,
            ]
        )

    loader.connection.commit()
    return id_historique


def queue_file_for_schema(filename, id_historique):
    loader = connect()
    try:
        ensure_meta_tables(loader)
        loader.cursor.execute(
            "DELETE FROM DDL_XSD_PENDING_FILES WHERE filename = :1", [filename]
        )
        loader.cursor.execute(
            "INSERT INTO DDL_XSD_PENDING_FILES (filename, id_historique) VALUES (:1, :2)",
            [filename, id_historique],
        )
        loader.connection.commit()
    finally:
        loader.disconnect()


def get_queued_schema(filename):
    loader = connect()
    try:
        ensure_meta_tables(loader)
        loader.cursor.execute(
            "SELECT id_historique FROM DDL_XSD_PENDING_FILES WHERE filename = :1",
            [filename],
        )
        row = loader.cursor.fetchone()
        return row[0] if row else None
    finally:
        loader.disconnect()


def remove_queued_schema(filename):
    loader = connect()
    try:
        loader.cursor.execute(
            "DELETE FROM DDL_XSD_PENDING_FILES WHERE filename = :1", [filename]
        )
        loader.connection.commit()
    finally:
        loader.disconnect()


def list_historique(loader=None):
    """
    Pour peupler la liste deroulante des XSD deja utilises.
    loader : connexion SANA deja ouverte a reutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect()
    try:
        ensure_meta_tables(loader)
        loader.cursor.execute(
            "SELECT id_historique, xsd_filename, root_name, date_creation "
            "FROM DDL_XSD_HISTORIQUE ORDER BY date_creation DESC"
        )
        return [
            {"id_historique": r[0], "xsd_filename": r[1], "root_name": r[2], "date_creation": r[3]}
            for r in loader.cursor.fetchall()
        ]
    finally:
        if own_connection:
            loader.disconnect()


def insert_xml_into_tables(id_historique, xml_path):
    """
    Reparse le XML depose avec le XSD deja stocke pour cet historique
    (meme fonction get_available_tables que le generateur DDL -- donc
    memes tables/colonnes), verifie que les tables existent toujours en
    base, puis insere les lignes dans l'ordre parent -> enfants.

    Table RACINE (parent_name is None) avec cle naturelle : MERGE au lieu
    d'un simple INSERT. Une ligne "squelette" (NUMERO_DOSSIER seul) a pu
    deja etre creee par sync_dossiers_liasse_pour_type (flux LIASSE) avant
    ce depot XML -- un INSERT simple echouerait alors sur la contrainte
    PK. Le MERGE complete/ecrase cette ligne avec les donnees du XML au
    lieu d'echouer.

    Tables FILLES avec cle naturelle : comportement inchange (INSERT
    simple) -- un re-depot du meme document doit toujours echouer ici,
    c'est le garde-fou contre les doublons de lignes filles.

    Retourne {"success": True, "inserted": n} ou {"error": "..."}.
    """
    loader = connect()
    try:
        ensure_meta_tables(loader)

        loader.cursor.execute(
            "SELECT xsd_stored_path, root_name FROM DDL_XSD_HISTORIQUE WHERE id_historique = :1",
            [id_historique],
        )
        row = loader.cursor.fetchone()
        if not row:
            return {"error": "Cet historique de schema est introuvable."}
        xsd_stored_path, root_name = row

        loader.cursor.execute(
            "SELECT table_name, parent_table, fk_column, pk_columns FROM DDL_XSD_TABLE_CONFIG "
            "WHERE id_historique = :1 ORDER BY ordre",
            [id_historique],
        )
        configs = loader.cursor.fetchall()
        if not configs:
            return {"error": "Aucune table enregistree pour ce schema."}

        config_by_name = {
            c[0]: {
                "parent": c[1],
                "fk_column": c[2],
                "pk_columns": c[3].split(",") if c[3] else [],
            }
            for c in configs
        }

        missing = []
        for table_name in config_by_name:
            loader.cursor.execute(
                "SELECT COUNT(*) FROM user_tables WHERE table_name = :1", [table_name]
            )
            if loader.cursor.fetchone()[0] == 0:
                missing.append(table_name)
        if missing:
            return {
                "error": (
                    "Table(s) introuvable(s) en base : " + ", ".join(missing) +
                    ". Recree-les via le generateur DDL avant de deposer ce fichier."
                )
            }

        tables = get_available_tables(xsd_stored_path, xml_path, root_name)
        tables_by_name = {t.sql_name: t for t in tables}

        id_map = {}
        total_inserted = 0

        for ordre_table_name, cfg in config_by_name.items():
            t = tables_by_name.get(ordre_table_name)
            if t is None or not t.rows:
                continue

            pk_cols = cfg["pk_columns"]
            fk_col = cfg["fk_column"]
            parent_name = cfg["parent"]
            use_natural_pk = bool(pk_cols)
            parent_table_obj = tables_by_name.get(parent_name) if parent_name else None

            for row in t.rows:
                cols, vals = [], []

                if parent_name:
                    parent_local_id = row.get("ID_PARENT")
                    if fk_col:
                        parent_row = (
                            parent_table_obj.rows[parent_local_id - 1]
                            if (parent_table_obj and parent_local_id) else {}
                        )
                        cols.append(fk_col)
                        vals.append(parent_row.get(fk_col))
                    else:
                        real_parent_id = id_map.get(parent_name, {}).get(parent_local_id)
                        cols.append(f"ID_{parent_name}")
                        vals.append(real_parent_id)

                for c in t.column_types:
                    cols.append(c)
                    vals.append(row.get(c))

                placeholders = [f":{i + 1}" for i in range(len(cols))]

                try:
                    if use_natural_pk:
                        if not parent_name:
                            # Table RACINE : MERGE au lieu d'un simple INSERT.
                            non_pk_cols = [c for c in cols if c not in pk_cols]
                            on_clause = " AND ".join(f"t.{c} = s.{c}" for c in pk_cols)
                            select_list_sql = ", ".join(
                                f":{i + 1} AS {c}" for i, c in enumerate(cols)
                            )
                            insert_cols_sql = ", ".join(cols)
                            insert_vals_sql = ", ".join(f"s.{c}" for c in cols)

                            if non_pk_cols:
                                set_clause = ", ".join(f"t.{c} = s.{c}" for c in non_pk_cols)
                                sql = (
                                    f"MERGE INTO {ordre_table_name} t "
                                    f"USING (SELECT {select_list_sql} FROM dual) s "
                                    f"ON ({on_clause}) "
                                    f"WHEN MATCHED THEN UPDATE SET {set_clause} "
                                    f"WHEN NOT MATCHED THEN INSERT ({insert_cols_sql}) "
                                    f"VALUES ({insert_vals_sql})"
                                )
                            else:
                                sql = (
                                    f"MERGE INTO {ordre_table_name} t "
                                    f"USING (SELECT {select_list_sql} FROM dual) s "
                                    f"ON ({on_clause}) "
                                    f"WHEN NOT MATCHED THEN INSERT ({insert_cols_sql}) "
                                    f"VALUES ({insert_vals_sql})"
                                )
                            loader.cursor.execute(sql, vals)
                        else:
                            # Table fille avec cle naturelle : INSERT simple,
                            # inchange -- doit echouer sur un re-depot du
                            # meme document (garde-fou anti-doublon).
                            sql = (
                                f"INSERT INTO {ordre_table_name} ({', '.join(cols)}) "
                                f"VALUES ({', '.join(placeholders)})"
                            )
                            loader.cursor.execute(sql, vals)

                        key_val = (
                            tuple(row.get(c) for c in pk_cols)
                            if len(pk_cols) > 1 else row.get(pk_cols[0])
                        )
                        id_map.setdefault(ordre_table_name, {})[row["ID"]] = key_val
                    else:
                        pk_name = f"ID_{ordre_table_name}"
                        new_id_var = loader.cursor.var(int)
                        sql = (
                            f"INSERT INTO {ordre_table_name} ({', '.join(cols)}) "
                            f"VALUES ({', '.join(placeholders)}) "
                            f"RETURNING {pk_name} INTO :{len(cols) + 1}"
                        )
                        loader.cursor.execute(sql, vals + [new_id_var])
                        id_map.setdefault(ordre_table_name, {})[row["ID"]] = new_id_var.getvalue()[0]
                    total_inserted += 1
                except Exception as exc:
                    loader.connection.rollback()
                    return {"error": _friendly_insert_error(exc, ordre_table_name)}

        loader.connection.commit()
        return {"success": True, "inserted": total_inserted}
    finally:
        loader.disconnect()


# ---------------------------------------------------------------
# Synchronisation des schemas avec DOSSIER (schema LIASSE)
# ---------------------------------------------------------------

def get_distinct_code_types_dossier(loader=None):
    """
    Valeurs distinctes de CODE_TYPE_DOSSIER dans DOSSIER (schema LIASSE).
    loader : connexion LIASSE deja ouverte a reutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect_liasse()
    try:
        loader.cursor.execute(
            "SELECT DISTINCT CODE_TYPE_DOSSIER FROM DOSSIER "
            "WHERE CODE_TYPE_DOSSIER IS NOT NULL ORDER BY CODE_TYPE_DOSSIER"
        )
        return [r[0] for r in loader.cursor.fetchall()]
    finally:
        if own_connection:
            loader.disconnect()


def get_distinct_activites_dossier(loader=None):
    """
    Valeurs distinctes de ACTIVITE dans DOSSIER (schema LIASSE), TRIM()
    car certaines valeurs ont des espaces parasites en fin de chaine
    (ex: '5.2  ' au lieu de '5.2', confirme via LENGTH() = 5) -- sans ce
    TRIM, le filtre du dashboard ne matchait jamais ces lignes-la.
    loader : connexion LIASSE deja ouverte a reutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect_liasse()
    try:
        loader.cursor.execute(
            "SELECT DISTINCT TRIM(ACTIVITE) FROM DOSSIER "
            "WHERE ACTIVITE IS NOT NULL ORDER BY TRIM(ACTIVITE)"
        )
        return [r[0] for r in loader.cursor.fetchall()]
    finally:
        if own_connection:
            loader.disconnect()


def get_dossier_columns(loader=None):
    """
    Liste les colonnes reelles de DOSSIER (schema LIASSE) -- colonne
    SOURCE possible pour une correspondance -- en excluant NUMERO_DOSSIER
    qui sert deja de cle de jointure.
    loader : connexion LIASSE deja ouverte a reutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect_liasse()
    try:
        loader.cursor.execute(
            "SELECT column_name FROM user_tab_columns "
            "WHERE table_name = 'DOSSIER' AND column_name != 'NUMERO_DOSSIER' "
            "ORDER BY column_id"
        )
        return [r[0] for r in loader.cursor.fetchall()]
    finally:
        if own_connection:
            loader.disconnect()


def get_root_table_columns(root_name, loader=None):
    """
    Liste les colonnes reelles de la table racine donnee (schema SANA) --
    colonne CIBLE possible pour une correspondance.
    loader : connexion SANA deja ouverte a reutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect()
    try:
        loader.cursor.execute(
            "SELECT column_name FROM user_tab_columns "
            "WHERE table_name = :1 AND column_name != 'NUMERO_DOSSIER' "
            "ORDER BY column_id",
            [root_name]
        )
        return [r[0] for r in loader.cursor.fetchall()]
    finally:
        if own_connection:
            loader.disconnect()


def get_schema_column_mappings(root_name, loader=None):
    """
    Correspondances de colonnes configurees pour ce schema.
    loader : connexion SANA deja ouverte a reutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect()
    try:
        ensure_meta_tables(loader)
        loader.cursor.execute(
            "SELECT source_column, target_column FROM SCHEMA_COLUMN_MAPPING "
            "WHERE root_name = :1 ORDER BY source_column",
            [root_name]
        )
        return [{"source": r[0], "target": r[1]} for r in loader.cursor.fetchall()]
    finally:
        if own_connection:
            loader.disconnect()


def save_schema_column_mappings(root_name, pairs):
    """
    Remplace toutes les correspondances de colonnes pour ce root_name.
    pairs : liste de tuples (source_column, target_column).
    """
    loader = connect()
    try:
        ensure_meta_tables(loader)
        loader.cursor.execute(
            "DELETE FROM SCHEMA_COLUMN_MAPPING WHERE root_name = :1", [root_name]
        )
        for source_column, target_column in pairs:
            if not source_column or not target_column:
                continue
            loader.cursor.execute(
                "INSERT INTO SCHEMA_COLUMN_MAPPING (root_name, source_column, target_column) "
                "VALUES (:1, :2, :3)",
                [root_name, source_column, target_column]
            )
        loader.connection.commit()
    finally:
        loader.disconnect()


def get_schema_types(loader=None):
    """
    Retourne toutes les correspondances configurees :
    root_name -> {code_type_dossier, activites (liste), activite_raw,
                  date_debut, date_fin, derniere_synchro}.
    loader : connexion SANA deja ouverte a reutiliser (optionnel).
    """
    own_connection = loader is None
    if own_connection:
        loader = connect()
    try:
        ensure_meta_tables(loader)
        loader.cursor.execute("""
            SELECT root_name, code_type_dossier, activite, date_debut, date_fin, derniere_synchro
            FROM SCHEMA_TYPE_CONFIG
            ORDER BY root_name
        """)
        result = {}
        for r in loader.cursor.fetchall():
            result[r[0]] = {
                "code_type_dossier": r[1],
                "activites": [a.strip() for a in r[2].split(",")] if r[2] else [],
                "activite_raw": r[2] or "",
                "date_debut": r[3],
                "date_fin": r[4],
                "derniere_synchro": r[5],
            }
        return result
    finally:
        if own_connection:
            loader.disconnect()


def update_schema_type(root_name, code_type_dossier, activite_raw, date_debut, date_fin):
    """
    Cree ou met a jour la partie "generale" d'une correspondance (hors
    colonnes, gerees a part par save_schema_column_mappings).
    Utilise des binds NOMMES (voir explication ci-dessous).
    """
    loader = connect()
    try:
        ensure_meta_tables(loader)
        loader.cursor.execute(
            """
            MERGE INTO SCHEMA_TYPE_CONFIG t
            USING (SELECT :root_name AS root_name FROM dual) d
            ON (t.root_name = d.root_name)
            WHEN MATCHED THEN UPDATE SET
                code_type_dossier = :code_type_dossier,
                activite = :activite_raw,
                date_debut = CASE WHEN :date_debut IS NOT NULL THEN TO_DATE(:date_debut, 'YYYY-MM-DD') ELSE NULL END,
                date_fin = CASE WHEN :date_fin IS NOT NULL THEN TO_DATE(:date_fin, 'YYYY-MM-DD') ELSE NULL END
            WHEN NOT MATCHED THEN INSERT
                (root_name, code_type_dossier, activite, date_debut, date_fin)
            VALUES
                (:root_name, :code_type_dossier, :activite_raw,
                 CASE WHEN :date_debut IS NOT NULL THEN TO_DATE(:date_debut, 'YYYY-MM-DD') ELSE NULL END,
                 CASE WHEN :date_fin IS NOT NULL THEN TO_DATE(:date_fin, 'YYYY-MM-DD') ELSE NULL END)
            """,
            root_name=root_name,
            code_type_dossier=code_type_dossier,
            activite_raw=activite_raw or None,
            date_debut=date_debut or None,
            date_fin=date_fin or None,
        )
        loader.connection.commit()
    finally:
        loader.disconnect()


def delete_schema_type(root_name):
    """Supprime une correspondance et ses mappings de colonnes associes."""
    loader = connect()
    try:
        loader.cursor.execute(
            "DELETE FROM SCHEMA_COLUMN_MAPPING WHERE root_name = :1", [root_name]
        )
        loader.cursor.execute(
            "DELETE FROM SCHEMA_TYPE_CONFIG WHERE root_name = :1", [root_name]
        )
        loader.connection.commit()
    finally:
        loader.disconnect()


def get_queued_root_name(filename):
    loader = connect()
    try:
        ensure_meta_tables(loader)
        loader.cursor.execute("""
            SELECT h.root_name
            FROM DDL_XSD_PENDING_FILES p
            JOIN DDL_XSD_HISTORIQUE h ON h.id_historique = p.id_historique
            WHERE p.filename = :1
        """, [filename])
        row = loader.cursor.fetchone()
        return row[0] if row else None
    finally:
        loader.disconnect()


def sync_liasse_to_stat_dossier():
    """
    Compare DOSSIER (schema LIASSE) a STAT_DOSSIER (schema SANA, "photo"
    de l'etat lors du dernier batch), MERGE pour que STAT_DOSSIER reflete
    a nouveau LIASSE, et retourne la liste des NUMERO_DOSSIER nouveaux ou
    modifies depuis le dernier passage -- ce sont ceux qui necessitent un
    retraitement de leur XML.

    La comparaison se fait en Python (lecture des deux tables, puis
    diff ligne a ligne) plutot que de MERGE aveuglement tout, pour savoir
    precisement CE QUI a change -- un MERGE seul ne donne pas cette info
    nativement.

    ATTENTION : si un meme NUMERO_DOSSIER existe en double dans
    LIASSE.DOSSIER (avec des valeurs differentes -- cas rencontre en test
    avec CODE_TYPE_DOSSIER), le comportement n'est pas deterministe :
    STAT_DOSSIER n'a qu'une ligne par NUMERO_DOSSIER (PK), donc la
    comparaison detecte un "changement" a chaque run tant que l'ordre de
    lecture SQL n'est pas garanti. Point ouvert a clarifier avec la
    tutrice -- pas encore de regle de priorite codee.

    Retourne : liste de NUMERO_DOSSIER (str) nouveaux ou modifies.
    """
    liasse_loader = connect_liasse()
    try:
        liasse_loader.cursor.execute("""
            SELECT NUMERO_DOSSIER, CODE_TYPE_DOSSIER, NUMERO_DEMANDE,
                   ACTIF_CLOS, DATE_CREATION, DATE_CLOTURE, TRIM(ACTIVITE)
            FROM DOSSIER
        """)
        source_rows = liasse_loader.cursor.fetchall()
    finally:
        liasse_loader.disconnect()

    if not source_rows:
        return []

    sana_loader = connect()
    try:
        sana_loader.cursor.execute("""
            SELECT NUMERO_DOSSIER, CODE_TYPE_DOSSIER, NUMERO_DEMANDE,
                   ACTIF_CLOS, DATE_CREATION, DATE_CLOTURE, ACTIVITE
            FROM STAT_DOSSIER
        """)
        existing = {row[0]: row[1:] for row in sana_loader.cursor.fetchall()}

        changed_dossiers = []

        merge_sql = """
            MERGE INTO STAT_DOSSIER t
            USING (
                SELECT
                    :numero_dossier      AS NUMERO_DOSSIER,
                    :code_type_dossier   AS CODE_TYPE_DOSSIER,
                    :numero_demande      AS NUMERO_DEMANDE,
                    :actif_clos          AS ACTIF_CLOS,
                    :date_creation       AS DATE_CREATION,
                    :date_cloture        AS DATE_CLOTURE,
                    :activite            AS ACTIVITE
                FROM dual
            ) d
            ON (t.NUMERO_DOSSIER = d.NUMERO_DOSSIER)
            WHEN MATCHED THEN UPDATE SET
                t.CODE_TYPE_DOSSIER = d.CODE_TYPE_DOSSIER,
                t.NUMERO_DEMANDE    = d.NUMERO_DEMANDE,
                t.ACTIF_CLOS        = d.ACTIF_CLOS,
                t.DATE_CREATION     = d.DATE_CREATION,
                t.DATE_CLOTURE      = d.DATE_CLOTURE,
                t.ACTIVITE          = d.ACTIVITE
            WHEN NOT MATCHED THEN INSERT
                (NUMERO_DOSSIER, CODE_TYPE_DOSSIER, NUMERO_DEMANDE,
                 ACTIF_CLOS, DATE_CREATION, DATE_CLOTURE, ACTIVITE)
            VALUES
                (d.NUMERO_DOSSIER, d.CODE_TYPE_DOSSIER, d.NUMERO_DEMANDE,
                 d.ACTIF_CLOS, d.DATE_CREATION, d.DATE_CLOTURE, d.ACTIVITE)
        """

        for row in source_rows:
            (numero_dossier, code_type_dossier, numero_demande,
             actif_clos, date_creation, date_cloture, activite) = row

            new_values = (code_type_dossier, numero_demande, actif_clos,
                          date_creation, date_cloture, activite)
            old_values = existing.get(numero_dossier)

            is_new = old_values is None
            is_modified = (old_values is not None and old_values != new_values)

            if is_new or is_modified:
                changed_dossiers.append(numero_dossier)

                sana_loader.cursor.execute(
                    merge_sql,
                    numero_dossier=numero_dossier,
                    code_type_dossier=code_type_dossier,
                    numero_demande=numero_demande,
                    actif_clos=actif_clos,
                    date_creation=date_creation,
                    date_cloture=date_cloture,
                    activite=activite,
                )

        sana_loader.connection.commit()
        return changed_dossiers

    except Exception:
        sana_loader.connection.rollback()
        raise
    finally:
        sana_loader.disconnect()


def sync_dossiers_liasse_pour_type(root_name):
    """
    Synchronise la table racine (schema SANA) depuis DOSSIER (schema
    LIASSE), via deux connexions distinctes (connect_liasse() pour lire,
    connect() pour ecrire) -- lecture complete des dossiers cotes LIASSE,
    puis un MERGE par dossier cote SANA.

    Si aucune correspondance de colonne n'est configuree pour ce schema
    (SCHEMA_COLUMN_MAPPING vide), le MERGE se limite a garantir qu'une
    ligne "squelette" existe (NUMERO_DOSSIER seul) pour chaque dossier
    filtre -- LIASSE sert alors uniquement a identifier les dossiers
    concernes, tout le contenu venant du XML depose ensuite. C'est
    insert_xml_into_tables() qui complete/ecrase cette ligne squelette
    (MERGE cote XML, plus un simple INSERT qui aurait echoue sur la ligne
    deja creee ici).

    date_debut/date_fin jouent un double role :
      1. Filtre sur DATE_CREATION des dossiers a synchroniser.
      2. Fenetre d'activation du batch lui-meme : ne s'execute que si
         aujourd'hui est compris entre date_debut et date_fin.

    Ne fait rien si la config generale est absente, ou si les
    dates/activites ne sont pas renseignees.

    TRACE_EXECUTION a NUMERO_DOSSIER en cle primaire : MERGE, pas INSERT.
    Met a jour derniere_synchro sur ce root_name apres un run reussi.
    """
    cfg = get_schema_types().get(root_name)
    if cfg is None or not cfg["date_debut"] or not cfg["date_fin"]:
        return
    if not cfg["activites"]:
        return

    from datetime import date
    today = date.today()
    if today < cfg["date_debut"].date() or today > cfg["date_fin"].date():
        return

    mappings = get_schema_column_mappings(root_name)

    sana_check = connect()
    try:
        sana_check.cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :1", [root_name]
        )
        if sana_check.cursor.fetchone()[0] == 0:
            return
    finally:
        sana_check.disconnect()

    activite_binds = {f"activite{i}": val for i, val in enumerate(cfg["activites"])}
    activite_placeholders = ", ".join(f":{k}" for k in activite_binds)

    date_debut_str = cfg["date_debut"].strftime("%Y-%m-%d")
    date_fin_str = cfg["date_fin"].strftime("%Y-%m-%d")

    source_columns = [m["source"] for m in mappings]
    select_columns_sql = ", ".join(["NUMERO_DOSSIER"] + source_columns)

    liasse_loader = connect_liasse()
    try:
        liasse_loader.cursor.execute(
            f"""
            SELECT {select_columns_sql}
            FROM DOSSIER
            WHERE CODE_TYPE_DOSSIER = :code_type
              AND TRIM(ACTIVITE) IN ({activite_placeholders})
              AND DATE_CREATION BETWEEN TO_DATE(:date_debut, 'YYYY-MM-DD') AND TO_DATE(:date_fin, 'YYYY-MM-DD')
            """,
            code_type=cfg["code_type_dossier"],
            date_debut=date_debut_str, date_fin=date_fin_str,
            **activite_binds,
        )
        dossiers = liasse_loader.cursor.fetchall()
    finally:
        liasse_loader.disconnect()

    if not dossiers:
        return

    sana_loader = connect()
    t_debut = time.time()
    try:
        using_select = ", ".join(
            [":numero_dossier AS NUMERO_DOSSIER"]
            + [f":src{i} AS SRC{i}" for i in range(len(mappings))]
        )
        insert_columns_sql = ", ".join(["NUMERO_DOSSIER"] + [m["target"] for m in mappings])
        insert_values_sql = ", ".join(
            ["d.NUMERO_DOSSIER"] + [f"d.SRC{i}" for i in range(len(mappings))]
        )

        if mappings:
            update_set_sql = ", ".join(
                f"t.{m['target']} = d.SRC{i}" for i, m in enumerate(mappings)
            )
            merge_sql = f"""
                MERGE INTO {root_name} t
                USING (SELECT {using_select} FROM dual) d
                ON (t.NUMERO_DOSSIER = d.NUMERO_DOSSIER)
                WHEN MATCHED THEN
                    UPDATE SET {update_set_sql}
                WHEN NOT MATCHED THEN
                    INSERT ({insert_columns_sql})
                    VALUES ({insert_values_sql})
            """
        else:
            merge_sql = f"""
                MERGE INTO {root_name} t
                USING (SELECT {using_select} FROM dual) d
                ON (t.NUMERO_DOSSIER = d.NUMERO_DOSSIER)
                WHEN NOT MATCHED THEN
                    INSERT ({insert_columns_sql})
                    VALUES ({insert_values_sql})
            """

        for row in dossiers:
            numero_dossier = row[0]
            binds = {"numero_dossier": numero_dossier}
            for i, val in enumerate(row[1:]):
                binds[f"src{i}"] = val
            sana_loader.cursor.execute(merge_sql, binds)

        duree = round(time.time() - t_debut, 2)

        for row in dossiers:
            numero_dossier = row[0]
            sana_loader.cursor.execute(
                """
                MERGE INTO TRACE_EXECUTION t
                USING (SELECT :numero_dossier AS numero_dossier FROM dual) d
                ON (t.NUMERO_DOSSIER = d.numero_dossier)
                WHEN MATCHED THEN UPDATE SET
                    DATE_EXECUTION = SYSTIMESTAMP,
                    DUREE = :duree,
                    LIGNES_CHARGEES = :lignes_chargees,
                    STATUT = :statut,
                    ERREUR = :erreur
                WHEN NOT MATCHED THEN INSERT
                    (NUMERO_DOSSIER, DATE_EXECUTION, DUREE, LIGNES_CHARGEES, STATUT, ERREUR)
                VALUES
                    (:numero_dossier, SYSTIMESTAMP, :duree, :lignes_chargees, :statut, :erreur)
                """,
                numero_dossier=numero_dossier,
                duree=duree,
                lignes_chargees=len(mappings),
                statut="OK",
                erreur=None,
            )

        sana_loader.cursor.execute(
            "UPDATE SCHEMA_TYPE_CONFIG SET derniere_synchro = SYSTIMESTAMP WHERE root_name = :1",
            [root_name]
        )
        sana_loader.connection.commit()

    except Exception as exc:
        sana_loader.connection.rollback()
        duree = round(time.time() - t_debut, 2)
        for row in dossiers:
            numero_dossier = row[0]
            sana_loader.cursor.execute(
                """
                MERGE INTO TRACE_EXECUTION t
                USING (SELECT :numero_dossier AS numero_dossier FROM dual) d
                ON (t.NUMERO_DOSSIER = d.numero_dossier)
                WHEN MATCHED THEN UPDATE SET
                    DATE_EXECUTION = SYSTIMESTAMP,
                    DUREE = :duree,
                    LIGNES_CHARGEES = :lignes_chargees,
                    STATUT = :statut,
                    ERREUR = :erreur
                WHEN NOT MATCHED THEN INSERT
                    (NUMERO_DOSSIER, DATE_EXECUTION, DUREE, LIGNES_CHARGEES, STATUT, ERREUR)
                VALUES
                    (:numero_dossier, SYSTIMESTAMP, :duree, :lignes_chargees, :statut, :erreur)
                """,
                numero_dossier=numero_dossier,
                duree=duree,
                lignes_chargees=0,
                statut="ERREUR",
                erreur=str(exc)[:4000],
            )
        sana_loader.connection.commit()
        raise
    finally:
        sana_loader.disconnect()