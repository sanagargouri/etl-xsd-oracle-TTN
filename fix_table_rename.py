#!/usr/bin/env python3
"""
Répare les clés étrangères après renommage d'une table parent (ex: DOCUMENT → TCE)
Exécute les corrections directement dans Oracle.
"""

import cx_Oracle
import os
import sys

# Charge la config Oracle
sys.path.insert(0, '/home/claude/projets/etl-xsd-oracle-TTN')
from config import DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_SERVICE

def get_connection():
    """Établit la connexion Oracle"""
    try:
        conn = cx_Oracle.connect(
            user=DB_USER,
            password=DB_PASSWORD,
            dsn=f"{DB_HOST}:{DB_PORT}/{DB_SERVICE}"
        )
        print(f"✅ Connecté à Oracle ({DB_HOST}:{DB_PORT}/{DB_SERVICE})")
        return conn
    except Exception as e:
        print(f"❌ Erreur de connexion : {e}")
        sys.exit(1)

def drop_all_tce_tables(cursor, schema='SANA'):
    """
    Supprime TOUTES les tables TCE pour les recréer proprement.
    Ordre : enfants d'abord, puis parents.
    """
    # Tables TCE à supprimer (ordre inverse de dépendance)
    tables_to_drop = [
        'MINISTERE_COMMERCE_OBSERVATION',
        'ORGANISME_OBSERVATION',
        'BANQUE_OBSERVATION',
        'ARTICLE_COMMERCE_OBSERVATION',
        'ARTICLE_TECHNIQUE_OBSERVATION',
        'ARTICLE_BANQUE_OBSERVATION',
        'PIECES_JOINTE',
        'ARTICLE',
        'TCE'  # Table parent
    ]
    
    print("\n🗑️  Suppression des tables TCE (ordre inverse de dépendance)...")
    for table in tables_to_drop:
        try:
            cursor.execute(f"DROP TABLE {schema}.{table} CASCADE CONSTRAINTS")
            print(f"  ✅ {table} supprimée")
        except cx_Oracle.DatabaseError as e:
            if 'table or view does not exist' in str(e).lower():
                print(f"  ⚠️  {table} n'existe pas (ignorée)")
            else:
                print(f"  ❌ Erreur sur {table} : {e}")
    
    print("✅ Suppression terminée\n")

