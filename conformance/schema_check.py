"""이 저장소의 JSON Schema(draft 2020-12)를 파이썬 표준 라이브러리만으로 확인합니다.

`check_analyzer.py`와 `fake/fake_analyzer.py`가 함께 씁니다. 이 저장소의 스키마가 쓰는 키워드만 알고, 모르는 키워드가
있으면 ValueError로 멈춥니다. 그래서 스키마에 이 확인기가 보지 않는 규칙이 들어가지 않습니다.
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_DIR = os.path.join(ROOT, "schema")

ANNOTATIONS = {"$schema", "$id", "$defs", "$comment", "title", "description", "default", "examples", "then", "else"}

TYPE_NAMES = {
    "null": "null",
    "boolean": "참거짓",
    "integer": "정수",
    "number": "숫자",
    "string": "문자열",
    "array": "배열",
    "object": "객체",
}


def api_version():
    """`API_VERSION` 파일의 버전 문자열입니다."""
    with open(os.path.join(ROOT, "API_VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def load(name):
    """`schema/<name>`의 확인기입니다."""
    with open(os.path.join(SCHEMA_DIR, name), encoding="utf-8") as f:
        return Validator(json.load(f))


def is_type(value, name):
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    raise ValueError("스키마의 type 값을 모릅니다: %r" % name)


def json_equal(a, b):
    # 파이썬에서는 True == 1, False == 0이지만 JSON에서는 다른 값입니다.
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return type(a) is type(b) and a == b
    return a == b


def show(value):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:57] + "..."


def child(path, key):
    if isinstance(key, int):
        return "%s[%d]" % (path, key)
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        return "%s.%s" % (path, key)
    return "%s[%s]" % (path, json.dumps(key, ensure_ascii=False))


class Validator:
    def __init__(self, schema):
        self.root = schema

    def errors(self, value, path="$"):
        """어긴 규칙마다 "경로: 내용" 문장 하나인 목록입니다. 빈 목록이면 스키마를 따릅니다."""
        out = []
        self.check(self.root, value, path, out)
        return out

    def check(self, schema, value, path, out):
        if schema is True:
            return
        if schema is False:
            out.append("%s: 이 자리에는 값이 없어야 합니다" % path)
            return
        for key, rule in schema.items():
            if key in ANNOTATIONS:
                continue
            keyword = KEYWORDS.get(key)
            if keyword is None:
                raise ValueError("스키마 확인기가 모르는 키워드입니다: %s" % key)
            keyword(self, schema, rule, value, path, out)

    def branch(self, schema, value, path):
        out = []
        self.check(schema, value, path, out)
        return out

    def resolve(self, ref):
        if not ref.startswith("#/"):
            raise ValueError("파일 안의 $ref만 받습니다: %s" % ref)
        node = self.root
        for part in ref[2:].split("/"):
            node = node[part]
        return node


def k_type(v, schema, rule, value, path, out):
    names = [rule] if isinstance(rule, str) else rule
    if not any(is_type(value, n) for n in names):
        out.append("%s: %s 형식이어야 합니다 (받은 값 %s)" % (path, " 또는 ".join(TYPE_NAMES[n] for n in names), show(value)))


def k_enum(v, schema, rule, value, path, out):
    if not any(json_equal(value, x) for x in rule):
        out.append("%s: 받을 수 있는 값은 %s입니다 (받은 값 %s)" % (path, ", ".join(show(x) for x in rule), show(value)))


def k_const(v, schema, rule, value, path, out):
    if not json_equal(value, rule):
        out.append("%s: 받을 수 있는 값은 %s입니다 (받은 값 %s)" % (path, show(rule), show(value)))


def k_required(v, schema, rule, value, path, out):
    if isinstance(value, dict):
        for key in rule:
            if key not in value:
                out.append("%s: 필수 키가 없습니다" % child(path, key))


def k_properties(v, schema, rule, value, path, out):
    if isinstance(value, dict):
        for key, sub in rule.items():
            if key in value:
                v.check(sub, value[key], child(path, key), out)


def k_additional(v, schema, rule, value, path, out):
    if isinstance(value, dict):
        known = schema.get("properties", {})
        for key in value:
            if key not in known:
                v.check(rule, value[key], child(path, key), out)


def k_dependent_required(v, schema, rule, value, path, out):
    if isinstance(value, dict):
        for key, needs in rule.items():
            if key in value:
                for need in needs:
                    if need not in value:
                        out.append("%s: %s가 있으면 함께 있어야 합니다" % (child(path, need), key))


def k_items(v, schema, rule, value, path, out):
    if isinstance(value, list):
        for i, item in enumerate(value):
            v.check(rule, item, child(path, i), out)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def k_minimum(v, schema, rule, value, path, out):
    if number(value) and value < rule:
        out.append("%s: %s 이상이어야 합니다 (받은 값 %s)" % (path, rule, show(value)))


def k_maximum(v, schema, rule, value, path, out):
    if number(value) and value > rule:
        out.append("%s: %s 이하여야 합니다 (받은 값 %s)" % (path, rule, show(value)))


def k_exclusive_minimum(v, schema, rule, value, path, out):
    if number(value) and value <= rule:
        out.append("%s: %s보다 커야 합니다 (받은 값 %s)" % (path, rule, show(value)))


def k_min_length(v, schema, rule, value, path, out):
    if isinstance(value, str) and len(value) < rule:
        out.append("%s: %d글자 이상이어야 합니다 (받은 값 %s)" % (path, rule, show(value)))


def k_pattern(v, schema, rule, value, path, out):
    if isinstance(value, str) and not re.search(rule, value):
        out.append("%s: 정규식 %s에 맞아야 합니다 (받은 값 %s)" % (path, rule, show(value)))


def k_ref(v, schema, rule, value, path, out):
    v.check(v.resolve(rule), value, path, out)


def k_all_of(v, schema, rule, value, path, out):
    for sub in rule:
        v.check(sub, value, path, out)


def k_any_of(v, schema, rule, value, path, out):
    results = [v.branch(sub, value, path) for sub in rule]
    if all(results):
        # 어느 경우에도 맞지 않으면 값의 형식이 맞는 경우 가운데 어긴 규칙이 가장 적은 경우의 문장을 보입니다.
        # null 또는 객체인 키에 틀린 객체가 오면 "null이어야 합니다" 대신 객체 안의 위반을 보이기 위함입니다.
        type_only = [r for r in results if len(r) == 1 and r[0].startswith(path + ": ") and "형식이어야" in r[0]]
        deeper = [r for r in results if r not in type_only]
        out.extend(min(deeper or results, key=len))


def k_if(v, schema, rule, value, path, out):
    if not v.branch(rule, value, path):
        if "then" in schema:
            v.check(schema["then"], value, path, out)
    elif "else" in schema:
        v.check(schema["else"], value, path, out)


KEYWORDS = {
    "type": k_type,
    "enum": k_enum,
    "const": k_const,
    "required": k_required,
    "properties": k_properties,
    "additionalProperties": k_additional,
    "dependentRequired": k_dependent_required,
    "items": k_items,
    "minimum": k_minimum,
    "maximum": k_maximum,
    "exclusiveMinimum": k_exclusive_minimum,
    "minLength": k_min_length,
    "pattern": k_pattern,
    "$ref": k_ref,
    "allOf": k_all_of,
    "anyOf": k_any_of,
    "if": k_if,
}
