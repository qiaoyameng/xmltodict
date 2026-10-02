"""Tests for the opt-in mixed-content representation.

Mixed content preserves the interleaving of text fragments and child
elements as an ordered ``content_key`` (default ``'#content'``) list.
"""
import io

import pytest
import xml.etree.ElementTree as ET

import xmltodict
from xmltodict import parse, unparse


# ---------------------------------------------------------------------------
# Semantic comparison helpers
# ---------------------------------------------------------------------------

def _events(element):
    """Yield ``('text', s)`` / ``('elem', tag)`` events in document order.

    Used to compare parsed and round-tripped XML semantically rather than by
    exact string equality.
    """
    if element.text:
        yield ('text', element.text)
    for child in element:
        yield ('elem', child.tag)
        yield from _events(child)
        if child.tail:
            yield ('text', child.tail)


def _semantic_events(xml):
    root = ET.fromstring(xml)
    return list(_events(root))


def _assert_semantically_equal(xml_a, xml_b):
    assert _semantic_events(xml_a) == _semantic_events(xml_b)


def _roundtrip(xml, **parse_kwargs):
    parsed = parse(xml, mixed_content=True, **parse_kwargs)
    emitted = unparse(parsed, full_document=False, **{
        k: v for k, v in parse_kwargs.items()
        if k in ('attr_prefix', 'cdata_key', 'content_key')
    })
    reparsed = parse(emitted, mixed_content=True, **parse_kwargs)
    return parsed, emitted, reparsed


# ---------------------------------------------------------------------------
# Parsing: representation
# ---------------------------------------------------------------------------

def test_basic_mixed_content_keeps_interleaving():
    xml = '<p>前面<b>重点</b>中间<i>补充</i>后面</p>'
    result = parse(xml, mixed_content=True)
    assert result == {
        'p': {
            '#content': [
                '前面',
                ('b', '重点'),
                '中间',
                ('i', '补充'),
                '后面',
            ],
        },
    }


def test_default_mode_unchanged():
    xml = '<p>前面<b>重点</b>中间<i>补充</i>后面</p>'
    assert parse(xml) == {
        'p': {'b': '重点', 'i': '补充', '#text': '前面中间后面'},
    }
    assert isinstance(parse(xml), dict)


def test_nested_emphasis():
    xml = '<a>lead<b>mid<c>deep</c>tail</b>end</a>'
    assert parse(xml, mixed_content=True) == {
        'a': {
            '#content': [
                'lead',
                ('b', {
                    '#content': ['mid', ('c', 'deep'), 'tail'],
                }),
                'end',
            ],
        },
    }


def test_consecutive_text_is_merged():
    xml = '<a>foo<b>x</b>bar</a>'
    result = parse(xml, mixed_content=True)
    assert result['a']['#content'][0] == 'foo'
    assert result['a']['#content'][2] == 'bar'


def test_cdata_merges_with_text():
    xml = '<a><![CDATA[foo]]>bar<b>x</b>baz<![CDATA[qux]]></a>'
    result = parse(xml, mixed_content=True)
    assert result == {
        'a': {
            '#content': ['foobar', ('b', 'x'), 'bazqux'],
        },
    }


def test_empty_child_in_mixed_content():
    assert parse('<a>x<b></b>y</a>', mixed_content=True) == {
        'a': {'#content': ['x', ('b', None), 'y']},
    }
    assert parse('<a>x<b/>y</a>', mixed_content=True) == {
        'a': {'#content': ['x', ('b', None), 'y']},
    }


def test_adjacent_same_name_children():
    assert parse('<a>x<b>1</b><b>2</b>y</a>', mixed_content=True) == {
        'a': {
            '#content': ['x', ('b', '1'), ('b', '2'), 'y'],
        },
    }


