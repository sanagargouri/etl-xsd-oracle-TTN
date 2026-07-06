# table_generator.py
import oracledb

class TableGenerator:
    def __init__(self, username, password, dsn):
        """
        Initialise la connexion Oracle.
        username : nom d'utilisateur Oracle (ex: 'sana')
        password : mot de passe
        dsn      : adresse de connexion (ex: 'localhost:1521/orcl2121')
        """
        self.username = username
        self.password = password
        self.dsn = dsn
        self.connection = None
        self.cursor = None

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

    def generate_ddl(self, table):
        """
        Génère le DDL (CREATE TABLE) pour une table donnée.
        Reçoit un dictionnaire table du format produit par xsd_parser.py
        """
        table_name = table["table_name"]
        columns = table["columns"]

        # Construit la liste des colonnes
        col_definitions = []
        for col in columns:
            col_def = f"    {col['name']} {col['sql_type']}"
            col_definitions.append(col_def)

        # Assemble le CREATE TABLE complet
        ddl = f"CREATE TABLE {table_name} (\n"
        ddl += ",\n".join(col_definitions)
        ddl += "\n)"

        return ddl

    def table_exists(self, table_name):
        """
        Vérifie si une table existe déjà dans Oracle.
        Retourne True si elle existe, False sinon.
        """
        self.cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :name",
            name=table_name.upper()
        )
        count = self.cursor.fetchone()[0]
        return count > 0

    def create_table(self, table, drop_if_exists=False):
        """
        Crée une table dans Oracle à partir du dictionnaire table.
        
        drop_if_exists : si True, supprime la table si elle existe déjà
                         avant de la recréer
        """
        table_name = table["table_name"]

        # Vérifie si la table existe déjà
        if self.table_exists(table_name):
            if drop_if_exists:
                print(f"    Table {table_name} existe déjà → suppression...")
                self.cursor.execute(f"DROP TABLE {table_name} CASCADE CONSTRAINTS")
                self.connection.commit()
            else:
                print(f"    Table {table_name} existe déjà → ignorée")
                return False

        # Génère et exécute le DDL
        ddl = self.generate_ddl(table)
        print(f"   DDL généré pour {table_name} :")
        print(f"     {ddl[:100]}...")  # affiche les 100 premiers caractères

        try:
            self.cursor.execute(ddl)
            self.connection.commit()
            print(f"   Table {table_name} créée avec succès")
            return True
        except oracledb.Error as e:
            print(f"   Erreur lors de la création de {table_name} : {e}")
            print(f"     DDL complet :\n{ddl}")
            return False

    def create_all_tables(self, tables, drop_if_exists=False):
        """
        Crée toutes les tables dans le bon ordre
        (les tables parentes avant les tables enfants).
        
        tables : liste de dictionnaires produite par xsd_parser.py
        """
        print(f"\n🏗️  Création de {len(tables)} tables dans Oracle...")

        # Trie les tables : d'abord celles sans parent (tables racines)
        # puis celles avec parent (pour respecter les FK)
        tables_sans_parent = [t for t in tables if "parent_table" not in t]
        tables_avec_parent = [t for t in tables if "parent_table" in t]

        # Ordre de création : racines d'abord, puis enfants
        tables_ordonnees = tables_sans_parent + tables_avec_parent

        created = 0
        skipped = 0
        errors = 0

        for table in tables_ordonnees:
            result = self.create_table(table, drop_if_exists=drop_if_exists)
            if result is True:
                created += 1
            elif result is False:
                # Vérifie si c'était un skip ou une erreur
                if self.table_exists(table["table_name"]):
                    skipped += 1
                else:
                    errors += 1

        print(f"\nRésumé :")
        print(f"    {created} tables créées")
        print(f"     {skipped} tables ignorées (déjà existantes)")
        print(f"   {errors} erreurs")

        return created, skipped, errors


# ---------------------------------------------------------------
# TEST RAPIDE
# ---------------------------------------------------------------
if __name__ == "__main__":
    import sys
    sys.path.append("src")
    from xsd_parser import XSDParser

    if len(sys.argv) < 2:
        print("Usage: python src/table_generator.py <chemin_vers_xsd>")
        sys.exit(1)

    # Étape 1 : parser le XSD
    print("=== ÉTAPE 1 : Parsing du XSD ===")
    parser = XSDParser(sys.argv[1])
    tables = parser.parse()

    # Étape 2 : créer les tables dans Oracle
    print("\n=== ÉTAPE 2 : Création des tables dans Oracle ===")
    generator = TableGenerator(
        username="sana",
        password="Oracle123",
        dsn="localhost:1521/orcl2121"
    )

    try:
        generator.connect()
        generator.create_all_tables(tables, drop_if_exists=True)
    finally:
        generator.disconnect()