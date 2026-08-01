# table_generator.py
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
        col_definitions = [f"    {col['name']} {col['sql_type']}" for col in columns]
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
        """
        Ajoute un COMMENT ON COLUMN pour chaque colonne dont le nom a été
        tronqué (full_name différent du nom réel de la colonne), avec le
        chemin XML complet d'origine. Non destructif : ne modifie ni la
        structure de la table, ni son contenu, ni le nom de la colonne
        elle-même -- purement informatif, consultable via :
            SELECT column_name, comments FROM user_col_comments
            WHERE table_name = 'DOCUMENT';
        Peut être appelé sans risque même sur une table déjà existante.
        """
        table_name = table["table_name"]
        added = 0

        for col in table["columns"]:
            xml_path = col.get("xml_path")
            full_name = col.get("full_name")

            # Aucune des deux infos disponible -> rien à documenter pour cette colonne
            if not xml_path and not full_name:
                continue

            if xml_path:
                display_name = " > ".join(xml_path)
            else:
                display_name = full_name

            # Inutile de commenter si le nom n'a pas été tronqué du tout
            if display_name.lower().replace("_", "") == col["name"].lower().replace("_", ""):
                continue

            comment = display_name.replace("'", "''")  # échappe les apostrophes SQL
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

    def _sort_tables_by_depth(self, tables):
        """
        Trie les tables par profondeur croissante dans la hiérarchie
        parent -> enfant (racine = 0, enfant direct = 1, petit-enfant = 2...),
        pour garantir qu'une table n'est JAMAIS créée avant sa table parente
        (indispensable pour que la contrainte FK "REFERENCES parent(...)"
        trouve bien le parent déjà existant).

        Avant ce correctif, l'ordre dépendait de l'ordre d'apparition dans
        la liste 'tables', qui pour xsd_parser_tce.py place les tables
        petites-filles AVANT leur table parente directe (elles sont
        ajoutées pendant la construction récursive du parent, donc avant
        que celui-ci soit lui-même ajouté) -> ORA-00942.
        """
        tables_index = {t["table_name"]: t for t in tables}
        cache = {}

        def depth(table_name, visited=None):
            if visited is None:
                visited = set()
            if table_name in cache:
                return cache[table_name]
            if table_name in visited:
                # Sécurité anti-boucle infinie en cas de cycle inattendu
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

    # -----------------------------------------------------------------
    # Métadonnées : quel XSD/schéma a produit quelle table (pour le
    # filtre par XSD dans /tables-oracle) + config des noms personnalisés
    # -----------------------------------------------------------------
    def ensure_metadata_tables(self):
        """Crée les tables techniques de métadonnées si absentes.
        Non destructif, appelable à chaque démarrage sans risque."""
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
                    schema_key       VARCHAR2(50) PRIMARY KEY,
                    custom_root_name VARCHAR2(30) NOT NULL,
                    xsd_filename     VARCHAR2(255),
                    updated_at       TIMESTAMP DEFAULT SYSTIMESTAMP
                )
            """)
            self.connection.commit()
            print("   Table technique ETL_SCHEMA_CONFIG créée")

    def register_table_schema(self, table_name, schema_key, xsd_filename):
        """Enregistre/rafraîchit la correspondance table -> schéma d'origine.
        MERGE pour rester idempotent (rejoué à chaque batch sans erreur).

        IMPORTANT : binds NOMMÉS obligatoires ici, pas positionnels (:1,:2,:3).
        Oracle traite le USING(...) d'un MERGE comme une sous-requête à part,
        donc un :1 réutilisé entre USING et VALUES est compté comme 2
        occurrences distinctes par python-oracledb -> DPY-4009 ("4 requises
        mais 3 fournies"). Les binds nommés n'ont pas ce problème : chaque
        occurrence du même nom est bien reconnue comme la même variable.
        """
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
        """Retourne le CREATE TABLE réel tel qu'Oracle le voit aujourd'hui
        (via DBMS_METADATA), donc toujours fidèle même si la table a été
        modifiée après sa création initiale."""
        try:
            self.cursor.execute(
                "SELECT DBMS_METADATA.GET_DDL('TABLE', :1) FROM dual",
                [table_name.upper()]
            )
            row = self.cursor.fetchone()
            if row is None:
                return None
            ddl = row[0]
            # LOB Oracle -> texte
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

        for table in tables_ordonnees:
            result = self.create_table(table, drop_if_exists=drop_if_exists)
            if result is True:
                created += 1
            elif result is False:
                if self.table_exists(table["table_name"]):
                    skipped += 1
                else:
                    errors += 1

            # Ajoute/rafraîchit les commentaires de colonnes dans tous les
            # cas (table neuve ou déjà existante) -- opération peu coûteuse
            # et sans risque, purement documentaire.
            self.add_column_comments(table)

            # Enregistre la correspondance table -> schéma, que la table
            # soit neuve ou déjà existante (idempotent).
            if schema_key:
                self.register_table_schema(table["table_name"], schema_key, xsd_filename)

        print(f"\nRésumé :")
        print(f"    {created} tables créées")
        print(f"     {skipped} tables ignorées (déjà existantes)")
        print(f"   {errors} erreurs")

        return created, skipped, errors


if __name__ == "__main__":
    import sys
    sys.path.append("src")
    from xsd_parser import XSDParser
    import config

    if len(sys.argv) < 2:
        print("Usage: python src/table_generator.py <chemin_vers_xsd>")
        sys.exit(1)

    parser = XSDParser(sys.argv[1])
    tables, tag_map = parser.parse()

    generator = TableGenerator(
        username=config.DB_USERNAME, password=config.DB_PASSWORD, dsn=config.DB_DSN
    )

    try:
        generator.connect()
        generator.create_all_tables(tables, drop_if_exists=True)
    finally:
        generator.disconnect()