import oracledb


class TableGenerator:
    def __init__(self, username, password, dsn):
        self.username = username
        self.password = password
        self.dsn = dsn
        self.connection = None
        self.cursor = None

    def connect(self):
        try:
            self.connection = oracledb.connect(
                user=self.username, password=self.password, dsn=self.dsn
            )
            self.cursor = self.connection.cursor()
            print(f" Connecté à Oracle ({self.dsn})")
        except oracledb.Error as e:
            print(f" Erreur de connexion Oracle : {e}")
            raise

    def disconnect(self):
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
        print("Déconnecté d'Oracle")

    def generate_ddl(self, table):
        table_name = table["table_name"]
        columns = table["columns"]
        
        # Ignore les pseudo-colonnes FK composite (is_fk_constraint)
        col_definitions = []
        fk_constraints = []
        
        for col in columns:
            if col.get("is_fk_constraint"):
                # C'est une pseudo-colonne pour stocker la contrainte FK
                # Extrait la contrainte et l'ajoute séparément
                fk_constraints.append(f"    {col['sql_type']}")
                continue
            
            col_definitions.append(f"    {col['name']} {col['sql_type']}")

        # Ajoute la PK composite
        composite_pk = table.get("primary_key")
        if composite_pk:
            pk_cols = ", ".join(composite_pk)
            constraint_name = f"PK_{table_name}"[:30]
            col_definitions.append(
                f"    CONSTRAINT {constraint_name} PRIMARY KEY ({pk_cols})"
            )

        # Ajoute les FK composites
        col_definitions.extend(fk_constraints)

        ddl = f"CREATE TABLE {table_name} (\n" + ",\n".join(col_definitions) + "\n)"
        return ddl

    def table_exists(self, table_name):
        self.cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :name",
            name=table_name.upper()
        )
        return self.cursor.fetchone()[0] > 0

    def get_table_signature(self, table_name):
        self.cursor.execute(
            """
            SELECT column_name, data_type
            FROM user_tab_columns
            WHERE table_name = :name
            ORDER BY column_name
            """,
            name=table_name.upper()
        )
        return [(row[0], row[1]) for row in self.cursor.fetchall()]

    def add_column_comments(self, table):
        table_name = table["table_name"]
        added = 0

        for col in table["columns"]:
            xml_path = col.get("xml_path")
            full_name = col.get("full_name")

            if not xml_path and not full_name:
                continue

            if xml_path:
                display_name = " > ".join(xml_path)
            else:
                display_name = full_name

            if display_name.lower().replace("_", "") == col["name"].lower().replace("_", ""):
                continue

            comment = display_name.replace("'", "''")
            try:
                self.cursor.execute(
                    f"COMMENT ON COLUMN {table_name}.{col['name']} IS '{comment}'"
                )
                added += 1
            except oracledb.Error as e:
                print(f"    Avertissement : impossible de commenter "
                      f"{table_name}.{col['name']} : {e}")

        if added:
            self.connection.commit()
            print(f"    {added} commentaire(s) de colonne ajouté(s) sur {table_name}")

    def create_table(self, table, drop_if_exists=False):
        table_name = table["table_name"]
        if self.table_exists(table_name):
            if drop_if_exists:
                print(f"    Table {table_name} existe déjà → suppression...")
                self.cursor.execute(f"DROP TABLE {table_name} CASCADE CONSTRAINTS")
                self.connection.commit()
            else:
                print(f"    Table {table_name} existe déjà → ignorée")
                return False

        ddl = self.generate_ddl(table)
        print(f"   DDL généré pour {table_name} :")
        print(f"     {ddl[:100]}...")

        try:
            self.cursor.execute(ddl)
            self.connection.commit()
            print(f"   Table {table_name} créée avec succès")
            return True
        except oracledb.Error as e:
            print(f"   Erreur lors de la création de {table_name} : {e}")
            print(f"     DDL complet :\n{ddl}")
            return False

    # -----------------------------------------------------------------
    # Migration automatique des clés "legacy" (NOUVEAU)
    # -----------------------------------------------------------------
    def _expected_key_columns(self, table):
        """
        Retourne la liste des noms de colonnes qui DOIVENT être la clé
        primaire de cette table, d'après la définition actuelle du
        parseur (table["primary_key"] pour une clé composite, ou la
        colonne portant "PRIMARY KEY" inline pour une clé simple).
        """
        composite_pk = table.get("primary_key")
        if composite_pk:
            return list(composite_pk)

        for col in table["columns"]:
            if "PRIMARY KEY" in col.get("sql_type", ""):
                return [col["name"]]

        return []

    def _actual_pk_info(self, table_name):
        """
        Retourne (constraint_name, [colonnes]) de la contrainte PRIMARY KEY
        RÉELLEMENT présente en base pour cette table, ou (None, []) si
        aucune.
        """
        self.cursor.execute(
            """
            SELECT constraint_name FROM user_constraints
            WHERE table_name = :t AND constraint_type = 'P'
            """,
            t=table_name.upper()
        )
        row = self.cursor.fetchone()
        if row is None:
            return None, []

        constraint_name = row[0]
        self.cursor.execute(
            """
            SELECT column_name FROM user_cons_columns
            WHERE table_name = :t AND constraint_name = :c
            ORDER BY position
            """,
            t=table_name.upper(), c=constraint_name
        )
        cols = [r[0].lower() for r in self.cursor.fetchall()]
        return constraint_name, cols

    def _base_sql_type(self, sql_type):
        """
        Retire les suffixes 'PRIMARY KEY' / 'REFERENCES ...(...)' d'un
        sql_type pour obtenir le type Oracle brut, utilisable dans un
        ALTER TABLE ADD (qui n'accepte pas ces clauses de la même façon
        qu'un CREATE TABLE).
        """
        base = sql_type
        if "REFERENCES" in base:
            base = base.split("REFERENCES")[0].strip()
        base = base.replace("PRIMARY KEY", "").strip()
        return base

    def _legacy_column_candidate(self, col):
        """
        Reconstruit le nom de colonne qu'aurait généré l'ANCIEN code
        (avant aplatissement de la racine / clés naturelles), à partir
        du xml_path actuel. Ex : xml_path=['REFERENCE_TTN','NUMERO_DOSSIER']
        -> 'reference_ttn_numero_dossier'. Retourne None si xml_path est
        absent (rien à deviner).
        """
        xml_path = col.get("xml_path")
        if not xml_path:
            return None
        return "_".join(seg.lower() for seg in xml_path)

    def migrate_legacy_key(self, table):
        """
        Détecte si la table existante en base a une clé primaire
        différente de celle attendue par le parseur actuel (typiquement :
        une table renommée AVANT le passage aux clés naturelles, qui a
        gardé son ancien ID généré comme PK et ses anciennes colonnes
        aplaties). Si c'est le cas, migre automatiquement :
          1. Ajoute la/les colonne(s) clé natives manquantes.
          2. Les repeuple depuis l'ancienne colonne équivalente si elle
             est identifiable (via le xml_path).
          3. Retire l'ancienne contrainte PRIMARY KEY.
          4. Pose la nouvelle contrainte PRIMARY KEY sur la clé naturelle.

        Ne fait rien si la table n'existe pas encore, ou si sa PK
        correspond déjà à ce qui est attendu -- conçu pour être appelé à
        chaque exécution (idempotent), y compris après un renommage de
        schéma, pour que l'expérience reste fiable à chaque fois.
        """
        table_name = table["table_name"]
        expected_key_cols = self._expected_key_columns(table)
        if not expected_key_cols:
            return  # table sans clé naturelle définie (exception ID généré) -- rien à migrer

        if not self.table_exists(table_name):
            return

        actual_constraint_name, actual_pk_cols = self._actual_pk_info(table_name)

        if set(actual_pk_cols) == set(expected_key_cols):
            return  # déjà cohérent, rien à faire

        print(f"\n Dérive de clé détectée sur {table_name} : "
              f"PK actuelle = {actual_pk_cols or '(aucune)'}, "
              f"attendue = {expected_key_cols} -- migration automatique...")

        # Colonnes existantes réellement en base
        self.cursor.execute(
            "SELECT column_name FROM user_tab_columns WHERE table_name = :t",
            t=table_name.upper()
        )
        existing_columns = {row[0].lower() for row in self.cursor.fetchall()}

        columns_by_name = {c["name"]: c for c in table["columns"]}
        any_column_added = False
        backfill_failed = False

        for key_col_name in expected_key_cols:
            if key_col_name in existing_columns:
                continue  # colonne déjà là, seule la contrainte doit changer

            col_def = columns_by_name.get(key_col_name)
            base_type = self._base_sql_type(col_def["sql_type"]) if col_def else "VARCHAR2(105)"

            try:
                self.cursor.execute(
                    f"ALTER TABLE {table_name} ADD {key_col_name} {base_type}"
                )
                any_column_added = True
                print(f"    Colonne {key_col_name} ajoutée à {table_name}")
            except oracledb.Error as e:
                print(f"    Impossible d'ajouter {key_col_name} à {table_name} : {e}")
                backfill_failed = True
                continue

            legacy_candidate = self._legacy_column_candidate(col_def) if col_def else None
            if legacy_candidate and legacy_candidate in existing_columns:
                try:
                    self.cursor.execute(
                        f"UPDATE {table_name} SET {key_col_name} = {legacy_candidate} "
                        f"WHERE {key_col_name} IS NULL"
                    )
                    print(f"    {table_name}.{key_col_name} repeuplée depuis "
                          f"l'ancienne colonne {legacy_candidate} "
                          f"({self.cursor.rowcount} ligne(s))")
                except oracledb.Error as e:
                    print(f"    Impossible de repeupler {key_col_name} depuis "
                          f"{legacy_candidate} : {e}")
                    backfill_failed = True
            else:
                print(f"    Aucune ancienne colonne correspondante trouvée pour "
                      f"{key_col_name} -- restera vide pour les lignes existantes")
                backfill_failed = True

        self.connection.commit()

        if backfill_failed:
            print(f"    Migration partielle de {table_name} : la nouvelle clé "
                  f"contient des valeurs manquantes -- contrainte PRIMARY KEY "
                  f"non posée pour éviter un échec. À corriger manuellement si besoin.")
            return

        # Retire l'ancienne contrainte PK (si elle existe et diffère de la nouvelle)
        if actual_constraint_name:
            try:
                self.cursor.execute(
                    f"ALTER TABLE {table_name} DROP CONSTRAINT {actual_constraint_name}"
                )
                print(f"    Ancienne contrainte PK ({actual_constraint_name}) retirée de {table_name}")
            except oracledb.Error as e:
                print(f"    Impossible de retirer l'ancienne PK de {table_name} : {e}")
                self.connection.commit()
                return

        # Pose la nouvelle contrainte PK sur la clé naturelle attendue
        try:
            pk_cols_sql = ", ".join(expected_key_cols)
            for col_name in expected_key_cols:
                self.cursor.execute(
                    f"ALTER TABLE {table_name} MODIFY ({col_name} NOT NULL)"
                )
            constraint_name = f"PK_{table_name}"[:30]
            self.cursor.execute(
                f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} "
                f"PRIMARY KEY ({pk_cols_sql})"
            )
            self.connection.commit()
            print(f"    Nouvelle PRIMARY KEY ({pk_cols_sql}) posée sur {table_name} "
                  f"-- migration terminée")
        except oracledb.Error as e:
            print(f"    Impossible de poser la nouvelle PRIMARY KEY sur "
                  f"{table_name} : {e}")
            self.connection.commit()

    def sync_missing_columns(self, table):
        table_name = table["table_name"]
        composite_pk_cols = set(table.get("primary_key") or [])

        self.cursor.execute(
            "SELECT column_name FROM user_tab_columns WHERE table_name = :name",
            name=table_name.upper()
        )
        existing_columns = {row[0] for row in self.cursor.fetchall()}

        added = []
        for col in table["columns"]:
            if col["name"].upper() in existing_columns:
                continue

            sql_type = col.get("sql_type", "")
            if ("GENERATED ALWAYS AS IDENTITY" in sql_type
                    or "REFERENCES" in sql_type
                    or col["name"] in composite_pk_cols):
                continue

            try:
                self.cursor.execute(
                    f"ALTER TABLE {table_name} ADD {col['name']} {sql_type}"
                )
                added.append(col["name"])
            except oracledb.Error as e:
                print(f"    Impossible d'ajouter la colonne {col['name']} "
                      f"à {table_name} : {e}")

        if added:
            self.connection.commit()
            print(f"    {len(added)} nouvelle(s) colonne(s) ajoutée(s) à "
                  f"{table_name} : {added}")

        return added

    def _sort_tables_by_depth(self, tables):
        tables_index = {t["table_name"]: t for t in tables}
        cache = {}

        def depth(table_name, visited=None):
            if visited is None:
                visited = set()
            if table_name in cache:
                return cache[table_name]
            if table_name in visited:
                return 0
            visited.add(table_name)

            table = tables_index.get(table_name)
            parent_name = table.get("parent_table") if table else None
            if not parent_name or parent_name not in tables_index:
                d = 0
            else:
                d = 1 + depth(parent_name, visited)

            cache[table_name] = d
            return d

        return sorted(tables, key=lambda t: depth(t["table_name"]))

    def ensure_metadata_tables(self):
        if not self.table_exists("ETL_SCHEMA_TABLES"):
            self.cursor.execute("""
                CREATE TABLE ETL_SCHEMA_TABLES (
                    table_name    VARCHAR2(30) PRIMARY KEY,
                    schema_key    VARCHAR2(50) NOT NULL,
                    xsd_filename  VARCHAR2(255),
                    date_creation TIMESTAMP DEFAULT SYSTIMESTAMP
                )
            """)
            self.connection.commit()
            print("   Table technique ETL_SCHEMA_TABLES créée")

        if not self.table_exists("ETL_SCHEMA_CONFIG"):
            self.cursor.execute("""
                CREATE TABLE ETL_SCHEMA_CONFIG (
                    schema_key         VARCHAR2(50) PRIMARY KEY,
                    custom_root_name   VARCHAR2(30) NOT NULL,
                    xsd_filename       VARCHAR2(255),
                    xsd_structure_hash VARCHAR2(64),
                    updated_at         TIMESTAMP DEFAULT SYSTIMESTAMP
                )
            """)
            self.connection.commit()
            print("   Table technique ETL_SCHEMA_CONFIG créée")
        else:
            self.cursor.execute("""
                SELECT COUNT(*) FROM user_tab_columns
                WHERE table_name = 'ETL_SCHEMA_CONFIG'
                  AND column_name = 'XSD_STRUCTURE_HASH'
            """)
            if self.cursor.fetchone()[0] == 0:
                self.cursor.execute(
                    "ALTER TABLE ETL_SCHEMA_CONFIG ADD xsd_structure_hash VARCHAR2(64)"
                )
                self.connection.commit()
                print("   Colonne xsd_structure_hash ajoutée à ETL_SCHEMA_CONFIG")

        if not self.table_exists("ETL_SCHEMA_SUGGESTIONS"):
            self.cursor.execute("""
                CREATE TABLE ETL_SCHEMA_SUGGESTIONS (
                    id                   NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    source_file          VARCHAR2(255) NOT NULL,
                    detected_root_tag    VARCHAR2(255),
                    xml_sample           CLOB,
                    suggested_schema_key VARCHAR2(50),
                    confidence_score     NUMBER,
                    justification        VARCHAR2(4000),
                    proposed_structure   CLOB,
                    status               VARCHAR2(20) DEFAULT 'pending',
                    created_at           TIMESTAMP DEFAULT SYSTIMESTAMP,
                    validated_by         VARCHAR2(100),
                    validated_at         TIMESTAMP
                )
            """)
            self.connection.commit()
            print("   Table technique ETL_SCHEMA_SUGGESTIONS créée")
        else:
            self.cursor.execute("""
                SELECT COUNT(*) FROM user_tab_columns
                WHERE table_name = 'ETL_SCHEMA_SUGGESTIONS'
                  AND column_name = 'PROPOSED_STRUCTURE'
            """)
            if self.cursor.fetchone()[0] == 0:
                self.cursor.execute(
                    "ALTER TABLE ETL_SCHEMA_SUGGESTIONS ADD proposed_structure CLOB"
                )
                self.connection.commit()
                print("   Colonne proposed_structure ajoutée à ETL_SCHEMA_SUGGESTIONS")

            self.cursor.execute("""
                SELECT COUNT(*) FROM user_tab_columns
                WHERE table_name = 'ETL_SCHEMA_SUGGESTIONS'
                  AND column_name = 'DETECTED_ROOT_TAG'
            """)
            if self.cursor.fetchone()[0] == 0:
                self.cursor.execute(
                    "ALTER TABLE ETL_SCHEMA_SUGGESTIONS ADD detected_root_tag VARCHAR2(255)"
                )
                self.connection.commit()
                print("   Colonne detected_root_tag ajoutée à ETL_SCHEMA_SUGGESTIONS")

        if not self.table_exists("ETL_ROOT_ALIASES"):
            self.cursor.execute("""
                CREATE TABLE ETL_ROOT_ALIASES (
                    root_tag             VARCHAR2(255) PRIMARY KEY,
                    schema_key           VARCHAR2(50) NOT NULL,
                    source_suggestion_id NUMBER,
                    created_at           TIMESTAMP DEFAULT SYSTIMESTAMP
                )
            """)
            self.connection.commit()
            print("   Table technique ETL_ROOT_ALIASES créée")

    def register_table_schema(self, table_name, schema_key, xsd_filename):
        self.cursor.execute("""
            MERGE INTO ETL_SCHEMA_TABLES t
            USING (SELECT :table_name AS table_name FROM dual) src
            ON (t.table_name = src.table_name)
            WHEN NOT MATCHED THEN
                INSERT (table_name, schema_key, xsd_filename)
                VALUES (:table_name, :schema_key, :xsd_filename)
        """, table_name=table_name, schema_key=schema_key, xsd_filename=xsd_filename)
        self.connection.commit()

    def get_table_ddl(self, table_name):
        try:
            self.cursor.execute(
                "SELECT DBMS_METADATA.GET_DDL('TABLE', :1) FROM dual",
                [table_name.upper()]
            )
            row = self.cursor.fetchone()
            if row is None:
                return None
            ddl = row[0]
            ddl_text = ddl.read() if hasattr(ddl, "read") else str(ddl)
            return ddl_text.strip()
        except oracledb.Error as e:
            print(f"   Impossible de récupérer le DDL de {table_name} : {e}")
            return None

    def create_all_tables(self, tables, drop_if_exists=False, schema_key=None, xsd_filename=None):
        print(f"\n🏗️  Création de {len(tables)} tables dans Oracle...")

        self.ensure_metadata_tables()
        tables_ordonnees = self._sort_tables_by_depth(tables)

        created = 0
        skipped = 0
        errors = 0
        columns_synced = 0

        for table in tables_ordonnees:
            result = self.create_table(table, drop_if_exists=drop_if_exists)
            if result is True:
                created += 1
            elif result is False:
                if self.table_exists(table["table_name"]):
                    skipped += 1
                    # Corrige d'abord toute dérive de clé (table renommée
                    # avant le passage aux clés naturelles, ou toute autre
                    # incohérence entre la PK réelle et celle attendue par
                    # le parseur actuel) -- AVANT de synchroniser le reste
                    # des colonnes, pour rester cohérent à chaque exécution.
                    self.migrate_legacy_key(table)
                    added = self.sync_missing_columns(table)
                    columns_synced += len(added)
                else:
                    errors += 1

            self.add_column_comments(table)

            if schema_key:
                self.register_table_schema(table["table_name"], schema_key, xsd_filename)

        print(f"\nRésumé :")
        print(f"    {created} tables créées")
        print(f"     {skipped} tables ignorées (déjà existantes)")
        if columns_synced:
            print(f"    {columns_synced} nouvelle(s) colonne(s) synchronisée(s) au total")
        print(f"   {errors} erreurs")

        return created, skipped, errors


if __name__ == "__main__":
    import sys
    sys.path.append("src")
    from xsd_parser_tce import XSDParserTCE
    import config

    if len(sys.argv) < 2:
        print("Usage: python src/table_generator.py <chemin_vers_xsd>")
        sys.exit(1)

    parser = XSDParserTCE(sys.argv[1])
    tables, tag_map = parser.parse()

    generator = TableGenerator(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN
    )

    try:
        generator.connect()
        generator.create_all_tables(tables, drop_if_exists=True)
    finally:
        generator.disconnect()