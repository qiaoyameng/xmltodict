"""Tests for the optional namespace-context preservation mode.

parse(..., process_namespaces=True, preserve_namespace_context=True) keeps
expanded names (``URI:local``) together with the per-element xmlns
declarations, and unparse(..., preserve_namespace_context=True) restores
legal namespace scoping for nested rebinding, undeclaration and newly added
nodes.

The tests compare against expanded names produced by the standard
``xml.etree.ElementTree`` parser: attribute order and the literal spelling of
prefixes are allowed to differ, but the namespace URI an element or attribute
belongs to must be preserved.
"""

import xml.etree.ElementTree as ET

import pytest

import xmltodict


NESTED_REBIND_XML = """
<root xmlns="urn:default" xmlns:a="urn:a">
  <a:child xmlns:a="urn:b">
    <a:grand plain="1" a:attr="2">orig</a:grand>
  </a:child>
  <undeclare xmlns="">
    <deep y="z">
      <leaf/>
    </deep>
  </undeclare>
  <multi xmlns:a="urn:c" xmlns:a2="urn:a">
    <a:z>3</a:z>
    <a2:w>4</a2:w>
  </multi>
</root>
"""


def _expanded_structure(xml_text):
    """Return a nested (tag, sorted attrs, children) structure via ET."""

    def build(element):
        return (
            element.tag,
            tuple(sorted(element.attrib.items())),
            tuple(build(child) for child in element),
        )

    return build(ET.fromstring(xml_text))


def test_parse_preserves_per_layer_declarations():
    result = xmltodict.parse(
        NESTED_REBIND_XML,
        process_namespaces=True,
        preserve_namespace_context=True,
    )
    root = result['urn:default:root']
    assert root['@xmlns'] == {'': 'urn:default', 'a': 'urn:a'}
    # prefix 'a' is rebound at the child layer to a different URI
    assert root['urn:b:child']['@xmlns'] == {'a': 'urn:b'}
    # default namespace undeclaration is recorded per element
    assert root['undeclare']['@xmlns'] == {'': None}
    # the same URI bound under two prefixes on one element
    assert root['urn:default:multi']['@xmlns'] == {
        'a': 'urn:c', 'a2': 'urn:a'}


def test_roundtrip_expanded_names_match_standard_parser():
    parsed = xmltodict.parse(
        NESTED_REBIND_XML,
        process_namespaces=True,
        preserve_namespace_context=True,
    )
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    assert _expanded_structure(output) == _expanded_structure(NESTED_REBIND_XML)


def test_modify_namespaced_node_then_serialize():
    parsed = xmltodict.parse(
        NESTED_REBIND_XML,
        process_namespaces=True,
        preserve_namespace_context=True,
    )
    grand = parsed['urn:default:root']['urn:b:child']['urn:b:grand']
    grand['#text'] = 'changed'
    grand['@urn:b:newattr'] = 'v'

    output = xmltodict.unparse(parsed, preserve_namespace_context=True)

    root = ET.fromstring(output)
    grand_el = root.find('.//{urn:b}grand')
    assert grand_el is not None
    assert grand_el.text == 'changed'
    assert grand_el.attrib['plain'] == '1'
    assert grand_el.attrib['{urn:b}attr'] == '2'
    assert grand_el.attrib['{urn:b}newattr'] == 'v'
    # unprefixed attribute must not inherit the default namespace
    assert 'plain' in grand_el.attrib
    assert '{urn:default}plain' not in grand_el.attrib


def test_unprefixed_attribute_never_inherits_default_namespace():
    xml = '<r xmlns="urn:d" xmlns:p="urn:p"><c local="1" p:other="2"/></r>'
    parsed = xmltodict.parse(
        xml, process_namespaces=True, preserve_namespace_context=True)
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    c = ET.fromstring(output).find('{urn:d}c')
    assert c is not None
    assert c.attrib == {'local': '1', '{urn:p}other': '2'}


def test_new_namespaced_node_gets_non_clashing_declaration():
    parsed = xmltodict.parse(
        NESTED_REBIND_XML,
        process_namespaces=True,
        preserve_namespace_context=True,
    )
    root_dict = parsed['urn:default:root']
    # brand new node with an unknown URI and a namespaced attribute
    root_dict['urn:brandnew:added'] = {
        '@plain': 'p',
        '@urn:extra:qa': 'q',
        '#text': 'new',
    }

    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    root = ET.fromstring(output)

    added = root.find('{urn:brandnew}added')
    assert added is not None
    assert added.text == 'new'
    assert added.attrib['plain'] == 'p'
    assert added.attrib['{urn:extra}qa'] == 'q'
    # the new URI must be declared somewhere in the output
    used_prefixes = set()
    import io
    for event, elem in ET.iterparse(io.StringIO(output), events=('start-ns',)):
        used_prefixes.add(elem)
    assert any(uri == 'urn:brandnew' for _prefix, uri in used_prefixes)