def test_attributes_live_on_element_dict():
    xml = '<p>x<b id="1" k="v">bold</b>y</p>'
    assert parse(xml, mixed_content=True) == {
        'p': {
            '#content': [
                'x',
                ('b', {'@id': '1', '@k': 'v', '#text': 'bold'}),
                'y',
            ],
        },
    }


def test_leaf_forms_stay_classic():
    assert parse('<a/>', mixed_content=True) == {'a': None}
    assert parse('<a>text</a>', mixed_content=True) == {'a': 'text'}
    assert parse('<a x="1">text</a>', mixed_content=True) == {
        'a': {'@x': '1', '#text': 'text'},
    }
    assert parse('<a x="1"/>', mixed_content=True) == {'a': {'@x': '1'}}


def test_element_only_content_keeps_sibling_order():
    # Child order must be preserved even without interleaved text, since a
    # grouped dict cannot express the order of differently named siblings.
    assert parse('<a><b>1</b><c>2</c></a>', mixed_content=True) == {
        'a': {'#content': [('b', '1'), ('c', '2')]},
    }
    assert parse('<a><c>1</c><b>2</b></a>', mixed_content=True) == {
        'a': {'#content': [('c', '1'), ('b', '2')]},
    }
    assert parse('<a><b>1</b><b>2</b></a>', mixed_content=True) == {
        'a': {'#content': [('b', '1'), ('b', '2')]},
    }


def test_whitespace_only_fragments_follow_strip_whitespace():
    xml = '<root>\n  <a>1</a>\n  text\n  <b>2</b>\n</root>'
    stripped = parse(xml, mixed_content=True)
    assert stripped == {
        'root': {
            '#content': [('a', '1'), '\n  text\n  ', ('b', '2')],
        },
    }
    kept = parse(xml, mixed_content=True, strip_whitespace=False)
    assert kept == {
        'root': {
            '#content': [
                '\n  ', ('a', '1'), '\n  text\n  ', ('b', '2'), '\n',
            ],
        },
    }


def test_significant_inner_whitespace_never_stripped():
    # These text fragments contain non-whitespace characters: the surrounding
    # spaces must survive even with strip_whitespace left at its default.
    xml = '<p>x <b>y</b> z </p>'
    result = parse(xml, mixed_content=True)
    assert result['p']['#content'] == ['x ', ('b', 'y'), ' z ']


def test_empty_element():
    assert parse('<a></a>', mixed_content=True) == {'a': None}
    assert parse('<a/>', mixed_content=True) == {'a': None}


def test_namespace_expansion():
    xml = (
        '<root xmlns="http://defaultns.com/" xmlns:a="http://a.com/">'
        '<x>1</x>lead<a:y href="z">2</a:y>tail</root>'
    )
    result = parse(xml, process_namespaces=True, mixed_content=True)
    assert result == {
        'http://defaultns.com/:root': {
            '@xmlns': {
                '': 'http://defaultns.com/',
                'a': 'http://a.com/',
            },
            '#content': [
                ('http://defaultns.com/:x', '1'),
                'lead',
                ('http://a.com/:y', {
                    '@href': 'z',
                    '#text': '2',
                }),
                'tail',
            ],
        },
    }


def test_custom_keys():
    xml = '<p>x<b>bold</b>y</p>'
    result = parse(
        xml, mixed_content=True,
        attr_prefix='!', cdata_key='$', content_key='#mixed',
    )
    assert result == {
        'p': {'#mixed': ['x', ('b', 'bold'), 'y']},
    }


def test_content_key_conflicts_raise():
    with pytest.raises(ValueError):
        parse('<a/>', mixed_content=True, content_key='#text')
    with pytest.raises(ValueError):
        parse('<a/>', mixed_content=True, content_key='@bad')
    with pytest.raises(ValueError):
        parse('<a/>', mixed_content=True, content_key='')


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def test_streaming_mixed_items():
    items = []
    parse(
        '<r><item>a<b>x</b>b</item><item>c<i>y</i>d</item></r>',
        item_depth=2,
        mixed_content=True,
        item_callback=lambda path, item: items.append(item) or True,
    )
    assert items == [
        {'#content': ['a', ('b', 'x'), 'b']},
        {'#content': ['c', ('i', 'y'), 'd']},
    ]


