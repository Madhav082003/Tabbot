import sys
from tableauhyperapi import HyperProcess, Telemetry, Connection, TableName
with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": "/tmp/claude-0/-home-claude/6c8a6dea-8363-51ae-bf45-91a7fdbc3bb0/scratchpad"}) as hp:
    with Connection(hp.endpoint, sys.argv[1]) as c:
        for s in c.catalog.get_schema_names():
            for t in c.catalog.get_table_names(s):
                d = c.catalog.get_table_definition(t)
                n = c.execute_scalar_query(f"SELECT COUNT(*) FROM {t}")
                print(t, n, "rows")
                print("  ", [(col.name.unescaped, str(col.type)) for col in d.columns])
                print(c.execute_list_query(f'SELECT "INITIAL_RECEIVED_DATE", "SOC", "CASEID", "AGE_UNIT", "GENDER" FROM {t} LIMIT 5'))
                print("distinct cases:", c.execute_scalar_query(f'SELECT COUNT(DISTINCT "CASEID") FROM {t}'))
                print("AGE_UNIT:", c.execute_list_query(f'SELECT "AGE_UNIT", COUNT(*) FROM {t} GROUP BY 1'))
                print("GENDER:", c.execute_list_query(f'SELECT "GENDER", COUNT(DISTINCT "CASEID") FROM {t} GROUP BY 1'))
