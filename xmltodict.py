#!/usr/bin/env python
"Makes working with XML feel like you are working with JSON"

import base64
import binascii
import re
from xml.parsers import expat
from xml.sax.saxutils import XMLGenerator, escape
from xml.sax.xmlreader import AttributesImpl
from datetime import datetime
from decimal import Decimal
from io import StringIO
from inspect import isgenerator
import codecs


XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XSD_NS = "http://www.w3.org/2001/XMLSchema"


class XsiValue:
    """Scalar value explicitly tagged with an ``xsi:type``.

    Instances are produced by :func:`parse` for unknown types when
    ``xsi_types=True`` and can be passed to :func:`unparse` to force an
    explicit ``xsi:type`` marker. ``type_name`` is the QName as it appears in
    the XML document (for example ``xsd:decimal`` or a bare built-in name).
    """

    __slots__ = ("value", "type_name")

    def __init__(self, value, type_name):
        self.value = value
        self.type_name = type_name

    def __repr__(self):
        return "XsiValue({!r}, {!r})".format(self.value, self.type_name)

    def __eq__(self, other):
        if not isinstance(other, XsiValue):
            return NotImplemented
        return self.value == other.value and self.type_name == other.type_name

    def __hash__(self):
        return hash((self.value, self.type_name))


# ---------------------------------------------------------------------------
# XML Schema (XSD) lexical conversions
# ---------------------------------------------------------------------------

_BOOLEAN_TRUE = frozenset(("true", "1"))
_BOOLEAN_FALSE = frozenset(("false", "0"))
_INTEGER_RE = re.compile(r"[+-]?[0-9]+$")
_DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)$")
_DATE_TIME_RE = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,9})?"
    r"(?:Z|[+-](?:(?:0[0-9]|1[0-3]):[0-5][0-9]|14:00))?$"
)
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}$")


def _collapse_whitespace(text):
    return re.sub(r"[\t\n\r ]+", " ", text).strip()


def _xsd_string(text):
    return text


def _xsd_boolean(text):
    token = _collapse_whitespace(text)
    if token in _BOOLEAN_TRUE:
        return True
    if token in _BOOLEAN_FALSE:
        return False
    raise ValueError("invalid xsd:boolean lexical value: {!r}".format(text))


def _xsd_integer(text):
    token = text.strip()
    if not _INTEGER_RE.match(token):
        raise ValueError("invalid xsd:integer lexical value: {!r}".format(text))
    return int(token)


def _xsd_decimal(text):
    token = text.strip()
    if not _DECIMAL_RE.match(token):
        raise ValueError("invalid xsd:decimal lexical value: {!r}".format(text))
    return Decimal(token)


def _xsd_date_time(text):
    token = text.strip()
    if not _DATE_TIME_RE.match(token):
        raise ValueError("invalid xsd:dateTime lexical value: {!r}".format(text))
    if token.endswith("Z"):
        iso_text = token[:-1] + "+00:00"
    else:
        iso_text = token
    try:
        return datetime.fromisoformat(iso_text)
    except ValueError as exc:
        raise ValueError(
            "invalid xsd:dateTime lexical value: {!r}".format(text)
        ) from exc


def _xsd_base64_binary(text):
    # XSD base64Binary allows tabs, line feeds, carriage returns and spaces,
    # which are ignored before decoding.
    token = re.sub(r"[\t\n\r ]", "", text)
    if not token:
        return b""
    if len(token) % 4 != 0 or not _BASE64_RE.match(token):
        raise ValueError(
            "invalid xsd:base64Binary lexical value: {!r}".format(text)
        )
    try:
        return base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "invalid xsd:base64Binary lexical value: {!r}".format(text)
        ) from exc


_XSD_CONVERTERS = {
    "string": _xsd_string,
    "boolean": _xsd_boolean,
    "integer": _xsd_integer,
    "decimal": _xsd_decimal,
    "dateTime": _xsd_date_time,
    "base64Binary": _xsd_base64_binary,
}


def _serialize_native(value):
    """Return ``(lexical_text, xsd_type_name)`` for supported Python values."""
    if isinstance(value, bool):
        return ("true" if value else "false"), "boolean"
    if isinstance(value, int):
        return str(value), "integer"
    if isinstance(value, Decimal):
        return str(value), "decimal"
    if isinstance(value, datetime):
        return value.isoformat(), "dateTime"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return (
            base64.b64encode(bytes(value)).decode("ascii"),
            "base64Binary",
        )
    return None


class ParsingInterrupted(Exception):
    pass


