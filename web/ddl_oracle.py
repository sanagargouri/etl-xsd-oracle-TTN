"""
web/ddl_oracle.py

Partie Oracle du generateur DDL (ex-app2). Separee de ddl_generator.py
pour garder la logique pure de generation du DDL (xsd_xml_to_ddl.py)
totalement independante de la base de donnees, comme demande.

Deux responsabilites :
  1. Executer le DDL genere contre Oracle quand l'utilisateur clique sur
     "Create table", et memoriser dans DDL_XSD_HISTORIQUE / DDL_XSD_TABLE_CONFIG
     quel XSD a produit quelles tables (avec quelles cles), pour pouvoir
     les reutiliser plus tard depuis "Deposer un fichier".
  2. Inserer les donnees d'un XML dans des tables deja creees, en
     choisissant un XSD deja utilise dans la liste DDL_XSD_HISTORIQUE.
"""

import os
import re
import time

import config
from src.data_loader import DataLoader
from src.xsd_xml_to_ddl import get_available_tables

# Dossier ou les XSD sont conserves de façon permanente des qu'ils
# servent a creer des tables (necessaire pour pouvoir les reutiliser
# plus tard dans "Deposer un fichier", sans les redemander a l'utilisateur).
XSD_STORE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "xsd_ddl")
os.makedirs(XSD_STORE_DIR, exist_ok=True)


