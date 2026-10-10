"""JSON 类型支持 + 函数库"""
import json


def parse_path(path: str):
    """'$.a.b[0]' → ['a', 'b', 0]"""
    if path.startswith('$'):
        path = path[1:]
    parts = []
    cur = ''
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == '.':
            if cur:
                parts.append(cur)
                cur = ''
        elif ch == '[':
            if cur:
                parts.append(cur)
                cur = ''
            j = path.find(']', i)
            if j > i:
                idx = path[i + 1:j].strip()
                if idx and (idx[0] in "\"'"):
                    parts.append(idx[1:-1])
                else:
                    try:
                        parts.append(int(idx))
                    except ValueError:
                        parts.append(idx)
                i = j
        else:
            cur += ch
        i += 1
    if cur:
        parts.append(cur)
    return parts


def _to_obj(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def json_extract(value, path):
    obj = _to_obj(value)
    if obj is None:
        return None
    parts = parse_path(path)
    cur = obj
    for p in parts:
        if isinstance(cur, dict):
            if p in cur:
                cur = cur[p]
            else:
                return None
        elif isinstance(cur, list):
            if isinstance(p, int) and 0 <= p < len(cur):
                cur = cur[p]
            else:
                return None
        else:
            return None
    return cur


def json_extract_text(value, path):
    """->> 结果转文本"""
    v = json_extract(value, path)
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(',', ':'))
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return str(v)


def json_set_func(value, path, new_value):
    obj = _to_obj(value)
    if obj is None:
        obj = {}
    parts = parse_path(path)
    _set_recursive(obj, parts, new_value)
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def _set_recursive(obj, parts, val):
    if not parts:
        return
    key = parts[0]
    rest = parts[1:]
    if isinstance(obj, dict):
        if not rest:
            obj[key] = val
        else:
            if key not in obj or not isinstance(obj[key], (dict, list)):
                obj[key] = {} if not isinstance(rest[0], int) else []
            _set_recursive(obj[key], rest, val)
    elif isinstance(obj, list):
        if isinstance(key, int) and 0 <= key < len(obj):
            if not rest:
                obj[key] = val
            else:
                _set_recursive(obj[key], rest, val)


def json_remove(value, path):
    obj = _to_obj(value)
    if obj is None:
        return None
    parts = parse_path(path)
    _remove_recursive(obj, parts)
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def _remove_recursive(obj, parts):
    if not parts:
        return
    key = parts[0]
    rest = parts[1:]
    if not rest:
        if isinstance(obj, dict) and key in obj:
            del obj[key]
        elif isinstance(obj, list) and isinstance(key, int) and 0 <= key < len(obj):
            del obj[key]
        return
    if isinstance(obj, dict) and key in obj:
        _remove_recursive(obj[key], rest)
    elif isinstance(obj, list) and isinstance(key, int) and 0 <= key < len(obj):
        _remove_recursive(obj[key], rest)


def json_keys(value):
    obj = _to_obj(value)
    if isinstance(obj, dict):
        return json.dumps(list(obj.keys()), ensure_ascii=False)
    return None


def json_length(value):
    obj = _to_obj(value)
    if isinstance(obj, (list, dict)):
        return len(obj)
    return None


def json_type(value):
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'BOOLEAN'
    if isinstance(value, int):
        return 'INTEGER'
    if isinstance(value, float):
        return 'DOUBLE'
    if isinstance(value, str):
        try:
            obj = json.loads(value)
            if isinstance(obj, dict):
                return 'OBJECT'
            if isinstance(obj, list):
                return 'ARRAY'
            return 'STRING'
        except (ValueError, TypeError):
            return 'STRING'
    if isinstance(value, dict):
        return 'OBJECT'
    if isinstance(value, list):
        return 'ARRAY'
    return 'UNKNOWN'


def json_valid(value):
    if value is None:
        return False
    if not isinstance(value, str):
        return True
    try:
        json.loads(value)
        return True
    except (ValueError, TypeError):
        return False


def json_object(*args):
    if len(args) % 2 != 0:
        raise ValueError("JSON_OBJECT 需要偶数个参数")
    obj = {}
    for i in range(0, len(args), 2):
        obj[str(args[i])] = args[i + 1]
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def json_array(*args):
    return json.dumps(list(args), ensure_ascii=False, separators=(',', ':'))


def json_merge(*args):
    results = []
    for a in args:
        obj = _to_obj(a)
        if isinstance(obj, list):
            results.extend(obj)
    return json.dumps(results, ensure_ascii=False, separators=(',', ':'))


# 函数注册表
JSON_FUNCTIONS = {
    'JSON_EXTRACT': lambda v, p: json.dumps(json_extract(v, p), ensure_ascii=False, separators=(',', ':'))
                    if isinstance(json_extract(v, p), (dict, list)) else json_extract(v, p),
    'JSON_UNQUOTE': lambda v: json_extract_text(v, '$') if False else (v[1:-1] if isinstance(v, str) and v.startswith('"') else v),
    'JSON_SET': json_set_func,
    'JSON_REMOVE': json_remove,
    'JSON_KEYS': json_keys,
    'JSON_LENGTH': json_length,
    'JSON_TYPE': json_type,
    'JSON_VALID': json_valid,
    'JSON_OBJECT': json_object,
    'JSON_ARRAY': json_array,
    'JSON_MERGE': json_merge,
}