def test_inner_prefix_rebind_does_not_leak_between_siblings():
    xml = """
    <root xmlns:p="urn:one">
      <s1 xmlns:p="urn:one"><p:e>1</p:e></s1>
      <s2 xmlns:p="urn:two"><p:e>2</p:e></s2>
    </root>
    """
    parsed = xmltodict.parse(
        xml, process_namespaces=True, preserve_namespace_context=True)
    # insert a node needing a fresh binding in s1 only
    parsed['root']['s1']['urn:new:added'] = 'x'
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)

    tags = {el.tag for el in ET.fromstring(output).iter()}
    assert {'root', 's1', 's2',
            '{urn:one}e', '{urn:two}e', '{urn:new}added'} <= tags
    # the fresh binding in s1 must not shadow the rebound p in s2
    s1, s2 = ET.fromstring(output)
    assert s1[0].tag == '{urn:one}e'
    assert s2[0].tag == '{urn:two}e'


def test_default_namespace_undeclaration_roundtrip():
    xml = '<r xmlns="urn:d"><u xmlns=""><k/></u></r>'
    parsed = xmltodict.parse(
        xml, process_namespaces=True, preserve_namespace_context=True)
    assert parsed['urn:d:r']['u']['@xmlns'] == {'': None}
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    root = ET.fromstring(output)
    assert root.tag == '{urn:d}r'
    u = root.find('u')
    assert u is not None
    assert u.find('k') is not None


def test_new_no_namespace_element_under_default_gets_xmlns_empty():
    parsed = {
        'urn:d:r': {
            '@xmlns': {'': 'urn:d'},
            'urn:d:keep': '1',
            'plain': '2',
        }
    }
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    root = ET.fromstring(output)
    assert root.find('{urn:d}keep') is not None
    plain = root.find('plain')
    assert plain is not None
    assert plain.text == '2'


def test_multiple_prefixes_for_same_uri_are_reused():
    xml = '<r xmlns:a="urn:x" xmlns:b="urn:x"><a:e/><b:f/></r>'
    parsed = xmltodict.parse(
        xml, process_namespaces=True, preserve_namespace_context=True)
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    tags = {el.tag for el in ET.fromstring(output).iter()}
    assert tags == {'r', '{urn:x}e', '{urn:x}f'}
    # both preserved prefixes remain declared; output is still legal
    assert 'xmlns:a="urn:x"' in output
    assert 'xmlns:b="urn:x"' in output


def test_synthesized_prefix_avoids_preserved_declaration():
    parsed = {
        'urn:foo:r': {
            'urn:foo:c': {
                '@xmlns': {'ns0': 'urn:other'},
                'urn:bar:x': '1',
            },
        },
    }
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    assert 'xmlns:ns0="urn:other"' in output
    assert 'xmlns:ns1="urn:bar"' in output
    x = ET.fromstring(output).find('.//{urn:bar}x')
    assert x is not None
    assert x.text == '1'


def test_uri_containing_separator_is_not_naively_split():
    xml = '<r xmlns:a="urn:weird:uri/with:colon"><a:e>x</a:e></r>'
    parsed = xmltodict.parse(
        xml, process_namespaces=True, preserve_namespace_context=True)
    key = 'urn:weird:uri/with:colon:e'
    assert key in parsed['r']
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    child = ET.fromstring(output)[0]
    assert child.tag == '{urn:weird:uri/with:colon}e'


def test_custom_namespace_separator_roundtrip():
    xml = '<r xmlns:a="urn:a"><a:e a:q="1"/></r>'
    parsed = xmltodict.parse(
        xml,
        process_namespaces=True,
        namespace_separator='|',
        preserve_namespace_context=True,
    )
    assert 'urn:a|e' in parsed['r']
    output = xmltodict.unparse(
        parsed,
        preserve_namespace_context=True,
        namespace_separator='|',
    )
    element = ET.fromstring(output)[0]
    assert element.tag == '{urn:a}e'
    assert element.attrib == {'{urn:a}q': '1'}


