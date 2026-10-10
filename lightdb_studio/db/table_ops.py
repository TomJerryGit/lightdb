"""表操作 SQL 生成"""


def quote_ident(name):
    return f"`{name}`"


def quote_value(v):
    if v is None:
        return 'NULL'
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    s = str(v).replace('\\', '\\\\').replace("'", "''")
    return f"'{s}'"


def build_select(table, columns=None, where=None, limit=1000, offset=0):
    if not columns or columns == ['*']:
        col_str = '*'
    else:
        col_str = ', '.join(quote_ident(c) for c in columns)
    sql = f"SELECT {col_str} FROM {quote_ident(table)}"
    if where:
        sql += f" WHERE {where}"
    sql += f" LIMIT {limit} OFFSET {offset}"
    return sql


def build_insert(table, columns, values):
    cols = ', '.join(quote_ident(c) for c in columns)
    vals = ', '.join(quote_value(v) for v in values)
    return f"INSERT INTO {quote_ident(table)} ({cols}) VALUES ({vals})"


def build_update(table, pk_cols, pk_values, set_cols, set_values):
    set_str = ', '.join(
        f"{quote_ident(c)} = {quote_value(v)}"
        for c, v in zip(set_cols, set_values)
    )
    where_str = ' AND '.join(
        f"{quote_ident(c)} = {quote_value(v)}"
        for c, v in zip(pk_cols, pk_values)
    )
    return f"UPDATE {quote_ident(table)} SET {set_str} WHERE {where_str}"


def build_delete(table, pk_cols, pk_values):
    where_str = ' AND '.join(
        f"{quote_ident(c)} = {quote_value(v)}"
        for c, v in zip(pk_cols, pk_values)
    )
    return f"DELETE FROM {quote_ident(table)} WHERE {where_str}"


def build_create_table(table, columns, if_not_exists=False, engine='InnoDB'):
    """columns: [{'name','type','length','primary','notnull','unique','default','auto_increment'}]"""
    lines = []
    pk_cols = []
    for c in columns:
        line = f"  {quote_ident(c['name'])} {c['type']}"
        if c.get('length'):
            line += f"({c['length']})"
        if c.get('notnull'):
            line += " NOT NULL"
        if c.get('default') is not None and c.get('default') != '':
            line += f" DEFAULT {c['default']}"
        if c.get('auto_increment'):
            line += " AUTO_INCREMENT"
        if c.get('unique'):
            line += " UNIQUE"
        if c.get('primary'):
            pk_cols.append(c['name'])
        lines.append(line)

    if pk_cols:
        lines.append(
            f"  PRIMARY KEY ({', '.join(quote_ident(c) for c in pk_cols)})")

    ine = "IF NOT EXISTS " if if_not_exists else ""
    sql = f"CREATE TABLE {ine}{quote_ident(table)} (\n"
    sql += ',\n'.join(lines)
    sql += f"\n) ENGINE={engine}"
    return sql