class _DictSAXHandler:
    def __init__(
        self,
        item_depth=0,
        item_callback=lambda *args: True,
        xml_attribs=True,
        attr_prefix="@",
        cdata_key="#text",
        force_cdata=False,
        cdata_separator="",
        postprocessor=None,
        dict_constructor=dict,
        strip_whitespace=True,
        namespace_separator=":",
        namespaces=None,
        force_list=None,
        comment_key="#comment",
        xsi_types=False,
        xsi_unknown_types="keep",
    ):
        self.path = []
        self.stack = []
        self.data = []
        self.item = None
        self.item_depth = item_depth
        self.xml_attribs = xml_attribs
        self.item_callback = item_callback
        self.attr_prefix = attr_prefix
        self.cdata_key = cdata_key
        self.force_cdata = force_cdata
        self.cdata_separator = cdata_separator
        self.postprocessor = postprocessor
        self.dict_constructor = dict_constructor
        self.strip_whitespace = strip_whitespace
        self.namespace_separator = namespace_separator
        self.namespaces = namespaces
        self.namespace_declarations = dict_constructor()
        self.force_list = force_list
        self.comment_key = comment_key
        self.xsi_types = xsi_types
        if xsi_unknown_types not in ("keep", "error"):
            raise ValueError(
                "xsi_unknown_types must be either 'keep' or 'error'"
            )
        self.xsi_unknown_types = xsi_unknown_types
        # Stack of in-scope namespace mappings (prefix or '' -> URI).
        self.ns_stack = []
        # Per-element flags recording whether a frame was pushed for it.
        self.ns_pushed = []
        # xsi:* markers for each open element, aligned with self.path.
        self.xsi_markers = []

    def _build_name(self, full_name):
        if self.namespaces is None:
            return full_name
        i = full_name.rfind(self.namespace_separator)
        if i == -1:
            return full_name
        namespace, name = full_name[:i], full_name[i+1:]
        try:
            short_namespace = self.namespaces[namespace]
        except KeyError:
            short_namespace = namespace
        if not short_namespace:
            return name
        else:
            return self.namespace_separator.join((short_namespace, name))

    def _attrs_to_dict(self, attrs):
        if isinstance(attrs, dict):
            return attrs
        return self.dict_constructor(zip(attrs[0::2], attrs[1::2]))

    def _current_ns_map(self):
        result = self.dict_constructor()
        for mapping in self.ns_stack:
            result.update(mapping)
        return result

    def _match_xsi_attr(self, attr_name, local_name):
        """Return True when ``attr_name`` denotes ``xsi:<local_name>``.

        Works both for expat expanded names
        (``uri:local`` with the namespaces separator) and raw prefixed names
        (``xsi:local``); the prefix is resolved through in-scope xmlns
        declarations, so it does not have to be literally ``xsi``.
        """
        sep = self.namespace_separator
        if sep in attr_name and attr_name.rsplit(sep, 1)[0] == XSI_NS:
            # Expanded name produced by expat with namespace processing on
            # (``http://.../instance:type``); the URI itself contains ':'.
            return attr_name.rsplit(sep, 1)[1] == local_name
        if ":" not in attr_name:
            return False
        prefix, name = attr_name.split(":", 1)
        if name != local_name:
            return False
        return self._current_ns_map().get(prefix) == XSI_NS

    def _extract_xsi_markers(self, attrs):
        """Pull ``xsi:nil``/``xsi:type`` out of the raw attribute mapping.

        Returns ``(nilled, xsi_type, nil_attr_key)``; ``nil_attr_key`` is the
        prefixed/expanded key used when the marker needs to be restored for an
        element that carries other attributes.
        """
        if not self.xsi_types:
            return None, None, None
        nilled = None
        xsi_type = None
        nil_key = None
        for attr_name, attr_value in list(attrs.items()):
            if self._match_xsi_attr(attr_name, "nil"):
                token = attr_value.strip()
                if token in _BOOLEAN_TRUE:
                    nilled = True
                    nil_key = attr_name
                elif token in _BOOLEAN_FALSE:
                    nilled = False
                else:
                    raise ValueError(
                        "invalid xsi:nil lexical value: {!r}".format(attr_value)
                    )
                del attrs[attr_name]
            elif self._match_xsi_attr(attr_name, "type"):
                xsi_type = attr_value
                del attrs[attr_name]
        return nilled, xsi_type, nil_key

    def _resolve_xsd_type(self, qname):
        """Resolve an ``xsi:type`` QName into a local XSD built-in name.

        Returns the converter key (e.g. ``"decimal"``) or ``None`` for types
        that are not the supported built-ins. Raises when a prefixed type
        references an undeclared prefix.
        """
        if ":" not in qname:
            return qname if qname in _XSD_CONVERTERS else None
        prefix, local = qname.split(":", 1)
        uri = self._current_ns_map().get(prefix)
        if uri is None:
            raise ValueError(
                "xsi:type prefix {!r} is not declared".format(prefix)
            )
        if uri == XSD_NS and local in _XSD_CONVERTERS:
            return local
        return None

    def _convert_typed(self, xsi_type, text):
        type_name = self._resolve_xsd_type(xsi_type)
        if type_name is not None:
            return _XSD_CONVERTERS[type_name](text)
        if self.xsi_unknown_types == "error":
            raise ValueError(
                "unknown xsi:type {!r} in strict mode".format(xsi_type)
            )
        return XsiValue(text, xsi_type)

    def _is_element_only(self, item):
        if not isinstance(item, dict):
            return True
        for key in item:
            if not key.startswith(self.attr_prefix):
                return False
        return True

    def _xsi_finalize(self, item, data_parts, name, xsi_info):
        """Apply xsi:nil / xsi:type semantics to a finished leaf element.

        Returns ``(item, data, handled)``: when ``handled`` is True the value
        already includes the final data and the caller must not append cdata
        again; otherwise ``data`` is the joined but unstripped text.
        """
        nilled, xsi_type, nil_key = xsi_info
        raw_text = self.cdata_separator.join(data_parts) if data_parts else ""

        if nilled:
            has_text = bool(raw_text.strip())
            if has_text or not self._is_element_only(item):
                raise ValueError(
                    "element with xsi:nil=\"true\" must be empty"
                )
            if isinstance(item, dict):
                item[self.attr_prefix + nil_key] = "true"
                return item, None, True
            return None, None, True

        if xsi_type is not None:
            if not self._is_element_only(item):
                raise ValueError(
                    "xsi:type is only supported on elements without children"
                )
            value = self._convert_typed(xsi_type, raw_text)
            if isinstance(item, dict):
                item[self.cdata_key] = value
                return item, None, True
            return value, None, True

        if item is None and not raw_text:
            # Distinguish an explicit empty element (<a/>) from a nilled one
            # (xsi:nil -> None) and from a missing node.
            return "", None, True

        return item, raw_text, False

    def startNamespaceDecl(self, prefix, uri):
        self.namespace_declarations[prefix or ''] = uri

    def startElement(self, full_name, attrs):
        name = self._build_name(full_name)
        attrs = self._attrs_to_dict(attrs)
        # When expat namespace processing is disabled it does not fire
        # StartNamespaceDeclHandler, so xmlns declarations only surface as
        # regular attributes. Collect them here as well so xsi prefixes can be
        # resolved independently of process_namespaces.
        if self.xsi_types and self.namespaces is None:
            for attr_name, attr_value in list(attrs.items()):
                if attr_name == "xmlns":
                    self.namespace_declarations[""] = attr_value
                elif attr_name.startswith("xmlns:"):
                    self.namespace_declarations[
                        attr_name[len("xmlns:"):]
                    ] = attr_value
        if self.namespace_declarations:
            if not attrs:
                attrs = self.dict_constructor()
            declarations = self.namespace_declarations
            attrs['xmlns'] = declarations
            self.namespace_declarations = self.dict_constructor()
        else:
            declarations = None
        if self.xsi_types:
            # The declarations collected for this element are in scope on the
            # element itself and on its descendants.
            if declarations is not None:
                self.ns_stack.append(declarations)
                self.ns_pushed.append(True)
            else:
                self.ns_pushed.append(False)
        if self.xsi_types:
            xsi_info = self._extract_xsi_markers(attrs)
            self.xsi_markers.append(xsi_info)
        self.path.append((name, attrs or None))
        if len(self.path) >= self.item_depth:
            self.stack.append((self.item, self.data))
            if self.xml_attribs:
                attr_entries = []
                for key, value in attrs.items():
                    key = self.attr_prefix+self._build_name(key)
                    if self.postprocessor:
                        entry = self.postprocessor(self.path, key, value)
                    else:
                        entry = (key, value)
                    if entry:
                        attr_entries.append(entry)
                attrs = self.dict_constructor(attr_entries)
            else:
                attrs = None
            self.item = attrs or None
            self.data = []

    def _finalize_item(self, item, data_parts, name):
        """Finish a leaf element: whitespace stripping, force_cdata, xsi."""
        if self.xsi_types:
            xsi_info = self.xsi_markers.pop()
            item, data, handled = self._xsi_finalize(
                item, data_parts, name, xsi_info)
            if not handled:
                if self.strip_whitespace and data:
                    data = data.strip() or None
                if (data and self._should_force_cdata(name, data)
                        and item is None):
                    item = self.dict_constructor()
                if item is not None:
                    if data:
                        self.push_data(item, self.cdata_key, data)
                else:
                    item = data
            return item

        data = (None if not data_parts
                else self.cdata_separator.join(data_parts))
        if self.strip_whitespace and data:
            data = data.strip() or None
        if data and self._should_force_cdata(name, data) and item is None:
            item = self.dict_constructor()
        if item is not None:
            if data:
                self.push_data(item, self.cdata_key, data)
        else:
            item = data
        return item

    def endElement(self, full_name):
        name = self._build_name(full_name)
        # If we just closed an item at the streaming depth, emit it and drop it
        # without attaching it back to its parent. This avoids accumulating all
        # streamed items in memory when using item_depth > 0.
        if len(self.path) == self.item_depth:
            item = self._finalize_item(self.item, self.data, name)

            should_continue = self.item_callback(self.path, item)
            if not should_continue:
                raise ParsingInterrupted
            # Reset state for the parent context without keeping a reference to
            # the emitted item.
            if self.stack:
                self.item, self.data = self.stack.pop()
            else:
                self.item = None
                self.data = []
            if self.xsi_types and self.ns_pushed.pop():
                self.ns_stack.pop()
            self.path.pop()
            return
        if self.stack:
            item = self.item
            item = self._finalize_item(item, self.data, name)
            self.item, self.data = self.stack.pop()
            self.item = self.push_data(self.item, name, item)
        else:
            self.item = None
            self.data = []
        if self.xsi_types and self.ns_pushed.pop():
            self.ns_stack.pop()
        self.path.pop()

    def characters(self, data):
        if not self.data:
            self.data = [data]
        else:
            self.data.append(data)

    def comments(self, data):
        if self.strip_whitespace:
            data = data.strip()
        self.item = self.push_data(self.item, self.comment_key, data)

    def push_data(self, item, key, data):
        if self.postprocessor is not None:
            result = self.postprocessor(self.path, key, data)
            if result is None:
                return item
            key, data = result
        if item is None:
            item = self.dict_constructor()
        try:
            value = item[key]
            if isinstance(value, list):
                value.append(data)
            else:
                item[key] = [value, data]
        except KeyError:
            if self._should_force_list(key, data):
                item[key] = [data]
            else:
                item[key] = data
        return item

    def _should_force_list(self, key, value):
        if not self.force_list:
            return False
        if isinstance(self.force_list, bool):
            return self.force_list
        try:
            return key in self.force_list
        except TypeError:
            return self.force_list(self.path[:-1], key, value)

    def _should_force_cdata(self, key, value):
        if not self.force_cdata:
            return False
        if isinstance(self.force_cdata, bool):
            return self.force_cdata
        try:
            return key in self.force_cdata
        except TypeError:
            return self.force_cdata(self.path[:-1], key, value)


