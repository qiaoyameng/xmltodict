"""Tests for the optional XSI typed mode (``xsi_types=True``)."""

import datetime
from decimal import Decimal

import pytest

from xmltodict import XsiValue, parse, unparse


XSI = "http://www.w3.org/2001/XMLSchema-instance"
XSD = "http://www.w3.org/2001/XMLSchema"

HEAD = (
    '<r xmlns:xsi="%s" xmlns:xsd="%s">' % (XSI, XSD)
)


def typed(body, xsd_type=None, nil=None, prefix="xsi"):
    attrs = ""
    if xsd_type is not None:
        attrs += ' %s:type="xsd:%s"' % (prefix, xsd_type)
    if nil is not None:
        attrs += ' %s:nil="%s"' % (prefix, nil)
    return HEAD + "<v%s>%s</v></r>" % (attrs, body)


def value_of(doc):
    return parse(doc, xsi_types=True)["r"]["v"]


def test_default_mode_is_unchanged():
    xml = HEAD + '<a xsi:nil="true"/><b xsi:type="xsd:integer">5</b></r>'
    result = parse(xml)
    assert result == {
        "r": {
            "@xmlns:xsi": XSI,
            "@xmlns:xsd": XSD,
            "a": {"@xsi:nil": "true"},
            "b": {"@xsi:type": "xsd:integer", "#text": "5"},
        }
    }


def test_string_type_preserves_whitespace():
    assert value_of(typed("\n a\tb ", "string")) == XsiValue(
        "\n a\tb ", xsd_local="string")


def test_boolean_lexical_forms():
    for literal, expected in [("true", True), ("1", True),
                              ("false", False), ("0", False)]:
        assert value_of(typed(literal, "boolean")) == XsiValue(
            expected, xsd_local="boolean")


def test_boolean_rejects_non_lexical_values():
    for literal in ("yes", "TRUE", "False", "", "  "):
        with pytest.raises(ValueError):
            value_of(typed(literal, "boolean"))


def test_integer_conversion_and_whitespace_collapse():
    assert value_of(typed(" \t 42 \n", "integer")) == XsiValue(
        42, xsd_local="integer")
    for literal in ("42.0", "12 3", "abc", ""):
        with pytest.raises(ValueError):
            value_of(typed(literal, "integer"))


def test_decimal_uses_decimal_type_and_preserves_scale():
    result = value_of(typed("3.140", "decimal"))
    assert result == XsiValue(Decimal("3.140"), xsd_local="decimal")
    assert isinstance(result.value, Decimal)
    assert str(result.value) == "3.140"
    with pytest.raises(ValueError):
        value_of(typed("1.2.3", "decimal"))


def test_datetime_preserves_timezone():
    utc = value_of(typed("2026-10-02T13:45:30Z", "dateTime"))
    assert utc == XsiValue(
        datetime.datetime(2026, 10, 2, 13, 45, 30,
                          tzinfo=datetime.timezone.utc),
        xsd_local="dateTime")

    offset = value_of(typed("2026-10-02T13:45:30+08:00", "dateTime"))
    assert offset.value.utcoffset() == datetime.timedelta(hours=8)

    west = value_of(typed("2026-10-02T13:45:30.1234567-05:30", "dateTime"))
    assert west.value.utcoffset() == datetime.timedelta(hours=-5, minutes=-30)
    assert west.value.microsecond == 123456

    naive = value_of(typed("2026-10-02T13:45:30", "dateTime"))
    assert naive.value.tzinfo is None


def test_datetime_rejects_bad_lexical_forms():
    for literal in (
        "2026-13-02T13:45:30Z",
        "2026-10-02T25:45:30Z",
        "2026-10-02 13:45:30Z",
        "2026-10-02t13:45:30z",
        "not-a-date",
    ):
        with pytest.raises(ValueError):
            value_of(typed(literal, "dateTime"))


def test_base64_binary():
    assert value_of(typed("aGVs bG8=\n", "base64Binary")) == XsiValue(
        b"hello", xsd_local="base64Binary")
    assert value_of(typed("  ", "base64Binary")) == XsiValue(
        b"", xsd_local="base64Binary")
    with pytest.raises(ValueError):
        value_of(typed("abc@def", "base64Binary")
                 )  # invalid character for base64


def test_nil_true_and_empty_and_absent_are_distinct():
    xml = HEAD + '<a xsi:nil="true"/><b></b><c/></r>'
    result = parse(xml, xsi_types=True)["r"]
    assert result["a"] == XsiValue(None, nil=True)
    assert result["b"] is None
    assert result["c"] is None
    assert "missing" not in result


