import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect('postgresql://hybrid_user:hybrid_pass@localhost:5432/hiring_platform')
    
    # Query database size
    db_size = await conn.fetchval("SELECT pg_size_pretty(pg_database_size('hiring_platform'));")
    
    # Query table sizes (data vs indexes)
    tables = await conn.fetch("""
        SELECT 
            relname as table_name,
            pg_size_pretty(pg_table_size(C.oid)) as table_size,
            pg_size_pretty(pg_indexes_size(C.oid)) as index_size,
            pg_size_pretty(pg_total_relation_size(C.oid)) as total_size
        FROM pg_class C
        LEFT JOIN pg_namespace N ON (N.oid = C.relnamespace)
        WHERE nspname NOT IN ('pg_catalog', 'information_schema')
          AND C.relkind <> 'i'
          AND nspname !~ '^pg_toast'
        ORDER BY pg_total_relation_size(C.oid) DESC
        LIMIT 10;
    """)
    
    print(f"Total Database Size: {db_size}")
    print("\nTable Breakdown:")
    for row in tables:
        print(f"- {row['table_name']}: Total={row['total_size']} (Data={row['table_size']}, Indexes={row['index_size']})")
        
    await conn.close()

asyncio.run(main())
