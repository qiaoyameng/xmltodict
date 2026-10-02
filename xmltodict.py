#!/usr/bin/env python
"Makes working with XML feel like you are working with JSON"

from xml.parsers import expat
from xml.sax.saxutils import XMLGenerator, escape
from xml.sax.xmlreader import AttributesImpl
from io import StringIO
from inspect import isgenerator
import codecs
import re
import base64
import datetime as _datetime
from decimal import Decimal


XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
XSD_NAMESPACE = "http://www.w3.org/2001/XMLSchema"


class XsiValue:
    """An XML element value carrying an ``xsi:type`` marker.

    Instances are produced by :func:`parse` when ``xsi_types=True`` and are
    understood by :func:`unparse`. Typed simple elements use an
    ``XsiValue`` with ``xsd_local`` or ``type_name``; ``xsi:nil="true"``
    elements use ``XsiValue(None, nil=True)``, distinct from the plain
    ``None`` used for empty elements.
    """

    __slots__ = ("value", "xsd_local", "type_name", "nil")

    def __init__(self, value, xsd_local=None, type_name=None, nil=False):
        self.value = value
        self.xsd_local = xsd_local
        self.type_name = type_name
        self.nil = nil

    def __repr__(self):
        if self.nil:
            return "XsiValue(None, nil=True)"
        return "XsiValue({!r}, {!r})".format(
            self.value, self.xsd_local or self.type_name)

    def __eq__(self, other):
        if not isinstance(other, XsiValue):
            return NotImplemented
        return (
            self.value == other.value
            and self.xsd_local == other.xsd_local
            and self.type_name == other.type_name
            and self.nil == other.nil
        )


def _xs_collapse_whitespace(text):
    return re.sub(r"[\t\n\r ]+", " ", text).strip()


def _xs_string(text):
    return text


def _xs_boolean(text):
    token = _xs_collapse_whitespace(text)
    if token in ("true", "1"):
        return True
    if token in ("false", "0"):
        return False
    raise ValueError("invalid xsd:boolean: %r" % text)


_INTEGER_RE = re.compile(r"^[+-]?\d+$")


def _xs_integer(text):
    token = _xs_collapse_whitespace(text)
    if not _INTEGER_RE.match(token):
        raise ValueError("invalid xsd:integer: %r" % text)
    return int(token)


_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)$")


def _xs_decimal(text):
    token = _xs_collapse_whitespace(text)
    if not _DECIMAL_RE.match(token):
        raise ValueError("invalid xsd:decimal: %r" % text)
    return Decimal(token)


_DATETIME_RE = re.compile(
    r"^(-?\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$"
)


def _xs_date_time(text):
    token = _xs_collapse_whitespace(text)
    match = _DATETIME_RE.match(token)
    if not match:
        raise ValueError("invalid xsd:dateTime: %r" % text)
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    microsecond = 0
    if fraction:
        microsecond = int((fraction + "000000")[:6])
    tzinfo = None
    if zone:
        if zone == "Z":
            tzinfo = _datetime.timezone.utc
        else:
            sign = 1 if zone[0] == "+" else -1
            offset_hours = int(zone[1:3])
            offset_minutes = int(zone[4:6])
            tzinfo = _datetime.timezone(
                sign * _datetime.timedelta(
                    hours=offset_hours, minutes=offset_minutes))
    return _datetime.datetime(
        int(year), int(month), int(day),
        int(hour), int(minute), int(second),
        microsecond, tzinfo)


def _xs_base64_binary(text):
    token = re.sub(r"[\t\n\r ]", "", text)
    try:
        return base64.b64decode(token, validate=True)
    except Exception as exc:
        raise ValueError("invalid xsd:base64Binary: %r" % text) from exc


_XSD_CONVERTERS = {
    "string": _xs_string,
    "boolean": _xs_boolean,
    "integer": _xs_integer,
    "decimal": _xs_decimal,
    "dateTime": _xs_date_time,
    "base64Binary": _xs_base64_binary,
}