def test_nil_true_rejects_content():
    with pytest.raises(ValueError):
        parse(HEAD + '<a xsi:nil="true">x</a></r>', xsi_types=True)
    with pytest.raises(ValueError):
        parse(HEAD + '<a xsi:nil="true"><c/></a></r>', xsi_types=True)


def test_nil_whitespace_only_is_allowed():
    result = parse(HEAD + '<a xsi:nil="true">  </a></r>', xsi_types=True)
    assert result["r"]["a"] == XsiValue(None, nil=True)


def test_nil_false_variants_are_kept_as_attributes():
    for literal in ("false", "0"):
        result = parse(
            HEAD + '<a xsi:nil="%s">v</a></r>' % literal, xsi_types=True)
        assert result["r"]["a"] == {
            "@xsi:nil": literal, "#text": "v"}


def test_nil_with_other_attributes_keeps_marker_in_text():
    result = parse(
        HEAD + '<a id="9" xsi:nil="true"/></r>', xsi_types=True)
    assert result["r"]["a"] == {
        "@id": "9", "#text": XsiValue(None, nil=True)}


def test_nil_attribute_must_be_boolean():
    with pytest.raises(ValueError):
        parse(HEAD + '<a xsi:nil="maybe"/>', xsi_types=True)


def test_prefix_rebinding_is_recognized():
    xml = (
        '<r xmlns:inst="%s" xmlns:schema="%s">'
        '<a inst:type="schema:boolean">1</a>'
        '<b inst:nil="true"/>'
        '<c xmlns:other="%s" other:type="schema:integer">7</c>'
        '</r>' % (XSI, XSD, XSI)
    )
    result = parse(xml, xsi_types=True)["r"]
    assert result["a"] == XsiValue(True, xsd_local="boolean")
    assert result["b"] == XsiValue(None, nil=True)
    assert result["c"]["#text"] == XsiValue(7, xsd_local="integer")


def test_prefix_bound_on_the_typed_element_itself():
    xml = (
        '<r><v xmlns:i="%s" xmlns:x="%s" i:type="x:decimal">2.5</v></r>'
        % (XSI, XSD)
    )
    result = parse(xml, xsi_types=True)["r"]["v"]
    assert result["#text"] == XsiValue(Decimal("2.5"), xsd_local="decimal")


def test_foreign_namespace_attributes_are_ignored():
    xml = '<r xmlns:z="urn:z" z:nil="true">keep me</r>'
    result = parse(xml, xsi_types=True)["r"]
    assert result["@z:nil"] == "true"
    assert result["#text"] == "keep me"


def test_unknown_type_keeps_raw_text_by_default():
    xml = (
        '<r xmlns:xsi="%s" xmlns:weird="urn:weird">'
        '<v xsi:type="weird:thing"> raw </v></r>' % XSI
    )
    result = parse(xml, xsi_types=True)["r"]["v"]
    assert result["#text"] == XsiValue(" raw ", type_name="weird:thing")


def test_unknown_type_strict_mode_raises():
    xml = (
        '<r xmlns:xsi="%s" xmlns:weird="urn:weird">'
        '<v xsi:type="weird:thing">raw</v></r>' % XSI
    )
    with pytest.raises(ValueError):
        parse(xml, xsi_types=True, xsi_strict=True)


def test_nested_typed_lists():
    xml = HEAD + (
        '<item xsi:type="xsd:integer">1</item>'
        '<item xsi:type="xsd:integer">2</item>'
        '<item xsi:nil="true"/></r>'
    )
    result = parse(xml, xsi_types=True, force_list=("item",))
    assert result["r"]["item"] == [
        XsiValue(1, xsd_local="integer"),
        XsiValue(2, xsd_local="integer"),
        XsiValue(None, nil=True),
    ]


def test_typed_values_roundtrip():
    xml = HEAD + (
        '<a xsi:nil="true"/>'
        '<b xsi:type="xsd:integer">42</b>'
        '<c xsi:type="xsd:boolean">false</c>'
        '<d xsi:type="xsd:decimal">3.140</d>'
        '<e xsi:type="xsd:dateTime">2026-10-02T13:45:30+08:00</e>'
        '<f xsi:type="xsd:base64Binary">aGVsbG8=</f>'
        '<g>plain</g>'
        '<h/>'
        '<i xsi:type="xsd:string">  keep  </i></r>'
    )
    parsed = parse(xml, xsi_types=True)
    emitted = unparse(parsed, xsi_types=True)
    assert parse(emitted, xsi_types=True) == parsed


def test_unparse_uses_bound_prefixes():
    data = {
        "r": {
            "@xmlns:inst": XSI,
            "@xmlns:schema": XSD,
            "v": XsiValue(5, xsd_local="integer"),
        }
    }
    emitted = unparse(data, xsi_types=True, full_document=False)
    assert "xmlns:xsi" not in emitted
    assert "xmlns:xsd" not in emitted
    assert 'inst:type="schema:integer"' in emitted
    assert parse(emitted, xsi_types=True)["r"]["v"] == XsiValue(
        5, xsd_local="integer")