def _friendly_insert_error(exc, table_name):
    """
    Traduit une erreur Oracle courante en message clair pour le Journal
    des depots, au lieu du texte brut ORA-xxxxx.

    ORA-01400 (NOT NULL violation) peut avoir PLUSIEURS causes dans cette
    appli -- on ne peut pas savoir laquelle sans revoir la configuration
    de la table, donc le message liste les causes possibles plutot que
    d'en affirmer une seule a tort :
      1. Le XSD declare ce champ obligatoire (minOccurs > 0) mais il est
         absent/vide dans ce XML precis.
      2. Ce champ a ete choisi comme cle (primaire ou composite) sur la
         page de selection du Generateur DDL -- une cle ne peut jamais
         etre vide, meme si le XSD la marque optionnelle.
      3. Ce champ sert de colonne de liaison (FK) vers la table parente
         -- toujours NOT NULL, meme raison que ci-dessus.
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
    loader = DataLoader(
        username=config.DB_USERNAME,
        password=config.DB_PASSWORD,
        dsn=config.DB_DSN,
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

    # File d'attente : associe un fichier XML depose (nom de fichier dans
    # data/xml/a_traiter) au schema choisi manuellement au moment du depot,
    # pour que le scheduler sache quelles tables utiliser quand il traite
    # le lot -- plus de detection automatique du type de document.
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

    # Correspondance root_name (table racine d'un schema XSD) <->
    # CODE_TYPE_DOSSIER / ACTIVITE / periode de dates dans LIASSE.DOSSIER,
    # configurable librement depuis le dashboard -- aucune limite au
    # nombre de schemas, remplace l'ancien dict SCHEMA_TYPES code en dur.
    # derniere_synchro : horodatage du dernier MERGE reussi pour ce schema
    # (affiche sous chaque carte du dashboard).
    loader.cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name = 'SCHEMA_TYPE_CONFIG'"
    )
    if loader.cursor.fetchone()[0] == 0:
        loader.cursor.execute("""
            CREATE TABLE SCHEMA_TYPE_CONFIG (
                root_name              VARCHAR2(255) PRIMARY KEY,
                code_type_dossier      VARCHAR2(50) NOT NULL,
                colonne_numero_demande VARCHAR2(128) NOT NULL,
                activite                VARCHAR2(100),
                date_debut              DATE,
                date_fin                 DATE,
                derniere_synchro        TIMESTAMP
            )
        """)

    loader.connection.commit()


def store_xsd_permanently(xsd_file_storage, root_name):
    """
    Sauvegarde une copie durable du XSD depose (le fichier temporaire de
    l'analyse est, lui, toujours supprime comme avant). Necessaire pour
    pouvoir reparser ce meme XSD plus tard, quand l'utilisateur choisira
    ce schema dans "Deposer un fichier" sans le redeposer.
    """
    import uuid
    safe_name = f"{uuid.uuid4().hex}_{xsd_file_storage.filename}"
    dest = os.path.join(XSD_STORE_DIR, safe_name)
    xsd_file_storage.save(dest)
    return dest


def create_tables_and_record(loader, xsd_filename, xsd_stored_path, root_name,
                              selected_tables_with_config):
    """
    Execute le CREATE TABLE de chaque table selectionnee (le texte DDL est
    calcule exactement comme avant, via AvailableTable.to_ddl() -- aucune
    modification de la logique de generation), puis enregistre l'historique.

    selected_tables_with_config : liste de tuples
        (ordre, table_obj, ddl_sql, fk_column_name, own_pk_columns, parent_name)
    """
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
    """
    Associe un fichier (deja copie dans data/xml/a_traiter) au schema
    choisi manuellement lors du depot, pour que le scheduler sache quelles
    tables utiliser quand il traitera ce fichier plus tard.
    """
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
    """Retourne l'id_historique associe a ce fichier, ou None si aucun."""
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
    """Retire l'entree de la file d'attente une fois le fichier traite."""
    loader = connect()
    try:
        loader.cursor.execute(
            "DELETE FROM DDL_XSD_PENDING_FILES WHERE filename = :1", [filename]
        )
        loader.connection.commit()
    finally:
        loader.disconnect()


def list_historique():
    """Pour peupler la liste deroulante des XSD deja utilises dans 'Deposer un fichier'."""
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
        loader.disconnect()


def insert_xml_into_tables(id_historique, xml_path):
    """
    Reparse le XML depose avec le XSD deja stocke pour cet historique
    (meme fonction get_available_tables que le generateur DDL -- donc
    memes tables/colonnes), verifie que les tables existent toujours en
    base, puis insere les lignes dans l'ordre parent -> enfants.

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

        # Verification : toutes les tables doivent encore exister en base
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

        # Meme fonction d'extraction que le generateur DDL -- structure et
        # valeurs des tables recalculees exactement de la meme facon.
        tables = get_available_tables(xsd_stored_path, xml_path, root_name)
        tables_by_name = {t.sql_name: t for t in tables}

        id_map = {}  # table_name -> {local_row_id: valeur reelle inseree en base}
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
                        # Cle naturelle : la valeur vient directement de la
                        # ligne du PARENT (donnee reelle du XML), pas de la base.
                        parent_row = (
                            parent_table_obj.rows[parent_local_id - 1]
                            if (parent_table_obj and parent_local_id) else {}
                        )
                        cols.append(fk_col)
                        vals.append(parent_row.get(fk_col))
                    else:
                        # ID genere par Oracle : il faut l'id reel du parent,
                        # deja insere juste avant (id_map).
                        real_parent_id = id_map.get(parent_name, {}).get(parent_local_id)
                        cols.append(f"ID_{parent_name}")
                        vals.append(real_parent_id)

                for c in t.column_types:
                    cols.append(c)
                    vals.append(row.get(c))

                placeholders = [f":{i + 1}" for i in range(len(cols))]

                try:
                    if use_natural_pk:
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
# Synchronisation des schemas avec LIASSE.DOSSIER (demande tutrice)
# ---------------------------------------------------------------

def get_distinct_code_types_dossier():
    """
    Valeurs distinctes de CODE_TYPE_DOSSIER dans LIASSE.DOSSIER, pour
    peupler la liste deroulante du formulaire de correspondance --
    l'utilisateur choisit parmi les vraies valeurs presentes en base,
    plus de saisie libre sujette a erreur de frappe.
    """
    loader = connect()
    try:
        loader.cursor.execute(
            "SELECT DISTINCT CODE_TYPE_DOSSIER FROM LIASSE.DOSSIER "
            "WHERE CODE_TYPE_DOSSIER IS NOT NULL ORDER BY CODE_TYPE_DOSSIER"
        )
        return [r[0] for r in loader.cursor.fetchall()]
    finally:
        loader.disconnect()


def get_distinct_activites_dossier():
    """
    Valeurs distinctes de ACTIVITE dans LIASSE.DOSSIER, pour peupler la
    liste deroulante des pastilles "Activite" du dashboard.
    """
    loader = connect()
    try:
        loader.cursor.execute(
            "SELECT DISTINCT ACTIVITE FROM LIASSE.DOSSIER "
            "WHERE ACTIVITE IS NOT NULL ORDER BY ACTIVITE"
        )
        return [r[0] for r in loader.cursor.fetchall()]
    finally:
        loader.disconnect()


def get_schema_types():
    """
    Retourne toutes les correspondances configurees :
    root_name -> {code_type_dossier, colonne_numero_demande,
                  activites (liste), activite_raw (chaine brute),
                  date_debut, date_fin, derniere_synchro}.
    Lu depuis SCHEMA_TYPE_CONFIG, alimente librement depuis le dashboard --
    aucune limite au nombre de schemas.
    """
    loader = connect()
    try:
        ensure_meta_tables(loader)
        loader.cursor.execute("""
            SELECT root_name, code_type_dossier, colonne_numero_demande,
                   activite, date_debut, date_fin, derniere_synchro
            FROM SCHEMA_TYPE_CONFIG
            ORDER BY root_name
        """)
        result = {}
        for r in loader.cursor.fetchall():
            result[r[0]] = {
                "code_type_dossier": r[1],
                "colonne_numero_demande": r[2],
                "activites": [a.strip() for a in r[3].split(",")] if r[3] else [],
                "activite_raw": r[3] or "",
                "date_debut": r[4],
                "date_fin": r[5],
                "derniere_synchro": r[6],
            }
        return result
    finally:
        loader.disconnect()


def update_schema_type(root_name, code_type_dossier, colonne_numero_demande,
                        activite_raw, date_debut, date_fin):
    """
    Cree ou met a jour une correspondance. activite_raw : chaine brute
    telle que construite par le JS du formulaire, ex: "6.1,6.3" (plusieurs
    valeurs possibles, separees par des virgules). date_debut/date_fin au
    format 'YYYY-MM-DD' (venant d'un <input type="date">), ou None/'' pour
    ne pas synchroniser ce schema pour l'instant.

    Utilise des binds NOMMES (:root_name, :code_type_dossier, ...) plutot
    que positionnels (:1, :2, ...) : chaque nom apparait plusieurs fois
    dans le MERGE (USING, WHEN MATCHED, WHEN NOT MATCHED), et avec des
    binds positionnels oracledb compte CHAQUE occurrence comme une valeur
    distincte a fournir (d'ou l'erreur DPY-4009 "16 positional bind values
    are required but 6 were provided"). Avec des binds nommes, oracledb
    reutilise automatiquement la meme valeur pour chaque occurrence du
    meme nom.
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
                colonne_numero_demande = :colonne_numero_demande,
                activite = :activite_raw,
                date_debut = CASE WHEN :date_debut IS NOT NULL THEN TO_DATE(:date_debut, 'YYYY-MM-DD') ELSE NULL END,
                date_fin = CASE WHEN :date_fin IS NOT NULL THEN TO_DATE(:date_fin, 'YYYY-MM-DD') ELSE NULL END
            WHEN NOT MATCHED THEN INSERT
                (root_name, code_type_dossier, colonne_numero_demande, activite, date_debut, date_fin)
            VALUES
                (:root_name, :code_type_dossier, :colonne_numero_demande, :activite_raw,
                 CASE WHEN :date_debut IS NOT NULL THEN TO_DATE(:date_debut, 'YYYY-MM-DD') ELSE NULL END,
                 CASE WHEN :date_fin IS NOT NULL THEN TO_DATE(:date_fin, 'YYYY-MM-DD') ELSE NULL END)
            """,
            root_name=root_name,
            code_type_dossier=code_type_dossier,
            colonne_numero_demande=colonne_numero_demande,
            activite_raw=activite_raw or None,
            date_debut=date_debut or None,
            date_fin=date_fin or None,
        )
        loader.connection.commit()
    finally:
        loader.disconnect()


def delete_schema_type(root_name):
    """Supprime une correspondance (n'affecte pas la table racine elle-meme)."""
    loader = connect()
    try:
        loader.cursor.execute(
            "DELETE FROM SCHEMA_TYPE_CONFIG WHERE root_name = :1", [root_name]
        )
        loader.connection.commit()
    finally:
        loader.disconnect()


def get_queued_root_name(filename):
    """Retourne le root_name (table racine) associe au schema en attente pour ce fichier, ou None."""
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


def sync_dossiers_liasse_pour_type(root_name):
    """
    MERGE table_racine (orcl_local) <- LIASSE.DOSSIER, pour un seul schema,
    en utilisant la configuration complete (CODE_TYPE_DOSSIER, ACTIVITE,
    dates) stockee dans SCHEMA_TYPE_CONFIG pour ce root_name.
    Ne fait rien si la config est absente, ou si les dates/activites ne
    sont pas renseignees (schema pas encore configure pour la synchro).

    TRACE_EXECUTION a NUMERO_DOSSIER en cle primaire (un seul etat par
    dossier, pas un historique cumulatif) : on y fait donc un MERGE, pas
    un INSERT, sinon un dossier deja journalise lors d'un run precedent
    fait echouer le run suivant (ORA-00001, violation de contrainte
    unique). Chaque nouveau passage met a jour la ligne existante du
    dossier plutot que d'en creer une seconde.

    Met a jour derniere_synchro sur ce root_name apres un MERGE reussi.
    """
    cfg = get_schema_types().get(root_name)
    if cfg is None or not cfg["date_debut"] or not cfg["date_fin"]:
        return
    if not cfg["activites"]:
        return

    loader = connect()
    try:
        loader.cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :1", [root_name]
        )
        if loader.cursor.fetchone()[0] == 0:
            return  # table pas encore creee via le generateur DDL

        activite_binds = {f"activite{i}": val for i, val in enumerate(cfg["activites"])}
        activite_placeholders = ", ".join(f":{k}" for k in activite_binds)

        date_debut_str = cfg["date_debut"].strftime("%Y-%m-%d")
        date_fin_str = cfg["date_fin"].strftime("%Y-%m-%d")

        # Recupere d'abord la liste des dossiers concernes, pour pouvoir
        # journaliser chacun individuellement dans TRACE_EXECUTION.
        loader.cursor.execute(
            f"""
            SELECT NUMERO_DOSSIER, NUMERO_DEMANDE
            FROM LIASSE.DOSSIER
            WHERE CODE_TYPE_DOSSIER = :code_type
              AND ACTIVITE IN ({activite_placeholders})
              AND DATE_CREATION BETWEEN TO_DATE(:date_debut, 'YYYY-MM-DD') AND TO_DATE(:date_fin, 'YYYY-MM-DD')
            """,
            code_type=cfg["code_type_dossier"],
            date_debut=date_debut_str, date_fin=date_fin_str,
            **activite_binds,
        )
        dossiers = loader.cursor.fetchall()  # [(numero_dossier, numero_demande), ...]

        t_debut = time.time()
        try:
            sql = f"""
                MERGE INTO {root_name} t
                USING (
                    SELECT NUMERO_DOSSIER, NUMERO_DEMANDE
                    FROM LIASSE.DOSSIER
                    WHERE CODE_TYPE_DOSSIER = :code_type
                      AND ACTIVITE IN ({activite_placeholders})
                      AND DATE_CREATION BETWEEN TO_DATE(:date_debut, 'YYYY-MM-DD') AND TO_DATE(:date_fin, 'YYYY-MM-DD')
                ) d
                ON (t.NUMERO_DOSSIER = d.NUMERO_DOSSIER)
                WHEN MATCHED THEN
                    UPDATE SET t.{cfg['colonne_numero_demande']} = d.NUMERO_DEMANDE
                WHEN NOT MATCHED THEN
                    INSERT (NUMERO_DOSSIER, {cfg['colonne_numero_demande']})
                    VALUES (d.NUMERO_DOSSIER, d.NUMERO_DEMANDE)
            """
            binds = {
                "code_type": cfg["code_type_dossier"],
                "date_debut": date_debut_str,
                "date_fin": date_fin_str,
                **activite_binds,
            }
            loader.cursor.execute(sql, binds)
            duree = round(time.time() - t_debut, 2)

            for numero_dossier, _ in dossiers:
                loader.cursor.execute(
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
                    lignes_chargees=1,
                    statut="OK",
                    erreur=None,
                )

            loader.cursor.execute(
                "UPDATE SCHEMA_TYPE_CONFIG SET derniere_synchro = SYSTIMESTAMP WHERE root_name = :1",
                [root_name]
            )
            loader.connection.commit()

        except Exception as exc:
            loader.connection.rollback()
            duree = round(time.time() - t_debut, 2)
            for numero_dossier, _ in dossiers:
                loader.cursor.execute(
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
            loader.connection.commit()
            raise
    finally:
        loader.disconnect()