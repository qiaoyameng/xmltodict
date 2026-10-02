"""Tests for the opt-in mixed content mode (parse/unparse)."""

import pytest

from xmltodict import parse, unparse, ParsingInterrupted


def _roundtrip(xml, parse_kwargs=None, unparse_kwargs=None):
    """Parse in mixed mode, unparse, re-parse and compare semantically.

    Equality of the resulting dicts compares text and element order (the
    '#children' lists are order-sensitive) without relying on exact XML
    string equality.
    """
    parse_kwargs = parse_kwargs or {}
    unparse_kwargs = unparse_kwargs or {}
    doc = parse(xml, mixed_content=True, **parse_kwargs)
    xml_again = unparse(doc, mixed_content=True, **unparse_kwargs)
    doc_again = parse(xml_again, mixed_content=True, **parse_kwargs)
    assert doc == doc_again
    return doc


class TestMixedContentParse:
    def test_text_and_children_order(self):
        doc = parse('<p>前面<b>重点</b>中间<i>补充</i>后面</p>',
                    mixed_content=True)
        assert doc == {
            'p': {
                '#children': [
                    '前面',
                    {'b': {'#children': ['重点']}},
                    '中间',
                    {'i': {'#children': ['补充']}},
                    '后面',
                ]
            }
        }

    def test_attributes_and_text(self):
        doc = parse('<p class="intro" id="x">hi<b>y</b></p>',
                    mixed_content=True)
        assert doc == {
            'p': {
                '@class': 'intro',
                '@id': 'x',
                '#children': ['hi', {'b': {'#children': ['y']}}],
            }
        }

    def test_nested_emphasis(self):
        doc = _roundtrip('<p>a<b>x<i>y</i>z</b>c</p>')
        assert doc == {
            'p': {
                '#children': [
                    'a',
                    {'b': {'#children': [
                        'x', {'i': {'#children': ['y']}}, 'z']}},
                    'c',
                ]
            }
        }

    def test_consecutive_text_and_cdata_merged(self):
        doc = parse('<p>a<![CDATA[b]]>c&amp;d</p>', mixed_content=True)
        assert doc == {'p': {'#children': ['abc&d']}}

    def test_empty_child_element(self):
        doc = _roundtrip('<p>a<br/>b</p>')
        assert doc == {'p': {'#children': ['a', {'br': {}}, 'b']}}

    def test_empty_element_forms_equal(self):
        assert (parse('<p><br/></p>', mixed_content=True)
                == parse('<p><br></br></p>', mixed_content=True)
                == {'p': {'#children': [{'br': {}}]}})

    def test_adjacent_same_name_children(self):
        doc = _roundtrip('<ul><li>1</li><li>2</li><li>3</li></ul>')
        assert doc == {
            'ul': {
                '#children': [
                    {'li': {'#children': ['1']}},
                    {'li': {'#children': ['2']}},
                    {'li': {'#children': ['3']}},
                ]
            }
        }

    def test_whitespace_not_stripped(self):
        # strip_whitespace defaults to True but must not eat mixed content
        doc = parse('<p> <b>x</b> </p>', mixed_content=True)
        assert doc == {
            'p': {'#children': [' ', {'b': {'#children': ['x']}}, ' ']}
        }

    def test_whitespace_roundtrip(self):
        _roundtrip('<p> a <b>x</b>\n</p>')

    def test_entity_references_merged_into_text(self):
        doc = _roundtrip('<p>a &amp; b</p>')
        assert doc == {'p': {'#children': ['a & b']}}

    def test_namespaces(self):
        xml = '<p xmlns:n="urn:x"><n:b>t</n:b></p>'
        doc = _roundtrip(
            xml,
            parse_kwargs={'process_namespaces': True},
            unparse_kwargs={'namespaces': {'urn:x': 'n'}},
        )
        assert doc == {
            'p': {
                '@xmlns': {'n': 'urn:x'},
                '#children': [{'urn:x:b': {'#children': ['t']}}],
            }
        }

    def test_namespaces_collapsed_prefix(self):
        doc = parse('<p xmlns:n="urn:x"><n:b>t</n:b></p>',
                    mixed_content=True, process_namespaces=True,
                    namespaces={'urn:x': 'n'})
        assert doc == {
            'p': {
                '@xmlns': {'n': 'urn:x'},
                '#children': [{'n:b': {'#children': ['t']}}],
            }
        }

    def test_comments(self):
        doc = _roundtrip('<p>a<!-- note -->b</p>',
                         parse_kwargs={'process_comments': True})
        assert doc == {
            'p': {'#children': ['a', {'#comment': 'note'}, 'b']}
        }

    def test_streaming_item_depth(self):
        items = []

        def callback(path, item):
            items.append((list(path), item))
            return True

        result = parse('<root><i>a<b>1</b>c</i><i>d</i></root>',
                       mixed_content=True, item_depth=2,
                       item_callback=callback)
        assert result is None
        assert items == [
            ([('root', None), ('i', None)],
             {'i': {'#children': [
                 'a', {'b': {'#children': ['1']}}, 'c']}}),
            ([('root', None), ('i', None)],
             {'i': {'#children': ['d']}}),
        ]

    def test_streaming_interrupted(self):
        def callback(path, item):
            return False

        with pytest.raises(ParsingInterrupted):
            parse('<root><i>a</i><i>b</i></root>', mixed_content=True,
                  item_depth=2, item_callback=callback)

    def test_default_mode_unchanged(self):
        xml = '<p>前面<b>重点</b>中间</p>'
        doc = parse(xml)
        assert doc == {'p': {'#text': '前面中间', 'b': '重点'}}
        assert isinstance(doc['p']['b'], str)