# ---------------------------------------------------------------------------
# Roundtripping (semantic)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("xml", [
    '<p>前面<b>重点</b>中间<i>补充</i>后面</p>',
    '<a>lead<b>mid<c>deep</c>tail</b>end</a>',
    '<a>x<b></b>y</a>',
    '<a>x<b>1</b><b>2</b>y</a>',
    '<p>a<b>b</b><b>c</b>d</p>',
    '<p>x <b>y</b> z</p>',
    '<a><![CDATA[foo]]>bar<b>x</b>baz</a>',
    '<p>x<b id="1" k="v">bold</b>y</p>',
])
def test_roundtrip_semantic(xml):
    parsed, emitted, reparsed = _roundtrip(xml)
    assert reparsed == parsed
    _assert_semantically_equal(xml, emitted)


def test_roundtrip_preserves_whitespace_when_kept():
    xml = '<root>\n  <a>1</a>\n  text\n  <b>2</b>\n</root>'
    parsed, emitted, reparsed = _roundtrip(xml, strip_whitespace=False)
    assert reparsed == parsed
    _assert_semantically_equal(xml, emitted)


def test_roundtrip_namespaces():
    xml = (
        '<root xmlns="http://defaultns.com/" xmlns:a="http://a.com/">'
        '<x>1</x>lead<a:y href="z">2</a:y>tail</root>'
    )
    namespaces = {'http://defaultns.com/': '', 'http://a.com/': 'a'}
    parsed = parse(xml, process_namespaces=True, mixed_content=True)
    emitted = unparse(
        parsed, full_document=False, namespaces=namespaces,
    )
    reparsed = parse(emitted, process_namespaces=True, mixed_content=True)
    assert reparsed == parsed
    _assert_semantically_equal(xml, emitted)


def test_roundtrip_custom_keys():
    xml = '<p>x<b>bold</b>y</p>'
    parsed, emitted, reparsed = _roundtrip(
        xml, attr_prefix='!', cdata_key='$', content_key='#mixed',
    )
    assert reparsed == parsed
    _assert_semantically_equal(xml, emitted)


def test_roundtrip_file_like_input():
    xml = '<p>a<b>x</b>c</p>'
    parsed = parse(io.BytesIO(xml.encode()), mixed_content=True)
    buffer = io.StringIO()
    unparse(parsed, buffer)
    reparsed = parse(buffer.getvalue(), mixed_content=True)
    assert reparsed == parsed


def test_short_empty_elements():
    parsed = parse('<p>a<b></b>c</p>', mixed_content=True)
    emitted = unparse(
        parsed, full_document=False, short_empty_elements=True,
    )
    assert emitted == '<p>a<b/>c</p>'


# ---------------------------------------------------------------------------
# unparse: hand-built mixed representations
# ---------------------------------------------------------------------------

def test_unparse_accepts_tuple_and_string_items():
    obj = {'p': {'#content': ['x', ('b', 'bold'), 'y']}}
    assert unparse(obj, full_document=False) == '<p>x<b>bold</b>y</p>'


def test_unparse_accepts_single_key_dict_items():
    obj = {'p': {'#content': ['x', {'b': 'bold'}, 'y']}}
    assert unparse(obj, full_document=False) == '<p>x<b>bold</b>y</p>'


def test_unparse_stringifies_scalar_items():
    obj = {'p': {'#content': ['n=', 1, ('b', True)]}}
    assert unparse(obj, full_document=False) == '<p>n=1<b>true</b></p>'


