"""Tests for the optional XSI typed mode (``xsi_types=True``).

Covers ``xsi:nil`` and ``xsi:type`` handling for the supported XML Schema
built-in types, unknown type policies, prefix rebinding, parse/unparse
round-trips and the default-mode compatibility guarantees.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from xmltodict import parse, unparse, XsiValue, XSI_NS, XSD_NS


XSI_ATTR = 'xmlns:xsi="{}"'.format(XSI_NS)
XSD_ATTR = 'xmlns:xsd="{}"'.format(XSD_NS)
NS_DECLS = '{} {}'.format(XSI_ATTR, XSD_ATTR)


def wrap(body, decls=NS_DECLS):
    return '<root {}>{}</root>'.format(decls, body)


# ---------------------------------------------------------------------------
# Default mode must stay unchanged
# ---------------------------------------------------------------------------

def test_default_mode_nil_is_plain_text_dict():
    xml = wrap('<amount xsi:nil="true"/><amount2/>')
    result = parse(xml)
    assert result['root']['amount'] == {'@xsi:nil': 'true'}
    assert result['root']['amount2'] is None


def test_default_mode_empty_element_is_none():
    assert parse('<a/>') == {'a': None}
    assert parse('<a></a>') == {'a': None}


def test_default_mode_unparse_has_no_xsi():
    output = unparse({'a': None})
    assert 'xsi' not in output
    assert '<a></a>' in output


# ---------------------------------------------------------------------------
# xsi:nil
# ---------------------------------------------------------------------------

def test_nil_becomes_none():
    result = parse(wrap('<amount xsi:nil="true"/>'), xsi_types=True)
    assert result['root']['amount'] is None


def test_nil_false_is_not_none():
    result = parse(
        wrap('<amount xsi:nil="false">5</amount>'), xsi_types=True)
    assert result['root']['amount'] == '5'


def test_nil_with_other_attributes_keeps_marker():
    xml = wrap('<amount currency="USD" xsi:nil="true"/>')
    result = parse(xml, xsi_types=True)
    assert result['root']['amount'] == {
        '@currency': 'USD', '@xsi:nil': 'true'}


def test_nil_with_text_is_rejected():
    with pytest.raises(ValueError):
        parse(wrap('<amount xsi:nil="true">money</amount>'), xsi_types=True)


def test_nil_with_whitespace_text_is_allowed():
    result = parse(
        wrap('<amount xsi:nil="true">  \n </amount>'), xsi_types=True)
    assert result['root']['amount'] is None


def test_nil_with_child_element_is_rejected():
    with pytest.raises(ValueError):
        parse(
            wrap('<amount xsi:nil="true"><child/></amount>'),
            xsi_types=True)


@pytest.mark.parametrize('literal', ['TRUE', 'yes', '2', '', 'banana'])
def test_nil_invalid_lexical_value_is_rejected(literal):
    with pytest.raises(ValueError):
        parse(
            wrap('<amount xsi:nil="{}"/>'.format(literal)),
            xsi_types=True)


def test_none_empty_text_and_missing_are_distinct():
    xml = wrap('<a xsi:nil="true"/><b></b><c>x</c>')
    result = parse(xml, xsi_types=True)['root']
    assert result['a'] is None
    assert result['b'] == ''
    assert result['c'] == 'x'
    assert 'd' not in result


def test_nil_prefix_does_not_have_to_be_xsi():
    xml = '<a xmlns:xx="{}"><b xx:nil="true"/></a>'.format(XSI_NS)
    result = parse(xml, xsi_types=True)
    assert result['a']['b'] is None


def test_nil_repeated_into_list():
    result = parse(
        wrap('<b xsi:nil="true"/><b xsi:nil="true"/>'), xsi_types=True)
    assert result['root']['b'] == [None, None]


# ---------------------------------------------------------------------------
# xsi:type for built-in types
# ---------------------------------------------------------------------------

def test_boolean_type():
    result = parse(
        wrap('<b xsi:type="xsd:boolean">true</b>'), xsi_types=True)
    assert result['root']['b'] is True


@pytest.mark.parametrize('literal,expected', [
    ('true', True), ('1', True),
    ('false', False), ('0', False),
    ('  1 ', True), ('\n0\t', False),
])
def test_boolean_lexical_forms(literal, expected):
    xml = wrap('<b xsi:type="xsd:boolean">{}</b>'.format(literal))
    assert parse(xml, xsi_types=True)['root']['b'] is expected


@pytest.mark.parametrize('literal', ['TRUE', 'False', 'yes', '2', '', 'tr ue'])
def test_boolean_invalid_lexical_forms(literal):
    xml = wrap('<b xsi:type="xsd:boolean">{}</b>'.format(literal))
    with pytest.raises(ValueError):
        parse(xml, xsi_types=True)


def test_integer_type():
    result = parse(
        wrap('<b xsi:type="xsd:integer"> 42 </b>'), xsi_types=True)
    value = result['root']['b']
    assert value == 42
    assert isinstance(value, int)


@pytest.mark.parametrize('literal,expected', [('42', 42), ('-17', -17),
                                              ('+17', 17), ('0001', 1)])
def test_integer_lexical_forms(literal, expected):
    xml = wrap('<b xsi:type="xsd:integer">{}</b>'.format(literal))
    assert parse(xml, xsi_types=True)['root']['b'] == expected


@pytest.mark.parametrize('literal', ['1.5', '1e3', '1 2', '0x1', ''])
def test_integer_invalid_lexical_forms(literal):
    xml = wrap('<b xsi:type="xsd:integer">{}</b>'.format(literal))
    with pytest.raises(ValueError):
        parse(xml, xsi_types=True)


def test_decimal_type_uses_decimal():
    result = parse(
        wrap('<b xsi:type="xsd:decimal">19.99</b>'), xsi_types=True)
    value = result['root']['b']
    assert value == Decimal('19.99')
    assert isinstance(value, Decimal)


@pytest.mark.parametrize('literal,expected', [
    ('19.99', Decimal('19.99')),
    ('.5', Decimal('0.5')),
    ('1.', Decimal('1')),
    ('-0.001', Decimal('-0.001')),
])
def test_decimal_lexical_forms(literal, expected):
    xml = wrap('<b xsi:type="xsd:decimal">{}</b>'.format(literal))
    assert parse(xml, xsi_types=True)['root']['b'] == expected


@pytest.mark.parametrize('literal', [
    '1.2.3', 'NaN', 'INF', '-Infinity', '1e3', '', 'abc'])
def test_decimal_invalid_lexical_forms(literal):
    xml = wrap('<b xsi:type="xsd:decimal">{}</b>'.format(literal))
    with pytest.raises(ValueError):
        parse(xml, xsi_types=True)


def test_datetime_preserves_timezone():
    xml = wrap('<b xsi:type="xsd:dateTime">2026-10-02T13:45:30+08:00</b>')
    value = parse(xml, xsi_types=True)['root']['b']
    assert value == datetime(2026, 10, 2, 13, 45, 30,
                             tzinfo=timezone(timedelta(hours=8)))
    assert value.utcoffset() == timedelta(hours=8)


@pytest.mark.parametrize('literal,expected', [
    ('2026-10-02T13:45:30Z', datetime(2026, 10, 2, 13, 45, 30,
                                      tzinfo=timezone.utc)),
    ('2026-10-02T05:45:30.250+02:00',
     datetime(2026, 10, 2, 5, 45, 30, 250000,
              tzinfo=timezone(timedelta(hours=2)))),
    ('2026-10-02T05:45:30.123456-05:00',
     datetime(2026, 10, 2, 5, 45, 30, 123456,
              tzinfo=timezone(-timedelta(hours=5)))),
])
def test_datetime_lexical_forms(literal, expected):
    xml = wrap('<b xsi:type="xsd:dateTime">{}</b>'.format(literal))
    assert parse(xml, xsi_types=True)['root']['b'] == expected


@pytest.mark.parametrize('literal', [
    '2026-13-01T00:00:00Z',
    '2026-02-30T00:00:00Z',
    'not-a-date',
    '2026-10-02 13:45:30Z',
    '',
])
def test_datetime_invalid_lexical_forms(literal):
    xml = wrap('<b xsi:type="xsd:dateTime">{}</b>'.format(literal))
    with pytest.raises(ValueError):
        parse(xml, xsi_types=True)


def test_base64_type_decodes_to_bytes():
    result = parse(
        wrap('<b xsi:type="xsd:base64Binary">aGVsbG8=</b>'),
        xsi_types=True)
    value = result['root']['b']
    assert value == b'hello'
    assert isinstance(value, bytes)


def test_base64_allows_xml_whitespace():
    xml = wrap(
        '<b xsi:type="xsd:base64Binary">\n\taGVs\n  bG8=\r\n </b>')
    assert parse(xml, xsi_types=True)['root']['b'] == b'hello'


@pytest.mark.parametrize('literal', ['aGVs!G8=', 'abc', 'ab=d', '===='])
def test_base64_invalid_lexical_forms(literal):
    xml = wrap('<b xsi:type="xsd:base64Binary">{}</b>'.format(literal))
    with pytest.raises(ValueError):
        parse(xml, xsi_types=True)


def test_string_preserves_inner_whitespace():
    xml = wrap('<b xsi:type="xsd:string">  hi  there </b>')
    assert parse(xml, xsi_types=True)['root']['b'] == '  hi  there '


def test_bare_builtin_type_name_without_prefix():
    xml = '<a xmlns:xsi="{}"><b xsi:type="integer">5</b>' \
          '<c xsi:type="boolean">true</c></a>'.format(XSI_NS)
    result = parse(xml, xsi_types=True)['a']
    assert result['b'] == 5
    assert result['c'] is True


def test_type_prefix_rebinding():
    xml = (
        '<a xmlns:xx="{}" xmlns:xs="{}">'
        '<b xx:type="xs:integer">7</b>'
        '<c xx:type="xs:boolean">0</c>'
        '</a>').format(XSI_NS, XSD_NS)
    result = parse(xml, xsi_types=True)['a']
    assert result['b'] == 7
    assert result['c'] is False


def test_typed_repeated_elements_become_list():
    xml = wrap(
        '<b xsi:type="xsd:integer">1</b>'
        '<b xsi:type="xsd:integer">2</b>'
        '<b xsi:type="xsd:integer">3</b>')
    assert parse(xml, xsi_types=True)['root']['b'] == [1, 2, 3]


def test_typed_element_with_children_is_rejected():
    with pytest.raises(ValueError):
        parse(
            wrap('<b xsi:type="xsd:string"><c/></b>'), xsi_types=True)



def test_empty_typed_scalar_value_is_rejected_for_non_string_types():
    with pytest.raises(ValueError):
        parse(wrap('<b xsi:type="xsd:integer"></b>'), xsi_types=True)


def test_empty_typed_string_is_empty_text():
    result = parse(
        wrap('<b xsi:type="xsd:string"></b>'), xsi_types=True)
    assert result['root']['b'] == ''


# ---------------------------------------------------------------------------
# Unknown types
# ---------------------------------------------------------------------------

def test_unknown_type_keeps_raw_text_by_default():
    xml = (
        '<a xmlns:xsi="{}" xmlns:z="http://z.example">'
        '<b xsi:type="z:thing">raw text</b></a>').format(XSI_NS)
    result = parse(xml, xsi_types=True)
    assert result['a']['b'] == XsiValue('raw text', 'z:thing')


def test_unknown_type_strict_mode_raises():
    xml = (
        '<a xmlns:xsi="{}" xmlns:z="http://z.example">'
        '<b xsi:type="z:thing">raw text</b></a>').format(XSI_NS)
    with pytest.raises(ValueError):
        parse(xml, xsi_types=True, xsi_unknown_types='error')


def test_unknown_type_invalid_policy_raises():
    with pytest.raises(ValueError):
        parse('<a/>', xsi_types=True, xsi_unknown_types='banana')


def test_type_with_undeclared_prefix_raises():
    with pytest.raises(ValueError):
        parse(
            wrap('<b xsi:type="zz:thing">x</b>'), xsi_types=True)


# ---------------------------------------------------------------------------
# process_namespaces modes
# ---------------------------------------------------------------------------

def test_xsi_with_process_namespaces():
    xml = wrap(
        '<a xsi:type="xsd:integer">5</a><b xsi:nil="true"/>')
    result = parse(xml, process_namespaces=True, xsi_types=True)['root']
    assert result['a'] == 5
    assert result['b'] is None


def test_xsi_with_process_namespaces_and_mapping():
    result = parse(
        wrap('<a xsi:type="xsd:integer">5</a>'),
        process_namespaces=True,
        namespaces={XSI_NS: 'xsi', XSD_NS: 'xsd'},
        xsi_types=True)
    assert result['root']['a'] == 5


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def test_streaming_applies_xsi_conversion():
    xml = wrap(
        '<a xsi:type="xsd:integer">5</a>'
        '<b xsi:nil="true"/>'
        '<c></c>'
        '<d>hi</d>')
    seen = []
    parse(xml, item_depth=2,
          item_callback=lambda path, item: seen.append(item) or True,
          xsi_types=True)
    assert seen == [5, None, '', 'hi']


# ---------------------------------------------------------------------------
# unparse
# ---------------------------------------------------------------------------

def test_unparse_nil_empty_and_missing_distinction():
    output = unparse(
        {'root': {'a': None, 'b': '', 'c': 'x'}}, xsi_types=True)
    back = parse(output, xsi_types=True)['root']
    assert back['a'] is None
    assert back['b'] == ''
    assert back['c'] == 'x'
    assert 'd' not in back
    assert 'xsi:nil="true"' in output


def test_unparse_native_types_get_type_markers():
    output = unparse({'root': {
        'flag': True,
        'count': 42,
        'price': Decimal('19.99'),
        'when': datetime(2026, 10, 2, 13, 45, 30,
                         tzinfo=timezone(timedelta(hours=8))),
        'data': b'hello',
    }}, xsi_types=True)
    assert 'xsi:type="xsd:boolean"' in output
    assert 'xsi:type="xsd:integer"' in output
    assert 'xsi:type="xsd:decimal"' in output
    assert 'xsi:type="xsd:dateTime"' in output
    assert 'xsi:type="xsd:base64Binary"' in output
    assert 'aGVsbG8=' in output
    back = parse(output, xsi_types=True)['root']
    assert back['flag'] is True
    assert back['count'] == 42
    assert back['price'] == Decimal('19.99')
    assert back['when'].utcoffset() == timedelta(hours=8)
    assert back['data'] == b'hello'


def test_unparse_nilled_element_with_attributes():
    output = unparse(
        {'a': {'amount': {'@currency': 'USD', '#text': None}}},
        xsi_types=True)
    back = parse(output, xsi_types=True)['a']['amount']
    assert back == {'@currency': 'USD', '@xsi:nil': 'true'}


def test_unparse_native_typed_cdata_with_attributes():
    output = unparse(
        {'order': {'price': {'@cur': 'USD', '#text': Decimal('19.99')}}},
        xsi_types=True)
    back = parse(output, xsi_types=True)['order']['price']
    assert back == {'@cur': 'USD', '#text': Decimal('19.99')}


def test_unparse_explicit_xsi_value_unknown_type():
    output = unparse({'a': {
        '@xmlns': {'z': 'http://z.example'},
        'b': XsiValue('raw text', 'z:thing'),
    }}, xsi_types=True)
    back = parse(output, xsi_types=True)['a']['b']
    assert back == XsiValue('raw text', 'z:thing')


def test_unparse_explicit_string_marker():
    output = unparse({'a': {
        'b': XsiValue('hi', 'xsd:string'),
    }}, xsi_types=True)
    assert 'xsi:type="xsd:string"' in output
    assert parse(output, xsi_types=True)['a']['b'] == 'hi'

def test_unparse_explicit_bare_builtin_marker_gets_prefix():
    output = unparse({'a': {'b': XsiValue(5, 'integer')}}, xsi_types=True)
    assert 'xsi:type="xsd:integer"' in output
    assert '<b xsi:type="xsd:integer">5</b>' in output
    value = parse(output, xsi_types=True)['a']['b']
    assert value == 5 and isinstance(value, int)


def test_unparse_typed_lists():
    output = unparse({'a': {
        'n': [1, 2, 3],
        'm': [None, None],
        'f': [True, False],
    }}, xsi_types=True)
    back = parse(output, xsi_types=True)['a']
    assert back['n'] == [1, 2, 3]
    assert back['m'] == [None, None]
    assert back['f'] == [True, False]


def test_unparse_rebinds_prefixes_from_arguments():
    output = unparse({'a': {
        '@xmlns': {'xx': XSI_NS, 'xs': XSD_NS},
        'b': 7, 'c': None,
    }}, xsi_types=True, xsi_prefix='xx', xsd_prefix='xs')
    assert 'xx:type="xs:integer"' in output
    assert 'xx:nil="true"' in output
    back = parse(output, xsi_types=True)['a']
    assert back['b'] == 7
    assert back['c'] is None


def test_unparse_detects_prefixes_from_root_xmlns():
    output = unparse({'a': {
        '@xmlns': {'xx': XSI_NS, 'xs': XSD_NS},
        'b': 7,
    }}, xsi_types=True)
    assert 'xx:type="xs:integer"' in output
    assert 'xmlns:xx="{}"'.format(XSI_NS) in output


def test_unparse_does_not_mutate_input():
    source = {'a': {'b': 7}}
    unparse(source, xsi_types=True)
    assert source == {'a': {'b': 7}}


# ---------------------------------------------------------------------------
# Full round trips
# ---------------------------------------------------------------------------

def test_roundtrip_preserves_types_and_values():
    original = {'doc': {
        's': 'hello world',
        'flag': True,
        'no': False,
        'count': 42,
        'neg': -17,
        'price': Decimal('19.99'),
        'when': datetime(2026, 10, 2, 13, 45, 30, 250000,
                         tzinfo=timezone(timedelta(hours=8))),
        'blob': b'\x00\x01\x02\xff',
        'nilled': None,
        'empty': '',
        'list': [1, 2, 3],
        'nillist': [None, None],
    }}
    output = unparse(original, xsi_types=True)
    once = parse(output, xsi_types=True)
    assert {k: v for k, v in once['doc'].items()
            if not k.startswith('@')} == original['doc']
    # A second serialization stays stable in type/value terms.
    twice = parse(unparse(once, xsi_types=True), xsi_types=True)
    assert {k: v for k, v in twice['doc'].items()
            if not k.startswith('@')} == original['doc']


def test_roundtrip_unknown_type_keeps_qname_and_text():
    original = {'doc': {
        '@xmlns': {'z': 'http://z.example'},
        'u': XsiValue('some raw text', 'z:thing'),
    }}
    output = unparse(original, xsi_types=True)
    once = parse(output, xsi_types=True)
    assert once['doc']['u'] == XsiValue('some raw text', 'z:thing')


def test_roundtrip_typed_xml_document():
    xml = (
        '<doc {} {}>'
        '<a xsi:type="xsd:boolean">1</a>'
        '<b xsi:type="xsd:decimal">3.14</b>'
        '<c xsi:type="xsd:dateTime">2026-10-02T13:45:30+08:00</c>'
        '<d xsi:type="xsd:base64Binary">aGVsbG8=</d>'
        '<e xsi:nil="true"/>'
        '<f/>'
        '</doc>').format(XSI_ATTR, XSD_ATTR)
    parsed = parse(xml, xsi_types=True)
    emitted = unparse(parsed, xsi_types=True)
    reparsed = parse(emitted, xsi_types=True)
    assert reparsed['doc']['a'] is True
    assert reparsed['doc']['b'] == Decimal('3.14')
    assert reparsed['doc']['c'] == datetime(
        2026, 10, 2, 13, 45, 30, tzinfo=timezone(timedelta(hours=8)))
    assert reparsed['doc']['d'] == b'hello'
    assert reparsed['doc']['e'] is None
    assert reparsed['doc']['f'] == ''