def regenerate_tce_schema(cursor, schema='SANA'):
    """
    Régénère le schéma TCE CORRECTEMENT avec les bonnes FK et clés naturelles.
    """
    print("🏗️  Régénération du schéma TCE...\n")
    
    # 1. TABLE RACINE TCE
    print("  → Création table TCE (clé naturelle)...")
    cursor.execute(f"""
        CREATE TABLE {schema}.TCE (
            numero_dossier VARCHAR2(50) PRIMARY KEY,
            numero_message VARCHAR2(100),
            numero_demande VARCHAR2(100),
            -- autres colonnes du document
            created_at TIMESTAMP DEFAULT SYSDATE,
            updated_at TIMESTAMP DEFAULT SYSDATE
        )
    """)
    cursor.connection.commit()
    print("     ✅ TCE créée")
    
    # 2. TABLE ARTICLE (clé composée + FK)
    print("  → Création table ARTICLE (clé composée)...")
    cursor.execute(f"""
        CREATE TABLE {schema}.ARTICLE (
            numero_dossier VARCHAR2(50) NOT NULL,
            numero_article VARCHAR2(50) NOT NULL,
            designation VARCHAR2(500),
            numero_nomenclature VARCHAR2(50),
            unite_mesure VARCHAR2(50),
            quantite NUMBER(12,2),
            prix_prix_facture_net NUMBER(15,2),
            pays_origine_code_pays VARCHAR2(50),
            pays_origine_nom_pays VARCHAR2(100),
            pays_exportation_code_pays VARCHAR2(50),
            pays_exportation_nom_pays VARCHAR2(100),
            valeur_cft NUMBER(15,2),
            poids_net NUMBER(15,2),
            date_fabrication DATE,
            date_arrivee DATE,
            statut_code_commerce_statut VARCHAR2(50),
            statut_libelle_commerce_statut VARCHAR2(200),
            statut_code_technique_statut VARCHAR2(50),
            statut_libelle_statut VARCHAR2(200),
            statut_banque_code_statut VARCHAR2(50),
            PRIMARY KEY (numero_dossier, numero_article),
            CONSTRAINT FK_ARTICLE_TCE FOREIGN KEY (numero_dossier) 
                REFERENCES {schema}.TCE(numero_dossier) ON DELETE CASCADE
        )
    """)
    cursor.connection.commit()
    print("     ✅ ARTICLE créée avec FK vers TCE")
    
    # 3. TABLE PIECES_JOINTE
    print("  → Création table PIECES_JOINTE (clé composée)...")
    cursor.execute(f"""
        CREATE TABLE {schema}.PIECES_JOINTE (
            numero_dossier VARCHAR2(50) NOT NULL,
            reference_base_image VARCHAR2(100) NOT NULL,
            type_document VARCHAR2(100),
            numero_document VARCHAR2(50),
            date_document DATE,
            reference_fichier_joint VARCHAR2(500),
            PRIMARY KEY (numero_dossier, reference_base_image),
            CONSTRAINT FK_PIECES_JOINTE_TCE FOREIGN KEY (numero_dossier)
                REFERENCES {schema}.TCE(numero_dossier) ON DELETE CASCADE
        )
    """)
    cursor.connection.commit()
    print("     ✅ PIECES_JOINTE créée avec FK vers TCE")
    
    # 4. TABLES D'OBSERVATION (sans discriminant naturel → ID généré)
    observation_tables = {
        'MINISTERE_COMMERCE_OBSERVATION': 'id_ministere_commerce_observation',
        'ORGANISME_OBSERVATION': 'id_organisme_observation',
        'BANQUE_OBSERVATION': 'id_banque_observation',
        'ARTICLE_COMMERCE_OBSERVATION': 'id_article_commerce_observation',
        'ARTICLE_TECHNIQUE_OBSERVATION': 'id_article_technique_observation',
        'ARTICLE_BANQUE_OBSERVATION': 'id_article_banque_observation'
    }
    
    for obs_table, id_col in observation_tables.items():
        print(f"  → Création table {obs_table}...")
        
        # Détermine la clé étrangère selon le type
        if 'ARTICLE' in obs_table and obs_table != 'ARTICLE_COMMERCE_OBSERVATION':
            # Enfant d'ARTICLE
            fk_col = 'numero_dossier'
            fk_table = 'ARTICLE'
            fk_constraint = f"""CONSTRAINT FK_{obs_table}_ARTICLE 
                FOREIGN KEY (numero_dossier) REFERENCES {schema}.ARTICLE(numero_dossier) ON DELETE CASCADE"""
        else:
            # Enfant direct de TCE
            fk_col = 'numero_dossier'
            fk_table = 'TCE'
            fk_constraint = f"""CONSTRAINT FK_{obs_table}_TCE 
                FOREIGN KEY (numero_dossier) REFERENCES {schema}.TCE(numero_dossier) ON DELETE CASCADE"""
        
        cursor.execute(f"""
            CREATE TABLE {schema}.{obs_table} (
                {id_col} NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                numero_dossier VARCHAR2(50) NOT NULL,
                code_observation VARCHAR2(50),
                libelle VARCHAR2(500),
                {fk_constraint}
            )
        """)
        cursor.connection.commit()
        print(f"     ✅ {obs_table} créée")
    
    print("\n✅ Schéma TCE régénéré complètement\n")

def verify_fk(cursor, schema='SANA'):
    """Vérifie que toutes les FK sont correctes"""
    print("🔍 Vérification des clés étrangères...\n")
    
    query = f"""
        SELECT CONSTRAINT_NAME, TABLE_NAME, R_TABLE_NAME
        FROM USER_CONSTRAINTS
        WHERE OWNER = '{schema}' 
        AND CONSTRAINT_TYPE = 'R'
        ORDER BY TABLE_NAME
    """
    
    cursor.execute(query)
    fks = cursor.fetchall()
    
    if not fks:
        print("  ⚠️  Aucune FK trouvée (à vérifier)")
    else:
        for fk_name, table_name, ref_table in fks:
            print(f"  ✅ {table_name} → {ref_table} ({fk_name})")
    
    print()

def main():
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # ÉTAPE 1 : Supprimer les anciennes tables
        response = input("⚠️  Cela supprimera TOUTES les tables TCE et leurs données.\nContinuer ? (yes/no) : ")
        if response.lower() != 'yes':
            print("Annulé.")
            return
        
        drop_all_tce_tables(cursor)
        
        # ÉTAPE 2 : Régénérer le schéma
        regenerate_tce_schema(cursor)
        
        # ÉTAPE 3 : Vérifier les FK
        verify_fk(cursor)
        
        print("✅ Réparation terminée !\n")
        print("📝 PROCHAINES ÉTAPES :")
        print("  1. Lance ton pipeline de chargement")
        print("  2. Les données vont insérer correctement maintenant")
        print("  3. Ne renomme PLUS les tables via l'interface (risque de recasser les FK)")
        
    except Exception as e:
        print(f"\n❌ Erreur lors de la réparation : {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
        print("\n🔌 Déconnecté")

if __name__ == '__main__':
    main()