def test_unparse_rejects_invalid_items():
    bad_docs = [
        {'a': {'#content': [None]}},
        {'a': {'#content': [('tag',)]}},
        {'a': {'#content': 123}},
        {'a': {'#content': [['x']]}},
        {'a': {'#content': ['x'], 'b': 1}},
        {'a': {'#content': ['x'], '#text': 'y'}},
    ]
    for doc in bad_docs:
        with pytest.raises(ValueError):
            unparse(doc, full_document=False)


def test_unparse_content_key_conflicts_raise():
    with pytest.raises(ValueError):
        unparse({'a': {'#content': []}}, content_key='#text',
                full_document=False)
    with pytest.raises(ValueError):
        unparse({'a': {'#content': []}}, content_key='@bad',
                full_document=False)


def test_streaming_root_element():
    items = []
    parse(
        '<root>a<b>x</b>b</root>',
        item_depth=1,
        mixed_content=True,
        item_callback=lambda path, item: items.append(item) or True,
    )
    assert items == [{'#content': ['a', ('b', 'x'), 'b']}]


def test_streaming_leaf_items_keep_classic_forms():
    items = []
    parse(
        '<r><item id="1"/><item>y</item></r>',
        item_depth=2,
        mixed_content=True,
        item_callback=lambda path, item: items.append(item) or True,
    )
    assert items == [{'@id': '1'}, 'y']


def test_comments_allowed_alongside_content():
    xml = '<p>x<!-- c --><b>z</b>y</p>'
    parsed = parse(
        xml, mixed_content=True, process_comments=True,
    )
    assert parsed == {
        'p': {'#comment': 'c',
              '#content': ['x', ('b', 'z'), 'y']},
    }
    emitted = unparse(parsed, full_document=False)
    _assert_semantically_equal(xml, emitted)


def test_postprocessor_on_child_in_mixed_content():
    def postprocessor(path, key, value):
        if key == 'b' and isinstance(value, str) and value.isdigit():
            return key, int(value)
        return key, value

    assert parse(
        '<a>x<b>1</b>y</a>',
        mixed_content=True,
        postprocessor=postprocessor,
    ) == {'a': {'#content': ['x', ('b', 1), 'y']}}


def test_postprocessor_can_drop_child():
    def postprocessor(path, key, value):
        return None if key == 'drop' else (key, value)

    xml = '<a>x<drop>gone</drop><keep>z</keep>y</a>'
    parsed = parse(xml, mixed_content=True, postprocessor=postprocessor)
    assert parsed == {'a': {'#content': ['x', ('keep', 'z'), 'y']}}


def test_unparse_nested_mixed_and_multiple_roots():
    obj = {'a': {'#content': [('b', '1'), 'x']}, 'c': None}
    assert unparse(obj, full_document=False) == '<a><b>1</b>x</a><c></c>'


def test_dict_constructor_is_honored():
    from collections import OrderedDict
    xml = '<p>x<b>y</b>z</p>'
    result = parse(
        xml, mixed_content=True, dict_constructor=OrderedDict,
    )
    assert isinstance(result, OrderedDict)
    inner = result['p']
    assert isinstance(inner, OrderedDict)
    assert list(inner.keys()) == ['#content']


def test_entities_decode_and_re_escape_semantically():
    xml = '<p>a &amp; b<b>x &lt; y</b>&#65;</p>'
    parsed, emitted, reparsed = _roundtrip(xml)
    assert parsed == {'p': {'#content': ['a & b', ('b', 'x < y'), 'A']}}
    assert '&amp;' in emitted and '&lt;' in emitted
    _assert_semantically_equal(xml, emitted)


def test_bytes_text_items_are_decoded():
    obj = {'a': {'#content': [b'x', ('b', b'y')]}}
    assert unparse(obj, full_document=False) == '<a>x<b>y</b></a>'


def test_empty_content_list_is_empty_element():
    obj = {'a': {'#content': []}}
    assert unparse(obj, full_document=False) == '<a></a>'
    assert parse(unparse(obj), mixed_content=True) == {'a': None}