def test_legacy_namespaces_parameter_still_supported():
    obj = {
        "http://defaultns.com/:root": {
            "@xmlns": {
                "": "http://defaultns.com/",
                "a": "http://a.com/",
            },
            "http://defaultns.com/:x": {
                "@http://a.com/:attr": "val",
                "#text": "1",
            },
        },
    }
    output = xmltodict.unparse(
        obj,
        namespaces={
            'http://defaultns.com/': '',
            'http://a.com/': 'a',
        },
    )
    root = ET.fromstring(output)
    assert root.tag == '{http://defaultns.com/}root'
    x = root.find('{http://defaultns.com/}x')
    assert x.attrib == {'{http://a.com/}attr': 'val'}


def test_namespaces_hint_used_as_preferred_prefix_in_context_mode():
    parsed = {
        'urn:a:r': {
            'urn:a:x': None,
            'urn:b:y': None,
        },
    }
    output = xmltodict.unparse(
        parsed,
        preserve_namespace_context=True,
        namespaces={'urn:a': 'aa', 'urn:b': 'bb'},
    )
    root = ET.fromstring(output)
    assert root.tag == '{urn:a}r'
    assert root.find('{urn:a}x') is not None
    assert root.find('{urn:b}y') is not None
    assert 'xmlns:aa="urn:a"' in output
    assert 'xmlns:bb="urn:b"' in output


def test_preserve_context_rejects_collapse_map_on_parse():
    with pytest.raises(ValueError):
        xmltodict.parse(
            '<a xmlns="urn:a"/>',
            process_namespaces=True,
            preserve_namespace_context=True,
            namespaces={'urn:a': 'p'},
        )


def test_preserve_context_keeps_xmlns_without_xml_attribs():
    xml = '<r xmlns:a="urn:a"><a:e>x</a:e></r>'
    parsed = xmltodict.parse(
        xml,
        process_namespaces=True,
        xml_attribs=False,
        preserve_namespace_context=True,
    )
    assert parsed['r']['@xmlns'] == {'a': 'urn:a'}
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    assert ET.fromstring(output)[0].tag == '{urn:a}e'


def test_attribute_with_default_namespace_uri_requires_prefix():
    parsed = {
        'urn:d:r': {
            '@xmlns': {'': 'urn:d'},
            '@urn:d:attr': 'v',
            'urn:d:c': '1',
        },
    }
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    root = ET.fromstring(output)
    assert root.attrib == {'{urn:d}attr': 'v'}


def test_full_roundtrip_with_modification_via_etree_equivalence():
    parsed = xmltodict.parse(
        NESTED_REBIND_XML,
        process_namespaces=True,
        preserve_namespace_context=True,
    )
    multi = parsed['urn:default:root']['urn:default:multi']
    multi['urn:c:z'] = 'changed-3'
    multi['urn:a:new'] = {'@urn:b:deep': 'd', '#text': 'fresh'}

    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    root = ET.fromstring(output)
    multi_el = root.find('{urn:default}multi')
    assert multi_el.find('{urn:c}z').text == 'changed-3'
    new_el = multi_el.find('{urn:a}new')
    assert new_el.text == 'fresh'
    assert new_el.attrib == {'{urn:b}deep': 'd'}
    # undeclared subtree still in no namespace
    undeclare = root.find('undeclare')
    assert undeclare is not None
    assert undeclare.find('deep').find('leaf') is not None


def test_prefixed_namespace_undeclaration_is_not_emitted():
    # XML 1.0 forbids xmlns:p="" for a non-default prefix. The scope must
    # simply forget the binding and synthesize a fresh prefix where needed.
    parsed = {
        'r': {
            '@xmlns': {'a': 'urn:bytes', 'b': None},
            'urn:bytes:e': None,
        },
    }
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    assert 'xmlns:b=""' not in output
    root = ET.fromstring(output)
    assert root.find('{urn:bytes}e') is not None


def test_prefix_rebound_after_undeclaration():
    parsed = {
        'r': {
            '@xmlns': {'a': 'urn:one', 'b': None},
            'urn:one:a1': None,
            'urn:two:c': {'@xmlns': {'b': 'urn:two'}},
        },
    }
    output = xmltodict.unparse(parsed, preserve_namespace_context=True)
    root = ET.fromstring(output)
    assert root.find('{urn:one}a1') is not None
    assert root.find('{urn:two}c') is not None