def _xsi_boolean_attr(text):
    token = _xs_collapse_whitespace(text)
    if token in ("true", "1"):
        return True
    if token in ("false", "0"):
        return False
    raise ValueError("xsi:nil must be a boolean, got %r" % text)


def _xsi_lexical(value):
    """Serialize an :class:`XsiValue` body back to its XML lexical form."""
    if value.nil:
        return ""
    inner = value.value
    if isinstance(inner, bool):
        return "true" if inner else "false"
    if isinstance(inner, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(inner)).decode("ascii")
    if isinstance(inner, _datetime.datetime):
        text = inner.strftime("%Y-%m-%dT%H:%M:%S")
        if inner.microsecond:
            text += "." + ("%06d" % inner.microsecond).rstrip("0")
        tzinfo = inner.tzinfo
        if tzinfo is not None:
            offset = int(tzinfo.utcoffset(inner).total_seconds())
            if offset == 0:
                text += "Z"
            else:
                sign = "+" if offset > 0 else "-"
                offset = abs(offset)
                text += "%s%02d:%02d" % (
                    sign, offset // 3600, (offset % 3600) // 60)
        return text
    if inner is None:
        return ""
    return str(inner)


def _infer_xsi_value(value):
    """Wrap a native Python scalar in its canonical XSD marker, if any."""
    if isinstance(value, XsiValue) or value is None:
        return value
    if isinstance(value, bool):
        return XsiValue(value, xsd_local="boolean")
    if isinstance(value, int):
        return XsiValue(value, xsd_local="integer")
    if isinstance(value, Decimal):
        return XsiValue(value, xsd_local="decimal")
    if isinstance(value, _datetime.datetime):
        return XsiValue(value, xsd_local="dateTime")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return XsiValue(bytes(value), xsd_local="base64Binary")
    return value


def _find_declared_prefix(element, uri, attr_prefix="@", cdata_key="#text"):
    """Return the prefix bound to ``uri`` by ``xmlns`` on this element dict."""
    if not isinstance(element, dict):
        return None
    xmlns = element.get(attr_prefix + "xmlns")
    if isinstance(xmlns, dict):
        for prefix, declared in xmlns.items():
            if prefix and declared == uri:
                return prefix
    marker = attr_prefix + "xmlns:"
    for key, declared in element.items():
        if isinstance(key, str) and key.startswith(marker) and declared == uri:
            return key[len(marker):]
    return None


def _scan_xsi_markers(value, cdata_key="#text"):
    """Return ``(needs_xsi, needs_xsd)`` for a parsed element subtree."""
    if isinstance(value, XsiValue):
        return True, bool(value.xsd_local)
    if isinstance(value, (bool, int, Decimal, _datetime.datetime,
                          bytes, bytearray, memoryview)):
        return True, True
    if isinstance(value, dict):
        needs_xsi = False
        needs_xsd = False
        for key, child in value.items():
            child_xsi, child_xsd = _scan_xsi_markers(child, cdata_key)
            needs_xsi = needs_xsi or child_xsi
            needs_xsd = needs_xsd or child_xsd
        return needs_xsi, needs_xsd
    if isinstance(value, (list, tuple)):
        needs_xsi = False
        needs_xsd = False
        for child in value:
            child_xsi, child_xsd = _scan_xsi_markers(child, cdata_key)
            needs_xsi = needs_xsi or child_xsi
            needs_xsd = needs_xsd or child_xsd
        return needs_xsi, needs_xsd
    return False, False


def _ensure_xsi_namespaces(input_dict, cdata_key="#text", attr_prefix="@",
                           comment_key="#comment"):
    """Declare xsi/xsd prefixes on the root element when unparsing typed data."""
    root_key = None
    for key in input_dict:
        if key != comment_key:
            root_key = key
            break
    if root_key is None:
        return input_dict
    root_value = input_dict[root_key]
    needs_xsi, needs_xsd = _scan_xsi_markers(root_value, cdata_key)
    if not needs_xsi and not needs_xsd:
        return input_dict

    new_dict = dict(input_dict)
    if isinstance(root_value, XsiValue):
        marker = root_value
        root_element = {cdata_key: marker}
    elif isinstance(root_value, dict):
        root_element = dict(root_value)
    else:
        return input_dict
    new_dict[root_key] = root_element

    if (needs_xsi and
            _find_declared_prefix(
                root_element, XSI_NAMESPACE, attr_prefix, cdata_key) is None):
        root_element.setdefault(attr_prefix + "xmlns:xsi", XSI_NAMESPACE)
    if (needs_xsd and
            _find_declared_prefix(
                root_element, XSD_NAMESPACE, attr_prefix, cdata_key) is None):
        root_element.setdefault(attr_prefix + "xmlns:xsd", XSD_NAMESPACE)
    return new_dict

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
        xsi_strict=False,
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
        self.xsi_strict = xsi_strict
        # Stack of prefix -> namespace URI maps introduced by xmlns
        # declarations on each currently open element. Only used when
        # xsi_types is enabled (process_namespaces leaves xmlns as attrs).
        self.ns_context_stack = []
        self.ns_bindings = {}

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

    def startNamespaceDecl(self, prefix, uri):
        self.namespace_declarations[prefix or ''] = uri

    def startElement(self, full_name, attrs):
        name = self._build_name(full_name)
        attrs = self._attrs_to_dict(attrs)
        if self.xsi_types:
            new_bindings = {}
            for attr_key, attr_value in list(attrs.items()):
                if attr_key == "xmlns":
                    new_bindings[""] = attr_value
                elif attr_key.startswith("xmlns:"):
                    new_bindings[attr_key[len("xmlns:"):]] = attr_value
            if new_bindings:
                self.ns_bindings = dict(self.ns_bindings)
                self.ns_bindings.update(new_bindings)
            self.ns_context_stack.append(new_bindings)
            xsi_info = self._read_xsi_attrs(attrs)
        else:
            xsi_info = None
        if self.namespace_declarations:
            if not attrs:
                attrs = self.dict_constructor()
            attrs['xmlns'] = self.namespace_declarations
            self.namespace_declarations = self.dict_constructor()
        self.path.append((name, attrs or None))
        if len(self.path) >= self.item_depth:
            self.stack.append((self.item, self.data, xsi_info))
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

    def _read_xsi_attrs(self, attrs):
        nil_key = nil_value = type_key = None
        type_value = (None, None)
        for attr_key, attr_value in list(attrs.items()):
            prefix, _, local = attr_key.rpartition(":")
            if local == "nil" and prefix and self.ns_bindings.get(prefix) == XSI_NAMESPACE:
                nil_key, nil_value = attr_key, _xsi_boolean_attr(attr_value)
            elif (local == "type" and prefix
                  and self.ns_bindings.get(prefix) == XSI_NAMESPACE):
                type_key, type_value = attr_key, self._resolve_xsi_type(attr_value)
        return (nil_key, nil_value, type_key, type_value)

    def _resolve_xsi_type(self, qname):
        prefix, _, local = qname.rpartition(":")
        if not prefix:
            uri = self.ns_bindings.get("")
            if uri == XSD_NAMESPACE and local in _XSD_CONVERTERS:
                return local, XSD_NAMESPACE
            return None, qname
        uri = self.ns_bindings.get(prefix)
        if uri == XSD_NAMESPACE and local in _XSD_CONVERTERS:
            return local, XSD_NAMESPACE
        return None, qname

    def _pop_ns_context(self):
        new_bindings = self.ns_context_stack.pop()
        for prefix in new_bindings:
            self.ns_bindings.pop(prefix, None)

    def _finalize_element(self, name, item, data, xsi_info=None):
        """Apply whitespace, xsi:nil/xsi:type and force_cdata handling."""
        raw = None if not data else self.cdata_separator.join(data)
        if self.xsi_types:
            return self._finalize_xsi(name, item, raw, xsi_info)
        value = raw
        if self.strip_whitespace and value:
            value = value.strip() or None
        if value and self._should_force_cdata(name, value) and item is None:
            item = self.dict_constructor()
        return item, value

    def _finalize_xsi(self, name, item, raw, info):
        nil_key, is_nil, type_key, (xsd_local, type_token) = (
            None, False, None, (None, None)) if info is None else info
        nil_attr = self.attr_prefix + (nil_key or "")
        type_attr = self.attr_prefix + (type_key or "")

        if isinstance(item, dict):
            child_keys = [
                key for key in item
                if not (isinstance(key, str)
                        and (key.startswith(self.attr_prefix)
                             or key == self.cdata_key))
            ]
        else:
            child_keys = []
        has_children = bool(child_keys)

        if is_nil and (raw and raw.strip() or has_children):
            raise ValueError(
                "xsi:nil=\"true\" element must be empty, got content for "
                "%r" % name)

        if has_children:
            # Element-bearing content cannot become a typed scalar. Keep the
            # raw xsi:type attribute and apply normal whitespace handling.
            value = raw
            if self.strip_whitespace and value:
                value = value.strip() or None
            return item, value

        # nil_key is set whenever xsi:nil was present, is_nil when it
        # evaluated true. nil="false"/"0" is an explicit non-nil marker.
        explicit_non_nil = nil_key is not None and not is_nil

        if is_nil:
            if isinstance(item, dict):
                item.pop(nil_attr, None)
                item.pop(type_attr, None)
                remaining_attrs = [
                    key for key in item
                    if isinstance(key, str)
                    and key.startswith(self.attr_prefix)]
            else:
                remaining_attrs = []
            if remaining_attrs:
                item[self.cdata_key] = XsiValue(None, nil=True)
                return item, None
            return None, XsiValue(None, nil=True)

        text = raw if raw is not None else ""
        if xsd_local is not None:
            marker = XsiValue(
                _XSD_CONVERTERS[xsd_local](text), xsd_local=xsd_local)
        elif type_token is not None:
            if self.xsi_strict:
                raise ValueError(
                    "unknown xsi:type %r for element %r" % (type_token, name))
            marker = XsiValue(text, type_name=type_token)
        elif raw is None:
            marker = None
        elif self.strip_whitespace:
            marker = text.strip() or None
        else:
            marker = text

        if isinstance(item, dict):
            # A known built-in xsi:type is fully represented by the
            # XsiValue, so drop that attribute; unknown types and explicit
            # nil="false" markers stay on the element for round-tripping.
            if xsd_local is not None:
                item.pop(type_attr, None)
            remaining_attrs = [
                key for key in item
                if isinstance(key, str) and key.startswith(self.attr_prefix)]
        else:
            remaining_attrs = []

        # Unknown types need a container so their raw xsi:type attribute
        # survives, even when there are no other attributes.
        if type_token is not None and xsd_local is None:
            if item is None:
                item = self.dict_constructor()
            item[self.cdata_key] = marker
            return item, None

        if remaining_attrs or explicit_non_nil:
            if marker is not None:
                if item is None:
                    item = self.dict_constructor()
                item[self.cdata_key] = marker
            return item, None
        return None, marker

    def endElement(self, full_name):
        name = self._build_name(full_name)
        frame_info = self.stack[-1][2] if self.stack else None
        data_list = self.data
        # If we just closed an item at the streaming depth, emit it and drop it
        # without attaching it back to its parent. This avoids accumulating all
        # streamed items in memory when using item_depth > 0.
        if len(self.path) == self.item_depth:
            item = self.item
            item, data = self._finalize_element(
                name, item, data_list, frame_info)
            if data and self._should_force_cdata(name, data) and item is None:
                item = self.dict_constructor()
            if item is not None:
                if data is not None:
                    self.push_data(item, self.cdata_key, data)
            else:
                item = data

            should_continue = self.item_callback(self.path, item)
            if not should_continue:
                raise ParsingInterrupted
            # Reset state for the parent context without keeping a reference to
            # the emitted item.
            if self.stack:
                self.item, self.data, frame_info = self.stack.pop()
            else:
                self.item = None
                self.data = []
            if self.xsi_types:
                self._pop_ns_context()
            self.path.pop()
            return
        if self.stack:
            item = self.item
            item, data = self._finalize_element(
                name, item, data_list, frame_info)
            self.item, self.data, _ = self.stack.pop()
            if data and self._should_force_cdata(name, data) and item is None:
                item = self.dict_constructor()
            if item is not None:
                if data is not None:
                    self.push_data(item, self.cdata_key, data)
                self.item = self.push_data(self.item, name, item)
            else:
                self.item = self.push_data(self.item, name, data)
        else:
            self.item = None
            self.data = []
        if self.xsi_types:
            self._pop_ns_context()
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
    namespace (`http://www.w3.org/2001/XMLSchema-instance`) are converted
    according to their `xsi:type` and `xsi:nil` attributes. The XSI/XSD
    prefixes are not required to be named `xsi`/`xsd`; they are resolved
    from the in-scope `xmlns` declarations. Supported built-in XSD types
    are `string`, `boolean` (`true`/`false`/`1`/`0`; implicit truthiness
    such as ``bool('false')`` is never used),
    `integer`, `decimal` (:class:`decimal.Decimal`), `dateTime`
    (timezone-aware :class:`datetime.datetime`) and `base64Binary`
    (:class:`bytes`). Whitespace is handled per the XSD lexical rules
    (preserved for `string`, collapsed for the other types). A simple
    element with `xsi:nil="true"` becomes ``XsiValue(None, nil=True)``
    (distinct from a missing key or an empty element, which is ``None``),
    and an element that is both nil and non-empty raises `ValueError`. An
    unrecognized `xsi:type` keeps its raw text (or raises `ValueError`
    when `xsi_strict=True`). :func:`unparse` with `xsi_types=True` writes
    the markers back. This option is incompatible with `process_namespaces`.
    """
    if kwargs.get('xsi_types') and process_namespaces:
        raise ValueError(
            "xsi_types is not compatible with process_namespaces")
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
          ns_stack=None):
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
        if isinstance(v, XsiValue) and xsi_types:
            v = {cdata_key: v}
        elif isinstance(v, XsiValue):
            v = {cdata_key: _xsi_lexical(v)}
        elif v is None:
            v = {}
        elif not isinstance(v, (dict, str)):
            if expand_iter and hasattr(v, '__iter__') and not isinstance(v, (bytes, bytearray, memoryview)):
                v = {expand_iter: v}
            elif xsi_types:
                v = {cdata_key: _infer_xsi_value(v)}
            else:
                v = _convert_value_to_string(v, encoding=encoding, bytes_errors=bytes_errors)
        if isinstance(v, str):
            v = {cdata_key: v}
        cdata = None
        attrs = {}
        children = []
        if xsi_types:
            if ns_stack is None:
                ns_stack = []
            declared_frames = [frame[0] for frame in ns_stack]
            declared_here = {}
            if isinstance(v, dict):
                xmlns_map = v.get(attr_prefix + "xmlns")
                if isinstance(xmlns_map, dict):
                    for xmlns_prefix, xmlns_uri in xmlns_map.items():
                        if xmlns_prefix:
                            declared_here[xmlns_prefix] = xmlns_uri
                xmlns_attr = attr_prefix + "xmlns:"
                for ik0, iv0 in v.items():
                    if isinstance(ik0, str) and ik0.startswith(xmlns_attr):
                        declared_here[ik0[len(xmlns_attr):]] = iv0
            in_scope = dict(ns_stack[-1][1]) if ns_stack else {}
            in_scope.update(declared_here)
            scopes = list(declared_frames) + [declared_here]

            def _in_scope_prefix(uri):
                for scope in reversed(scopes):
                    for xmlns_prefix, xmlns_uri in scope.items():
                        if xmlns_uri == uri:
                            return xmlns_prefix
                return None

            xsi_prefix = _in_scope_prefix(XSI_NAMESPACE)
            xsd_prefix = _in_scope_prefix(XSD_NAMESPACE)
            explicit_xsi_nil = None
            explicit_xsi_type = None
            for attr_ik, attr_iv in v.items():
                if not (isinstance(attr_ik, str)
                        and attr_ik.startswith(attr_prefix)):
                    continue
                raw_attr = attr_ik[len(attr_prefix):]
                attr_pfx, _, attr_local = raw_attr.rpartition(":")
                if (attr_pfx and in_scope.get(attr_pfx) == XSI_NAMESPACE):
                    if attr_local == "nil":
                        explicit_xsi_nil = raw_attr
                    elif attr_local == "type":
                        explicit_xsi_type = raw_attr
        else:
            declared_here = None
        for ik, iv in v.items():
            if ik == cdata_key:
                marker = iv
                if marker is None:
                    cdata = None
                    continue
                if xsi_types and not isinstance(marker, XsiValue):
                    marker = _infer_xsi_value(marker)
                if xsi_types and isinstance(marker, XsiValue):
                    prefix = xsi_prefix or "xsi"
                    if marker.nil:
                        if explicit_xsi_nil is None:
                            attrs["%s:nil" % prefix] = "true"
                        if marker.type_name is not None and explicit_xsi_type is None:
                            attrs["%s:type" % prefix] = marker.type_name
                        cdata = None
                    else:
                        if explicit_xsi_type is not None:
                            pass
                        elif marker.xsd_local is not None:
                            attrs["%s:type" % prefix] = "%s:%s" % (
                                xsd_prefix or "xsd", marker.xsd_local)
                        elif marker.type_name is not None:
                            attrs["%s:type" % prefix] = marker.type_name
                        cdata = _xsi_lexical(marker)
                else:
                    cdata = _convert_value_to_string(iv, encoding=encoding, bytes_errors=bytes_errors)
                continue
            if isinstance(ik, str) and ik.startswith(attr_prefix):
                ik = _process_namespace(ik, namespaces, namespace_separator,
                                        attr_prefix)
                if ik == attr_prefix + 'xmlns' and isinstance(iv, dict):
                    for k, xmlns_val in iv.items():
                        _validate_name(k, "attribute")
                        attr = 'xmlns{}'.format(f':{k}' if k else '')
                        attrs[attr] = (
                            '' if xmlns_val is None
                            else _convert_value_to_string(
                                xmlns_val, encoding=encoding,
                                bytes_errors=bytes_errors)
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
        if isinstance(indent, int):
            indent = ' ' * indent
        if xsi_types:
            ns_stack.append((declared_here, in_scope))
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
                  xsi_types=xsi_types, ns_stack=ns_stack)
        if cdata is not None:
            content_handler.characters(cdata)
        if pretty and children:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.endElement(key)
        if xsi_types:
            ns_stack.pop()
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

    If `xsi_types` is `True`, :class:`XsiValue` markers are written back as
    `xsi:type`/`xsi:nil` attributes, and native Python values are typed
    automatically: `bool` -> `xsd:boolean`, `int` -> `xsd:integer`,
    :class:`decimal.Decimal` -> `xsd:decimal`, timezone-aware (or naive)
    :class:`datetime.datetime` -> `xsd:dateTime`, and `bytes`/`bytearray` ->
    `xsd:base64Binary`. Plain `None` still produces an empty element, while
    ``XsiValue(None, nil=True)`` produces an `xsi:nil="true"` element. The
    XSI/XSD prefixes follow the document's in-scope `xmlns` declarations;
    missing declarations are added on the root element using `xsi`/`xsd`.

    """
    bytes_errors = kwargs.pop('bytes_errors', 'replace')
    try:
        codecs.lookup_error(bytes_errors)
    except LookupError as exc:
        raise ValueError(f"Invalid bytes_errors handler: {bytes_errors}") from exc
    xsi_types = kwargs.pop('xsi_types', False)
    if xsi_types:
        input_dict = _ensure_xsi_namespaces(
            input_dict,
            cdata_key=kwargs.get('cdata_key', '#text'),
            attr_prefix=kwargs.get('attr_prefix', '@'),
            comment_key=comment_key)

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
