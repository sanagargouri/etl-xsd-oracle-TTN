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
        """
        Génère le CREATE TABLE.

        Deux cas :
        - Clé simple : la colonne PK porte déjà "PRIMARY KEY" dans son
          sql_type (inline), comme avant.
        - Clé composite naturelle (table["primary_key"] = [col1, col2]) :
          aucune colonne individuelle ne porte "PRIMARY KEY" -- on ajoute
          une contrainte de table CONSTRAINT ... PRIMARY KEY (col1, col2)
          à la fin du DDL. Cas de ARTICLE (numero_dossier, numero_article),
          PIECES_JOINTE (numero_dossier, reference_base_image), etc.
        """
        table_name = table["table_name"]
        columns = table["columns"]
        col_definitions = [f"    {col['name']} {col['sql_type']}" for col in columns]

        composite_pk = table.get("primary_key")
        if composite_pk:
            pk_cols = ", ".join(composite_pk)
            constraint_name = f"PK_{table_name}"[:30]
            col_definitions.append(
                f"    CONSTRAINT {constraint_name} PRIMARY KEY ({pk_cols})"
            )

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