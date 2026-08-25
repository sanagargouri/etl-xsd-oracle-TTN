# data_loader.py
import oracledb

class DataLoader:
    def __init__(self, username, password, dsn):
        """
        Initialise la connexion Oracle.
        username : nom d'utilisateur Oracle
        password : mot de passe
        dsn      : adresse de connexion (ex: 'localhost:1521/orcl2121')
        """
        self.username = username
        self.password = password
        self.dsn = dsn
        self.connection = None
        self.cursor = None

        # id_mapping : {nom_table: {local_id: vrai_id_oracle}}
        # Ne sert plus QUE pour les tables "exception" sans discriminant
        # naturel (ex: MINISTERE_COMMERCE_OBSERVATION), qui gardent un ID
        # généré par Oracle (IDENTITY) -- cf. xsd_parser_tce.py.
        self.id_mapping = {}

        # document_key_mapping : {nom_table: {local_id: numero_dossier}}
        # Remplace le rôle "clé de liaison" que jouait id_mapping avant.
        # NUMERO_DOSSIER est une vraie donnée du XML (extraite une seule
        # fois au niveau DOCUMENT), propagée ici de proche en proche vers
        # toutes les tables descendantes -- jamais générée par Oracle.
        self.document_key_mapping = {}

        # Nom de la colonne clé naturelle, doit rester synchro avec
        # XSDParserTCE.ROOT_KEY_COLUMN_NAME.
        self.NATURAL_KEY_COLUMN = "numero_dossier"

    def connect(self):
        """Ouvre la connexion à Oracle."""
        try:
            self.connection = oracledb.connect(
                user=self.username,
                password=self.password,
                dsn=self.dsn
            )
            self.cursor = self.connection.cursor()
            print(f" Connecté à Oracle ({self.dsn})")
        except oracledb.Error as e:
            print(f" Erreur de connexion Oracle : {e}")
            raise

    def disconnect(self):
        """Ferme la connexion à Oracle."""
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
        print("🔌 Déconnecté d'Oracle")

    def create_log_table(self):
        """
        Crée la table ETL_LOG si elle n'existe pas déjà.
        Une ligne = une exécution de traitement pour un fichier XML donné.
        Ne supprime jamais la table existante (contrairement aux tables de
        données) : on veut conserver l'historique d'une exécution à l'autre.
        """
        self.cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = 'ETL_LOG'"
        )
        exists = self.cursor.fetchone()[0] > 0

        if exists:
            print(" Table ETL_LOG déjà présente")
            return

        ddl = """
        CREATE TABLE ETL_LOG (
            id_log NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            nom_fichier VARCHAR2(255) NOT NULL,
            date_traitement TIMESTAMP DEFAULT SYSTIMESTAMP,
            statut VARCHAR2(20) NOT NULL,
            lignes_chargees NUMBER,
            message_erreur VARCHAR2(4000),
            duree_secondes NUMBER
        )
        """
        self.cursor.execute(ddl)
        self.connection.commit()
        print(" Table ETL_LOG créée")

    def log_result(self, nom_fichier, statut, lignes_chargees=0,
                    message_erreur=None, duree_secondes=None):
        """
        Enregistre le résultat du traitement d'un fichier dans ETL_LOG.
        """
        if message_erreur and len(message_erreur) > 4000:
            message_erreur = message_erreur[:3997] + "..."

        self.cursor.execute(
            """
            INSERT INTO ETL_LOG
                (nom_fichier, statut, lignes_chargees, message_erreur, duree_secondes)
            VALUES (:1, :2, :3, :4, :5)
            """,
            [nom_fichier, statut, lignes_chargees, message_erreur, duree_secondes]
        )
        self.connection.commit()

    def _get_pk_column(self, table):
        """
        Retourne le nom de la colonne PK générée (IDENTITY) SI la table en
        a une. Reste utile uniquement pour les tables "exception" sans
        discriminant naturel connu (ex: MINISTERE_COMMERCE_OBSERVATION),
        qui gardent un ID Oracle généré.
        """
        for col in table["columns"]:
            if "GENERATED ALWAYS AS IDENTITY" in col["sql_type"]:
                return col["name"]
        return None

    def _table_depth(self, table_name, tables_index, cache=None):
        if cache is None:
            cache = {}
        if table_name in cache:
            return cache[table_name]

        table = tables_index[table_name]
        parent_name = table.get("parent_table")

        if not parent_name:
            depth = 0
        else:
            depth = 1 + self._table_depth(parent_name, tables_index, cache)

        cache[table_name] = depth
        return depth

    def _sort_tables_by_depth(self, tables):
        tables_index = {t["table_name"]: t for t in tables}
        cache = {}
        return sorted(
            tables,
            key=lambda t: self._table_depth(t["table_name"], tables_index, cache)
        )

    def load_all(self, tables, data):
        # La colonne clé racine dépend du schéma : "numero_dossier" pour
        # TCE, "document_identifier" pour TEIF (facture_INVOIC). On la
        # déduit de la table racine plutôt que de la coder en dur, sinon
        # tous les documents d'un schéma dont la clé porte un autre nom
        # sont rejetés avec "clé racine absente".
        root_table = next((t for t in tables if not t.get("parent_table")), None)
        if root_table:
            root_pk = root_table.get("primary_key")
            if root_pk:
                self.NATURAL_KEY_COLUMN = root_pk[0]

        sorted_tables = self._sort_tables_by_depth(tables)

        print(f"\nOrdre de chargement : {[t['table_name'] for t in sorted_tables]}")

        root_name = root_table["table_name"] if root_table else None
        total_inserted = 0

        try:
            for table in sorted_tables:
                table_name = table["table_name"]
                rows = data.get(table_name, [])
                inserted = self.load_table(table, rows)

                # Si la ligne racine n'a pas pu être insérée (doublon,
                # clé absente...), le document entier est abandonné :
                # charger ses tables filles créerait des lignes
                # orphelines et des doublons dans les tables à ID généré.
                if table_name == root_name and rows and inserted == 0:
                    self.connection.rollback()
                    print(f"\n Document abandonné : la ligne racine "
                          f"({table_name}) n'a pas pu être insérée. "
                          f"Aucune ligne fille n'a été conservée.")
                    return 0

                total_inserted += inserted

            self.connection.commit()

        except Exception:
            self.connection.rollback()
            print("\n Erreur inattendue : transaction annulée, "
                  "aucune ligne de ce document n'a été conservée.")
            raise

        print(f"\n Chargement terminé : {total_inserted} ligne(s) au total")
        return total_inserted

    def _coerce_value(self, value, sql_type):
        if value is None:
            return None
        if sql_type.startswith("NUMBER"):
            try:
                return float(value)
            except (TypeError, ValueError):
                print(f"    Valeur numérique invalide ignorée : {value!r} (type {sql_type})")
                return None
        return value

    def load_table(self, table, rows):
        
        table_name = table["table_name"]
        generated_pk_column = self._get_pk_column(table)
        parent_table = table.get("parent_table")
        is_root = parent_table is None

        self.document_key_mapping.setdefault(table_name, {})
        if generated_pk_column:
            self.id_mapping.setdefault(table_name, {})

        if not rows:
            print(f"   {table_name} : aucune ligne à charger")
            return 0

        # FIX : on exclut désormais aussi les pseudo-colonnes de contrainte
        # FK composite (is_fk_constraint=True, ex: "__fk_constraint_ARTICLE").
        # Ce ne sont pas de vraies colonnes -- table_generator.py les
        # convertit en clause CONSTRAINT dans le DDL et ne les crée jamais
        # comme colonnes réelles. Avant ce correctif, elles étaient quand
        # même incluses dans l'INSERT (colonne + valeur NULL), ce qui
        # provoquait un ORA-00904 (identifiant invalide) pour toute table
        # enfant d'un parent à clé composite (ex: ARTICLE_COMMERCE_OBSERVATION,
        # ARTICLE_TECHNIQUE_OBSERVATION, ARTICLE_BANQUE_OBSERVATION).
        insertable_columns = [
            c for c in table["columns"]
            if c["name"] != generated_pk_column and not c.get("is_fk_constraint")
        ]

        inserted = 0
        errors = 0

        for row in rows:
            local_id = row.get("_local_id")
            parent_local_id = row.get("_parent_local_id")

            if is_root:
                # DOCUMENT : numero_dossier vient directement du XML.
                document_key = row.get(self.NATURAL_KEY_COLUMN)
                if document_key is None:
                    print(f"    Ligne ignorée dans {table_name} : "
                          f"NUMERO_DOSSIER absent -- impossible de charger ce document".replace(
                              "NUMERO_DOSSIER", self.NATURAL_KEY_COLUMN.upper()))
                    errors += 1
                    continue
            else:
                # Table descendante : on récupère le numero_dossier déjà
                # résolu pour le parent (quel que soit le niveau de
                # profondeur), et on l'injecte dans la ligne courante.
                document_key = self.document_key_mapping.get(parent_table, {}).get(parent_local_id)
                if document_key is None:
                    print(f"    Ligne ignorée dans {table_name} : "
                          f"parent introuvable (parent_local_id={parent_local_id})")
                    errors += 1
                    continue
                row = dict(row)  # copie défensive, ne pas modifier les données partagées
                row[self.NATURAL_KEY_COLUMN] = document_key

            # Mémorise le numero_dossier résolu pour cette ligne, pour que
            # les éventuelles tables petites-filles puissent le retrouver
            # en remontant via _parent_local_id.
            self.document_key_mapping[table_name][local_id] = document_key

            insert_columns = [c["name"] for c in insertable_columns]
            insert_values = [
                self._coerce_value(row.get(c["name"]), c["sql_type"])
                for c in insertable_columns
            ]
            placeholders = [f":{i+1}" for i in range(len(insert_columns))]

            if generated_pk_column:
                # --- Chemin "exception" : ID généré par Oracle ---
                new_id_var = self.cursor.var(int)
                sql = (
                    f"INSERT INTO {table_name} ({', '.join(insert_columns)}) "
                    f"VALUES ({', '.join(placeholders)}) "
                    f"RETURNING {generated_pk_column} INTO :{len(insert_columns)+1}"
                )
                try:
                    self.cursor.execute(sql, insert_values + [new_id_var])
                    real_id = new_id_var.getvalue()[0]
                    self.id_mapping[table_name][local_id] = real_id
                    inserted += 1
                except oracledb.Error as e:
                    print(f"    Erreur INSERT dans {table_name} (local_id={local_id}) : {e}")
                    print(f"      SQL : {sql}")
                    print(f"      Valeurs : {insert_values}")
                    errors += 1
            else:
                # --- Chemin "clé naturelle" : rien à récupérer après coup ---
                sql = (
                    f"INSERT INTO {table_name} ({', '.join(insert_columns)}) "
                    f"VALUES ({', '.join(placeholders)})"
                )
                try:
                    self.cursor.execute(sql, insert_values)
                    inserted += 1
                except oracledb.Error as e:
                    # Cas fréquent attendu : ré-import du même document
                    # (même numero_dossier / discriminant) -> violation de
                    # contrainte PK, normal si le fichier a déjà été traité.
                    print(f"    Erreur INSERT dans {table_name} (local_id={local_id}) : {e}")
                    print(f"      SQL : {sql}")
                    print(f"      Valeurs : {insert_values}")
                    errors += 1

        # PAS de commit ici : le document entier forme une seule
        # transaction, validée (ou annulée) par load_all. Sinon, quand la
        # ligne racine est refusée (doublon), les tables filles à ID
        # généré restaient quand même insérées et polluaient la base à
        # chaque relance.
        self.last_errors = errors
        print(f"   {table_name} : {inserted} ligne(s) chargée(s), {errors} erreur(s)")
        return inserted


if __name__ == "__main__":
    import sys
    sys.path.append("src")
    from xsd_parser_tce import XSDParserTCE
    from xml_extractor import XMLExtractor
    import config

    if len(sys.argv) < 3:
        print("Usage: python src/data_loader.py <xsd> <xml>")
        sys.exit(1)

    print("=== ÉTAPE 1 : Parsing du XSD ===")
    parser = XSDParserTCE(sys.argv[1])
    tables, tag_map = parser.parse()

    print("\n=== ÉTAPE 2 : Extraction du XML ===")
    extractor = XMLExtractor(sys.argv[2], tables, tag_map)
    data = extractor.extract()

    print("\n=== ÉTAPE 3 : Chargement dans Oracle ===")
    loader = DataLoader(
        username=config.DB_USERNAME,
        password=config.DB_PASSWORD,
        dsn=config.DB_DSN,
    )

    try:
        loader.connect()
        loader.load_all(tables, data)
        print(f"\ndocument_key_mapping obtenu :")
        for table_name, mapping in loader.document_key_mapping.items():
            print(f"  {table_name} : {mapping}")
    finally:
        loader.disconnect()