class TestMixedContentOptionConflicts:
    @pytest.mark.parametrize('kwargs', [
        {'cdata_key': '#children'},
        {'comment_key': '#children'},
        {'attr_prefix': '#'},
        {'attr_prefix': ''},
        {'postprocessor': lambda path, key, value: (key, value)},
        {'force_list': True},
        {'force_list': ('b',)},
        {'force_cdata': True},
    ])
    def test_parse_rejects_conflicting_options(self, kwargs):
        with pytest.raises(ValueError):
            parse('<p>a</p>', mixed_content=True, **kwargs)

    @pytest.mark.parametrize('kwargs', [
        {'cdata_key': '#children'},
        {'comment_key': '#children'},
        {'attr_prefix': '#'},
        {'attr_prefix': ''},
        {'pretty': True},
        {'preprocessor': lambda key, value: (key, value)},
        {'expand_iter': 'item'},
    ])
    def test_unparse_rejects_conflicting_options(self, kwargs):
        with pytest.raises(ValueError):
            unparse({'p': {}}, mixed_content=True, **kwargs)

    def test_custom_keys_still_work(self):
        doc = parse('<p a="1">x</p>', mixed_content=True,
                    attr_prefix='$', cdata_key='#text')
        assert doc == {'p': {'$a': '1', '#children': ['x']}}
        xml = unparse(doc, mixed_content=True, attr_prefix='$')
        assert parse(xml, mixed_content=True, attr_prefix='$') == doc


class TestMixedContentUnparse:
    def test_basic_unparse(self):
        doc = {'p': {'#children': [
            '前面', {'b': {'#children': ['重点']}}, '后面']}}
        xml = unparse(doc, mixed_content=True)
        assert parse(xml, mixed_content=True) == doc

    def test_attributes(self):
        doc = {'p': {'@class': 'x', '#children': ['hi']}}
        xml = unparse(doc, mixed_content=True)
        assert parse(xml, mixed_content=True) == doc

    def test_empty_element(self):
        for value in ({}, {'#children': []}, None):
            assert parse(unparse({'br': value}, mixed_content=True),
                         mixed_content=True) == {'br': {}}

    def test_short_empty_elements(self):
        xml = unparse({'br': {}}, mixed_content=True,
                      short_empty_elements=True)
        assert '<br/>' in xml

    def test_text_escaping(self):
        doc = {'p': {'#children': ['a < b & "c"']}}
        xml = unparse(doc, mixed_content=True)
        assert parse(xml, mixed_content=True) == doc

    def test_non_string_text_values(self):
        xml = unparse({'p': {'#children': [1, True]}}, mixed_content=True)
        assert parse(xml, mixed_content=True) == {
            'p': {'#children': ['1true']}}

    def test_top_level_comment(self):
        xml = unparse({'#comment': 'hi', 'p': {}}, mixed_content=True)
        assert '<!--hi-->' in xml

    def test_comment_in_children(self):
        doc = {'p': {'#children': ['a', {'#comment': 'note'}, 'b']}}
        xml = unparse(doc, mixed_content=True)
        assert parse(xml, mixed_content=True, process_comments=True) == doc

    def test_full_document_false(self):
        xml = unparse({'p': {'#children': ['a']}}, mixed_content=True,
                      full_document=False)
        assert xml == '<p>a</p>'

    def test_element_value_must_be_dict(self):
        with pytest.raises(ValueError):
            unparse({'p': 'text'}, mixed_content=True)

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError):
            unparse({'p': {'b': {'#children': []}}}, mixed_content=True)

    def test_children_must_be_list(self):
        with pytest.raises(ValueError):
            unparse({'p': {'#children': 'text'}}, mixed_content=True)

    def test_child_element_must_be_single_key_dict(self):
        with pytest.raises(ValueError):
            unparse({'p': {'#children': [{'a': {}, 'b': {}}]}},
                    mixed_content=True)

    def test_none_text_node_rejected(self):
        with pytest.raises(ValueError):
            unparse({'p': {'#children': [None]}}, mixed_content=True)

    def test_list_text_node_rejected(self):
        with pytest.raises(ValueError):
            unparse({'p': {'#children': [['a']]}}, mixed_content=True)

    def test_multiple_roots_rejected(self):
        with pytest.raises(ValueError):
            unparse({'a': {}, 'b': {}}, mixed_content=True)

    def test_missing_root_rejected(self):
        with pytest.raises(ValueError):
            unparse({}, mixed_content=True)

    def test_default_unparse_unchanged(self):
        doc = {'p': {'@a': '1', '#text': 'x', 'b': ['1', '2']}}
        assert parse(unparse(doc)) == doc
