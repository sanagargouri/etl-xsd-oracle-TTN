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
        # Rempli au fur et à mesure des insertions, réutilisé pour les FK des enfants
        self.id_mapping = {}

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

        nom_fichier      : nom du fichier XML traité
        statut           : 'OK' ou 'ERREUR'
        lignes_chargees  : nombre de lignes chargées avec succès
        message_erreur   : message d'erreur si statut == 'ERREUR', sinon None
        duree_secondes   : durée du traitement en secondes
        """
        # Tronque le message d'erreur si besoin (colonne limitée à 4000 caractères)
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
        Trouve la colonne clé primaire d'une table
        (celle générée automatiquement par Oracle).
        """
        for col in table["columns"]:
            if "GENERATED ALWAYS AS IDENTITY" in col["sql_type"]:
                return col["name"]
        return None

    def _get_fk_column(self, table):
        """
        Trouve la colonne clé étrangère d'une table (s'il y en a une).
        Retourne None si la table n'a pas de parent (ex: TEIF).
        """
        for col in table["columns"]:
            if "REFERENCES" in col["sql_type"]:
                return col["name"]
        return None

    def _table_depth(self, table_name, tables_index, cache=None):
        """
        Calcule récursivement la profondeur d'une table dans la hiérarchie.
        TEIF (racine, sans parent) = 0
        PARTDETAIL (parent = TEIF) = 1
        LOC (parent = PARTDETAIL)  = 2
        etc.

        cache : dictionnaire pour éviter de recalculer plusieurs fois
                la même profondeur (mémoïsation)
        """
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
        """
        Trie les tables par profondeur croissante (racine d'abord,
        puis niveau 1, puis niveau 2...), pour garantir qu'un parent
        est toujours chargé avant ses enfants.
        """
        tables_index = {t["table_name"]: t for t in tables}
        cache = {}
        return sorted(
            tables,
            key=lambda t: self._table_depth(t["table_name"], tables_index, cache)
        )

    def load_all(self, tables, data):
        """
        Charge toutes les tables dans Oracle, dans le bon ordre
        (parents avant enfants), en propageant les FK via id_mapping.

        tables : liste de dictionnaires de définition de table (xsd_parser.py)
        data   : dictionnaire {nom_table: [lignes]} (xml_extractor.py)
        """
        sorted_tables = self._sort_tables_by_depth(tables)

        print(f"\nOrdre de chargement : {[t['table_name'] for t in sorted_tables]}")

        total_inserted = 0
        for table in sorted_tables:
            table_name = table["table_name"]
            rows = data.get(table_name, [])
            inserted = self.load_table(table, rows)
            total_inserted += inserted

        print(f"\n Chargement terminé : {total_inserted} ligne(s) au total")
        return total_inserted

    def _coerce_value(self, value, sql_type):
        """
        Convertit une valeur texte extraite du XML vers le type Python
        adapté à la colonne Oracle cible.

        Pourquoi : si on laisse une chaîne comme '2.540' pour une colonne
        NUMBER, c'est Oracle qui doit la convertir — et cette conversion
        dépend des paramètres régionaux de la session (NLS_NUMERIC_CHARACTERS),
        ce qui peut faire échouer '.' comme séparateur décimal (ORA-01722).
        En convertissant nous-mêmes en float côté Python (indépendant de la
        locale), on évite ce problème une fois pour toutes.
        """
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
        """
        Insère toutes les lignes d'une table dans Oracle.

        table : dictionnaire de définition de table (format xsd_parser.py)
        rows  : liste de lignes extraites (format xml_extractor.py),
                chaque ligne contient '_local_id' et éventuellement '_parent_local_id'

        Remplit self.id_mapping[table_name] au fur et à mesure.
        """
        table_name = table["table_name"]
        pk_column = self._get_pk_column(table)
        fk_column = self._get_fk_column(table)
        parent_table = table.get("parent_table")

        self.id_mapping[table_name] = {}

        if not rows:
            print(f"   {table_name} : aucune ligne à charger")
            return 0

        # Colonnes "normales" à insérer : toutes sauf la PK (auto-générée)
        # et sauf la FK (gérée séparément, car sa valeur vient de id_mapping)
        normal_columns = [
            c for c in table["columns"]
            if c["name"] != pk_column and c["name"] != fk_column
        ]

        inserted = 0
        errors = 0

        for row in rows:
            local_id = row.get("_local_id")
            parent_local_id = row.get("_parent_local_id")

            # Construit la liste des colonnes et valeurs à insérer
            # (avec conversion de type pour éviter les erreurs Oracle liées aux locales)
            insert_columns = [c["name"] for c in normal_columns]
            insert_values = [
                self._coerce_value(row.get(c["name"]), c["sql_type"])
                for c in normal_columns
            ]

            # Ajoute la FK si cette table a un parent
            if fk_column and parent_table:
                parent_real_id = self.id_mapping.get(parent_table, {}).get(parent_local_id)
                if parent_real_id is None:
                    print(f"    Ligne ignorée dans {table_name} : "
                          f"parent introuvable (parent_local_id={parent_local_id})")
                    errors += 1
                    continue
                insert_columns.append(fk_column)
                insert_values.append(parent_real_id)

            # Construit les placeholders :1, :2, :3...
            placeholders = [f":{i+1}" for i in range(len(insert_columns))]

            # Variable de sortie pour récupérer le vrai ID généré par Oracle
            new_id_var = self.cursor.var(int)

            sql = (
                f"INSERT INTO {table_name} ({', '.join(insert_columns)}) "
                f"VALUES ({', '.join(placeholders)}) "
                f"RETURNING {pk_column} INTO :{len(insert_columns)+1}"
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

        self.connection.commit()
        print(f"   {table_name} : {inserted} ligne(s) chargée(s), {errors} erreur(s)")
        return inserted


# ---------------------------------------------------------------
# TEST RAPIDE - ÉTAPE 1 : charge uniquement TEIF (pas de FK)
# ---------------------------------------------------------------
if __name__ == "__main__":
    import sys
    sys.path.append("src")
    from xsd_parser import XSDParser
    from xml_extractor import XMLExtractor

    if len(sys.argv) < 3:
        print("Usage: python src/data_loader.py <xsd> <xml>")
        sys.exit(1)

    # Étape 1 : parser le XSD
    print("=== ÉTAPE 1 : Parsing du XSD ===")
    parser = XSDParser(sys.argv[1])
    tables, tag_map = parser.parse()

    # Étape 2 : extraire les données du XML
    print("\n=== ÉTAPE 2 : Extraction du XML ===")
    extractor = XMLExtractor(sys.argv[2], tables, tag_map)
    data = extractor.extract()

    # Étape 3 : charger UNIQUEMENT la table TEIF dans Oracle
    print("\n=== ÉTAPE 3 : Chargement de TEIF dans Oracle ===")
    loader = DataLoader(
        username="sana",
        password="Oracle123",
        dsn="localhost:1521/orcl2121"
    )

    teif_table = next(t for t in tables if t["table_name"] == "TEIF")
    partdetail_table = next(t for t in tables if t["table_name"] == "PARTDETAIL")

    try:
        loader.connect()
        loader.load_all(tables, data)
        print(f"\nid_mapping obtenu :")
        for table_name, mapping in loader.id_mapping.items():
            print(f"  {table_name} : {mapping}")
    finally:
        loader.disconnect()