def parse(xml_input, encoding=None, expat=expat, process_namespaces=False,
          namespace_separator=':', disable_entities=True, process_comments=False, **kwargs):
    """Parse the given XML input and convert it into a dictionary.

    `xml_input` can either be a `string`, a file-like object, or a generator of strings.

    If `xml_attribs` is `True`, element attributes are put in the dictionary
    among regular child elements, using `@` as a prefix to avoid collisions. If
    set to `False`, they are just ignored.

    Simple example::

        >>> import xmltodict
        >>> doc = xmltodict.parse(\"\"\"
        ... <a prop="x">
        ...   <b>1</b>
        ...   <b>2</b>
        ... </a>
        ... \"\"\")
        >>> doc['a']['@prop']
        'x'
        >>> doc['a']['b']
        ['1', '2']

    If `item_depth` is `0`, the function returns a dictionary for the root
    element (default behavior). Otherwise, it calls `item_callback` every time
    an item at the specified depth is found and returns `None` in the end
    (streaming mode).

    The callback function receives two parameters: the `path` from the document
    root to the item (name-attribs pairs), and the `item` (dict). If the
    callback's return value is false-ish, parsing will be stopped with the
    :class:`ParsingInterrupted` exception.

    Streaming example::

        >>> def handle(path, item):
        ...     print('path:%s item:%s' % (path, item))
        ...     return True
        ...
        >>> xmltodict.parse(\"\"\"
        ... <a prop="x">
        ...   <b>1</b>
        ...   <b>2</b>
        ... </a>\"\"\", item_depth=2, item_callback=handle)
        path:[('a', {'prop': 'x'}), ('b', None)] item:1
        path:[('a', {'prop': 'x'}), ('b', None)] item:2

    The optional argument `postprocessor` is a function that takes `path`,
    `key` and `value` as positional arguments and returns a new `(key, value)`
    pair where both `key` and `value` may have changed. Usage example::

        >>> def postprocessor(path, key, value):
        ...     try:
        ...         return key + ':int', int(value)
        ...     except (ValueError, TypeError):
        ...         return key, value
        >>> xmltodict.parse('<a><b>1</b><b>2</b><b>x</b></a>',
        ...                 postprocessor=postprocessor)
        {'a': {'b:int': [1, 2], 'b': 'x'}}

    You can pass an alternate version of `expat` (such as `defusedexpat`) by
    using the `expat` parameter. E.g:

        >>> import defusedexpat
        >>> xmltodict.parse('<a>hello</a>', expat=defusedexpat.pyexpat)
        {'a': 'hello'}

    You can use the force_list argument to force lists to be created even
    when there is only a single child of a given level of hierarchy. The
    force_list argument is a tuple of keys. If the key for a given level
    of hierarchy is in the force_list argument, that level of hierarchy
    will have a list as a child (even if there is only one sub-element).
    The index_keys operation takes precedence over this. This is applied
    after any user-supplied postprocessor has already run.

        For example, given this input:
        <servers>
          <server>
            <name>host1</name>
            <os>Linux</os>
            <interfaces>
              <interface>
                <name>em0</name>
                <ip_address>10.0.0.1</ip_address>
              </interface>
            </interfaces>
          </server>
        </servers>

        If called with force_list=('interface',), it will produce
        this dictionary:
        {'servers':
          {'server':
            {'name': 'host1',
             'os': 'Linux'},
             'interfaces':
              {'interface':
                [ {'name': 'em0', 'ip_address': '10.0.0.1' } ] } } }

        `force_list` can also be a callable that receives `path`, `key` and
        `value`. This is helpful in cases where the logic that decides whether
        a list should be forced is more complex.


        If `process_comments` is `True`, comments will be added using `comment_key`
        (default=`'#comment'`) to the tag that contains the comment.

            For example, given this input:
            <a>
              <b>
                <!-- b comment -->
                <c>
                    <!-- c comment -->
                    1
                </c>
                <d>2</d>
              </b>
            </a>

            If called with `process_comments=True`, it will produce
            this dictionary:
            'a': {
                'b': {
                    '#comment': 'b comment',
                    'c': {

                        '#comment': 'c comment',
                        '#text': '1',
                    },
                    'd': '2',
                },
            }
        Comment text is subject to the `strip_whitespace` flag: when it is left
        at the default `True`, comments will have leading and trailing
        whitespace removed. Disable `strip_whitespace` to keep comment
        indentation or padding intact.

    If `xsi_types` is `True`, elements using the XML Schema Instance
    namespace are interpreted instead of returned as plain text. The xsi
    prefix is taken from in-scope xmlns declarations and does not have to be
    literally ``xsi``.

    - ``<b xsi:nil="true"/>`` becomes ``None``; an element that is nilled but
      also carries non-whitespace text or child elements raises `ValueError`.
    - ``xsi:type`` converts the text to a native Python value for the built-in
      types ``string``, ``boolean`` (``true``/``false``/``1``/``0`` only,
      never via ``bool(text)``), ``integer`` (``int``), ``decimal``
      (:class:`decimal.Decimal`), ``dateTime`` (timezone-aware
      :class:`datetime.datetime`) and ``base64Binary`` (``bytes``).
    - Unknown ``xsi:type`` values keep their original text wrapped in an
      :class:`XsiValue` by default; pass ``xsi_unknown_types="error"`` to
      raise `ValueError` instead.
    - An empty element such as ``<b/>`` becomes ``''`` rather than ``None``,
      so nilled values, empty text and missing nodes remain distinguishable.

    Whitespace follows the XSD lexical rules of each type (trimmed/collapsed
    for scalar types, preserved for ``string``).
    """
    handler = _DictSAXHandler(namespace_separator=namespace_separator,
                              **kwargs)
    if isinstance(xml_input, str):
        encoding = encoding or 'utf-8'
        xml_input = xml_input.encode(encoding)
    if not process_namespaces:
        namespace_separator = None
    parser = expat.ParserCreate(
        encoding,
        namespace_separator
    )
    parser.ordered_attributes = True
    parser.StartNamespaceDeclHandler = handler.startNamespaceDecl
    parser.StartElementHandler = handler.startElement
    parser.EndElementHandler = handler.endElement
    parser.CharacterDataHandler = handler.characters
    if process_comments:
        parser.CommentHandler = handler.comments
    parser.buffer_text = True
    if disable_entities:
        def _forbid_entities(*_args, **_kwargs):
            raise ValueError("entities are disabled")

        parser.EntityDeclHandler = _forbid_entities
    if hasattr(xml_input, 'read'):
        parser.ParseFile(xml_input)
    elif isgenerator(xml_input):
        for chunk in xml_input:
            parser.Parse(chunk, False)
        parser.Parse(b'', True)
    else:
        parser.Parse(xml_input, True)
    return handler.item