def test_unparse_declares_default_prefixes_when_missing():
    emitted = unparse(
        {"r": {"v": XsiValue(5, xsd_local="integer")}},
        xsi_types=True, full_document=False)
    assert 'xmlns:xsi="%s"' % XSI in emitted
    assert 'xmlns:xsd="%s"' % XSD in emitted
    assert 'xsi:type="xsd:integer"' in emitted


def test_unparse_native_python_types():
    data = {"r": {
        "i": 7,
        "flag": True,
        "money": Decimal("1.5"),
        "when": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        "blob": b"hi",
    }}
    emitted = unparse(data, xsi_types=True)
    reparsed = parse(emitted, xsi_types=True)["r"]
    assert reparsed["i"] == XsiValue(7, xsd_local="integer")
    assert reparsed["flag"] == XsiValue(True, xsd_local="boolean")
    assert reparsed["money"] == XsiValue(Decimal("1.5"), xsd_local="decimal")
    assert reparsed["when"] == XsiValue(
        datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        xsd_local="dateTime")
    assert reparsed["blob"] == XsiValue(b"hi", xsd_local="base64Binary")


def test_unparse_none_vs_nil_marker():
    data = {"r": {"a": None, "b": "", "c": XsiValue(None, nil=True)}}
    emitted = unparse(data, xsi_types=True, full_document=False)
    assert emitted == (
        '<r xmlns:xsi="%s">'
        '<a></a><b></b><c xsi:nil="true"></c></r>' % XSI)
    reparsed = parse(emitted, xsi_types=True)["r"]
    assert reparsed["a"] is None
    assert reparsed["b"] is None
    assert reparsed["c"] == XsiValue(None, nil=True)


def test_unparse_unknown_type_marker():
    data = {
        "r": {
            "@xmlns:xsi": XSI,
            "@xmlns:weird": "urn:weird",
            "v": XsiValue("raw text", type_name="weird:thing"),
        }
    }
    emitted = unparse(data, xsi_types=True, full_document=False)
    assert 'xsi:type="weird:thing"' in emitted
    assert parse(emitted, xsi_types=True)["r"]["v"]["#text"] == XsiValue(
        "raw text", type_name="weird:thing")


def test_xsi_value_stringifies_in_default_unparse():
    assert unparse(
        {"r": XsiValue(5, xsd_local="integer")},
        full_document=False) == "<r>5</r>"
    assert unparse(
        {"r": XsiValue(None, nil=True)},
        full_document=False) == "<r></r>"


def test_streaming_mode_converts_typed_items():
    xml = HEAD + (
        '<item xsi:type="xsd:integer">1</item>'
        '<item xsi:nil="true"/></r>')
    items = []
    parse(xml, xsi_types=True, item_depth=2,
          item_callback=lambda path, item: items.append(item) or True)
    assert items == [
        XsiValue(1, xsd_local="integer"),
        XsiValue(None, nil=True),
    ]


def test_xsi_types_incompatible_with_process_namespaces():
    with pytest.raises(ValueError):
        parse(HEAD + "<v>1</v></r>", xsi_types=True,
              process_namespaces=True)


def test_complex_content_with_type_keeps_raw_attribute():
    xml = HEAD + (
        '<v xsi:type="weird:thing" xmlns:weird="urn:weird">'
        '<c>1</c></v></r>')
    result = parse(xml, xsi_types=True)["r"]["v"]
    assert result["@xsi:type"] == "weird:thing"
    assert result["c"] == "1"


def test_nil_with_type_attribute():
    result = parse(
        HEAD + '<a id="1" xsi:nil="true" xsi:type="xsd:string"/></r>',
        xsi_types=True)
    assert result["r"]["a"] == {
        "@id": "1", "#text": XsiValue(None, nil=True)}
    emitted = unparse(result, xsi_types=True)
    assert 'xsi:nil="true"' in emitted
    assert parse(emitted, xsi_types=True) == result


def test_custom_attr_prefix_and_cdata_key_roundtrip():
    xml = HEAD + '<a id="1" xsi:type="xsd:integer">5</a></r>'
    parsed = parse(xml, xsi_types=True, attr_prefix="~", cdata_key="$")
    assert parsed["r"]["a"]["$"] == XsiValue(5, xsd_local="integer")
    emitted = unparse(parsed, xsi_types=True,
                      attr_prefix="~", cdata_key="$")
    assert parse(emitted, xsi_types=True,
                 attr_prefix="~", cdata_key="$") == parsed