def _convert_value_to_string(value, encoding='utf-8', bytes_errors='replace'):
    """Convert a value to its string representation for XML output.

    Handles boolean values consistently by converting them to lowercase.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode(encoding, errors=bytes_errors)
    return str(value)


def _validate_name(value, kind):
    """Validate an element/attribute name for XML safety.

    Raises ValueError with a specific reason when invalid.

    kind: 'element' or 'attribute' (used in error messages)
    """
    if not isinstance(value, str):
        raise ValueError(f"{kind} name must be a string")
    if value.startswith("?") or value.startswith("!"):
        raise ValueError(f'Invalid {kind} name: cannot start with "?" or "!"')
    if "<" in value or ">" in value:
        raise ValueError(f'Invalid {kind} name: "<" or ">" not allowed')
    if "/" in value:
        raise ValueError(f'Invalid {kind} name: "/" not allowed')
    if '"' in value or "'" in value:
        raise ValueError(f"Invalid {kind} name: quotes not allowed")
    if "=" in value:
        raise ValueError(f'Invalid {kind} name: "=" not allowed')
    if any(ch.isspace() for ch in value):
        raise ValueError(f"Invalid {kind} name: whitespace not allowed")


def _validate_comment(value):
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Comment text must be valid UTF-8") from exc
    if not isinstance(value, str):
        raise ValueError("Comment text must be a string")
    if "--" in value:
        raise ValueError("Comment text cannot contain '--'")
    if value.endswith("-"):
        raise ValueError("Comment text cannot end with '-'")
    return value


def _process_namespace(name, namespaces, ns_sep=':', attr_prefix='@'):
    if not isinstance(name, str):
        return name
    if not namespaces:
        return name
    try:
        ns, name = name.rsplit(ns_sep, 1)
    except ValueError:
        pass
    else:
        ns_res = namespaces.get(ns.strip(attr_prefix))
        name = '{}{}{}{}'.format(
            attr_prefix if ns.startswith(attr_prefix) else '',
            ns_res, ns_sep, name) if ns_res else name
    return name


def _emit(key, value, content_handler,
          attr_prefix='@',
          cdata_key='#text',
          depth=0,
          preprocessor=None,
          pretty=False,
          newl='\n',
          indent='\t',
          namespace_separator=':',
          namespaces=None,
          full_document=True,
          expand_iter=None,
          encoding='utf-8',
          bytes_errors='replace',
          comment_key='#comment',
          xsi_types=False,
          xsi_prefix='xsi',
          xsd_prefix='xsd'):
    if isinstance(key, str) and key == comment_key:
        comments_list = value if isinstance(value, list) else [value]
        if isinstance(indent, int):
            indent = " " * indent
        for comment_text in comments_list:
            if comment_text is None:
                continue
            comment_text = _convert_value_to_string(
                comment_text, encoding=encoding, bytes_errors=bytes_errors
            )
            if not comment_text:
                continue
            if pretty:
                content_handler.ignorableWhitespace(depth * indent)
            content_handler.comment(comment_text)
            if pretty:
                content_handler.ignorableWhitespace(newl)
        return

    key = _process_namespace(key, namespaces, namespace_separator, attr_prefix)
    if preprocessor is not None:
        result = preprocessor(key, value)
        if result is None:
            return
        key, value = result
    # Minimal validation to avoid breaking out of tag context
    _validate_name(key, "element")
    if not hasattr(value, '__iter__') or isinstance(value, (str, bytes, bytearray, memoryview, dict)):
        value = [value]
    for index, v in enumerate(value):
        if full_document and depth == 0 and index > 0:
            raise ValueError('document with multiple roots')
        add_nil = False
        add_type = None
        if xsi_types and isinstance(v, XsiValue):
            add_type = v.type_name
            v = {cdata_key: v.value}
        if v is None:
            v = {}
            if xsi_types:
                add_nil = True
        elif not isinstance(v, (dict, str)):
            native = _serialize_native(v) if xsi_types else None
            if native is not None:
                lexical, type_name = native
                add_type = '{}:{}'.format(xsd_prefix, type_name)
                v = {cdata_key: lexical}
            elif expand_iter and hasattr(v, '__iter__') and not isinstance(v, (bytes, bytearray, memoryview)):
                v = {expand_iter: v}
            else:
                v = _convert_value_to_string(v, encoding=encoding, bytes_errors=bytes_errors)
        if isinstance(v, str):
            v = {cdata_key: v}
        cdata = None
        attrs = {}
        children = []
        root_ns_extra = None
        if xsi_types and depth == 0:
            # Make sure the schema instance (and schema) prefixes used for
            # synthesized markers are declared on the document root.
            xmlns = v.get(attr_prefix + 'xmlns')
            declared = xmlns if isinstance(xmlns, dict) else {}
            root_ns_extra = {}
            if xsi_prefix not in declared:
                root_ns_extra[xsi_prefix] = XSI_NS
            if xsd_prefix not in declared:
                root_ns_extra[xsd_prefix] = XSD_NS
        for ik, iv in v.items():
            if ik == cdata_key:
                if xsi_types and iv is None:
                    add_nil = True
                    cdata = None
                else:
                    if xsi_types and isinstance(iv, XsiValue):
                        add_type = iv.type_name
                        iv = iv.value
                    if xsi_types and not isinstance(iv, str):
                        converted = _serialize_native(iv)
                        if converted is not None:
                            lexical, type_name = converted
                            cdata = lexical
                            if add_type is None:
                                add_type = '{}:{}'.format(xsd_prefix, type_name)
                        else:
                            cdata = _convert_value_to_string(
                                iv, encoding=encoding, bytes_errors=bytes_errors)
                    elif iv is None:
                        cdata = None
                    else:
                        cdata = _convert_value_to_string(
                            iv, encoding=encoding,
                            bytes_errors=bytes_errors)
                continue
            if isinstance(ik, str) and ik.startswith(attr_prefix):
                ik = _process_namespace(ik, namespaces, namespace_separator,
                                        attr_prefix)
                if ik == attr_prefix + 'xmlns' and isinstance(iv, dict):
                    for k, v in iv.items():
                        _validate_name(k, "attribute")
                        attr = 'xmlns{}'.format(f':{k}' if k else '')
                        attrs[attr] = '' if v is None else _convert_value_to_string(
                            v, encoding=encoding, bytes_errors=bytes_errors
                        )
                    continue
                if iv is None:
                    iv = ''
                elif not isinstance(iv, str):
                    iv = _convert_value_to_string(iv, encoding=encoding, bytes_errors=bytes_errors)
                attr_name = ik[len(attr_prefix) :]
                _validate_name(attr_name, "attribute")
                attrs[attr_name] = iv
                continue
            if isinstance(iv, list) and not iv:
                continue # Skip empty lists to avoid creating empty child elements
            children.append((ik, iv))
        if root_ns_extra:
            for prefix, uri in root_ns_extra.items():
                attrs.setdefault(
                    'xmlns{}'.format(f':{prefix}' if prefix else ''), uri)
        if xsi_types:
            if add_nil and not any(
                    a == '{}:nil'.format(xsi_prefix) or a == 'nil'
                    for a in attrs):
                attrs['{}:nil'.format(xsi_prefix)] = 'true'
            if add_type is not None:
                qname = add_type
                if ':' not in qname:
                    qname = '{}:{}'.format(xsd_prefix, qname)
                type_attr = '{}:type'.format(xsi_prefix)
                if type_attr not in attrs and 'type' not in attrs:
                    attrs[type_attr] = qname
        if isinstance(indent, int):
            indent = ' ' * indent
        if pretty:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.startElement(key, AttributesImpl(attrs))
        if pretty and children:
            content_handler.ignorableWhitespace(newl)
        for child_key, child_value in children:
            _emit(child_key, child_value, content_handler,
                  attr_prefix, cdata_key, depth+1, preprocessor,
                  pretty, newl, indent, namespaces=namespaces,
                  namespace_separator=namespace_separator,
                  expand_iter=expand_iter, encoding=encoding,
                  bytes_errors=bytes_errors, comment_key=comment_key,
                  xsi_types=xsi_types, xsi_prefix=xsi_prefix,
                  xsd_prefix=xsd_prefix)
        if cdata is not None:
            content_handler.characters(cdata)
        if pretty and children:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.endElement(key)
        if pretty and depth:
            content_handler.ignorableWhitespace(newl)


class _XMLGenerator(XMLGenerator):
    def comment(self, text):
        text = _validate_comment(text)
        self._write(f"<!--{escape(text)}-->")


def unparse(input_dict, output=None, encoding='utf-8', full_document=True,
            short_empty_elements=False, comment_key='#comment',
            **kwargs):
    """Emit an XML document for the given `input_dict` (reverse of `parse`).

    The resulting XML document is returned as a string, but if `output` (a
    file-like object) is specified, it is written there instead.

    Dictionary keys prefixed with `attr_prefix` (default=`'@'`) are interpreted
    as XML node attributes, whereas keys equal to `cdata_key`
    (default=`'#text'`) are treated as character data.

    Empty lists are omitted entirely: ``{"a": []}`` produces no ``<a>`` element.
    Provide a placeholder entry (for example ``{"a": [""]}``) when an explicit
    empty container element must be emitted.

    The `pretty` parameter (default=`False`) enables pretty-printing. In this
    mode, lines are terminated with `'\n'` and indented with `'\t'`, but this
    can be customized with the `newl` and `indent` parameters.
    The `bytes_errors` parameter controls decoding errors for byte values and
    defaults to `'replace'`.

    If `xsi_types` is `True`, native scalars are serialized with an explicit
    ``xsi:type`` marker: ``bool`` -> ``xsd:boolean``, ``int`` ->
    ``xsd:integer``, :class:`decimal.Decimal` -> ``xsd:decimal``,
    :class:`datetime.datetime` -> ``xsd:dateTime`` and ``bytes``/``bytearray``
    -> base64 ``xsd:base64Binary``. ``None`` is emitted as an element with
    ``xsi:nil="true"``, whereas ``''`` produces an ordinary empty element and
    a missing key produces no element at all. :class:`XsiValue` forces an
    explicit marker (including round-tripped unknown types). The ``xsi`` /
    ``xsd`` prefixes declared on the root element (``@xmlns``) are reused;
    override them with `xsi_prefix` / `xsd_prefix`.

    """
    bytes_errors = kwargs.pop('bytes_errors', 'replace')
    try:
        codecs.lookup_error(bytes_errors)
    except LookupError as exc:
        raise ValueError(f"Invalid bytes_errors handler: {bytes_errors}") from exc

    xsi_types = kwargs.pop('xsi_types', False)
    attr_prefix = kwargs.get('attr_prefix', '@')
    xsi_prefix = kwargs.pop('xsi_prefix', None)
    xsd_prefix = kwargs.pop('xsd_prefix', None)
    if xsi_types:
        # Respect prefixes already declared on the root element; fall back to
        # the conventional ``xsi`` / ``xsd`` prefixes.
        root_entries = [
            (rk, rv) for rk, rv in input_dict.items()
            if rk != comment_key
        ]
        root_ns = {}
        if root_entries:
            root_value = root_entries[0][1]
            if isinstance(root_value, dict):
                root_ns = root_value.get(attr_prefix + 'xmlns') or {}
        uri_to_prefix = {uri: prefix for prefix, uri in root_ns.items()}
        xsi_prefix = xsi_prefix or uri_to_prefix.get(XSI_NS, 'xsi')
        xsd_prefix = xsd_prefix or uri_to_prefix.get(XSD_NS, 'xsd')

    must_return = False
    if output is None:
        output = StringIO()
        must_return = True
    if short_empty_elements:
        content_handler = _XMLGenerator(output, encoding, True)
    else:
        content_handler = _XMLGenerator(output, encoding)
    if full_document:
        content_handler.startDocument()
    seen_root = False
    for key, value in input_dict.items():
        if key != comment_key and full_document and seen_root:
            raise ValueError("Document must have exactly one root.")
        _emit(
            key,
            value,
            content_handler,
            full_document=full_document,
            encoding=encoding,
            bytes_errors=bytes_errors,
            comment_key=comment_key,
            xsi_types=xsi_types,
            xsi_prefix=xsi_prefix if xsi_types else 'xsi',
            xsd_prefix=xsd_prefix if xsi_types else 'xsd',
            **kwargs,
        )
        if key != comment_key:
            seen_root = True
    if full_document and not seen_root:
        raise ValueError("Document must have exactly one root.")
    if full_document:
        content_handler.endDocument()
    if must_return:
        value = output.getvalue()
        try:  # pragma no cover
            value = value.decode(encoding)
        except AttributeError:  # pragma no cover
            pass
        return value


if __name__ == '__main__':  # pragma: no cover
    import marshal
    import sys

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    (item_depth,) = sys.argv[1:]
    item_depth = int(item_depth)

    def handle_item(path, item):
        marshal.dump((path, item), stdout)
        return True

    try:
        root = parse(stdin,
                     item_depth=item_depth,
                     item_callback=handle_item,
                     dict_constructor=dict)
        if item_depth == 0:
            handle_item([], root)
    except KeyboardInterrupt:
        pass
