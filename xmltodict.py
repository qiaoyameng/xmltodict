#!/usr/bin/env python
"Makes working with XML feel like you are working with JSON"

from xml.parsers import expat
from xml.sax.saxutils import XMLGenerator, escape
from xml.sax.xmlreader import AttributesImpl
from io import StringIO
from inspect import isgenerator
import codecs

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
        preserve_namespace_context=False,
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
        self.preserve_namespace_context = preserve_namespace_context

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
        xmlns = None
        if self.namespace_declarations:
            if not attrs:
                attrs = self.dict_constructor()
            xmlns = self.namespace_declarations
            if self.xml_attribs:
                attrs['xmlns'] = xmlns
            self.namespace_declarations = self.dict_constructor()
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
            elif xmlns is not None:
                # Keep per-element namespace declarations available even when
                # regular attributes are not recorded.
                attrs = self.dict_constructor(
                    ((self.attr_prefix + 'xmlns', xmlns),))
            else:
                attrs = None
            self.item = attrs or None
            self.data = []

    def endElement(self, full_name):
        name = self._build_name(full_name)
        # If we just closed an item at the streaming depth, emit it and drop it
        # without attaching it back to its parent. This avoids accumulating all
        # streamed items in memory when using item_depth > 0.
        if len(self.path) == self.item_depth:
            data = (None if not self.data
                    else self.cdata_separator.join(self.data))
            item = self.item
            if self.strip_whitespace and data:
                data = data.strip() or None
            if data and self._should_force_cdata(name, data) and item is None:
                item = self.dict_constructor()
            if item is not None:
                if data:
                    self.push_data(item, self.cdata_key, data)
            else:
                item = data

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
            self.path.pop()
            return
        if self.stack:
            data = (None if not self.data
                    else self.cdata_separator.join(self.data))
            item = self.item
            self.item, self.data = self.stack.pop()
            if self.strip_whitespace and data:
                data = data.strip() or None
            if data and self._should_force_cdata(name, data) and item is None:
                item = self.dict_constructor()
            if item is not None:
                if data:
                    self.push_data(item, self.cdata_key, data)
                self.item = self.push_data(self.item, name, item)
            else:
                self.item = self.push_data(self.item, name, data)
        else:
            self.item = None
            self.data = []
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
          namespace_separator=':', disable_entities=True, process_comments=False,
          preserve_namespace_context=False, **kwargs):
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

    If `preserve_namespace_context` is `True` (requires
    `process_namespaces=True`), elements and attributes are keyed by their
    expanded ``URI<separator>local`` names and every element keeps the xmlns
    declarations present on it (an undeclared default namespace is recorded as
    ``None``). This captures nested namespace rebinding that a single global
    URI-to-prefix map cannot express. Feed the result to
    ``unparse(..., preserve_namespace_context=True)`` to regenerate legal,
    correctly scoped declarations, even after modifying or adding namespaced
    nodes. Expanded names are not collapsed, so the `namespaces` collapse map
    cannot be combined with this option. The declarations are retained even
    when `xml_attribs` is `False`.
    """
    handler = _DictSAXHandler(namespace_separator=namespace_separator,
                              preserve_namespace_context=preserve_namespace_context,
                              **kwargs)
    if isinstance(xml_input, str):
        encoding = encoding or 'utf-8'
        xml_input = xml_input.encode(encoding)
    if not process_namespaces:
        namespace_separator = None
    elif preserve_namespace_context and kwargs.get('namespaces') is not None:
        raise ValueError(
            'preserve_namespace_context requires expanded names; the '
            'namespaces collapse map cannot be combined with it')
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


def _split_expanded_name(name, ns_sep=':'):
    """Split an expanded ``URI<sep>local`` name without assuming only one
    separator occurs: namespace URIs may themselves contain the separator.
    Returns ``(None, name)`` when no namespace part is present.
    """
    if not isinstance(name, str):
        return None, name
    i = name.rfind(ns_sep)
    if i == -1:
        return None, name
    return name[:i], name[i + 1:]


class _NamespaceScope:
    """In-scope namespace bindings used while unparsing expanded names.

    The scope mirrors XML namespace inheritance: each element receives a
    clone of its parent's bindings, applies its own declarations, and the
    parent state is restored when the element closes.
    """

    #: Prefixes predefined by the XML Namespaces spec; never synthesized.
    _BUILTIN_PREFIXES = {
        'xml': 'http://www.w3.org/XML/1998/namespace',
        'xmlns': 'http://www.w3.org/2000/xmlns/',
    }

    def __init__(self, preferred=None, ns_sep=':', declared_prefixes=None):
        # URI -> preferred prefix hints (e.g. the legacy ``namespaces`` map)
        self.preferred = dict(preferred or {})
        # Every prefix that appears in a preserved xmlns declaration in the
        # whole document; synthesized prefixes must avoid these names.
        self.declared_prefixes = set(declared_prefixes or ())
        # prefix -> URI for the currently open elements (None undeclares)
        self.bindings = {}
        # URI -> set of prefixes bound to it in scope
        self.uri_prefixes = {}
        prefix, uri = 'xml', self._BUILTIN_PREFIXES['xml']
        self.bindings[prefix] = uri
        self.uri_prefixes.setdefault(uri, set()).add(prefix)

    def clone(self):
        child = _NamespaceScope(
            preferred=self.preferred,
            declared_prefixes=self.declared_prefixes,
        )
        child.bindings = dict(self.bindings)
        child.uri_prefixes = {
            uri: set(prefixes) for uri, prefixes in self.uri_prefixes.items()
        }
        return child

    def declare(self, prefix, uri):
        """Apply an xmlns declaration on the current element.

        ``uri`` of None/'' undeclares the binding (``xmlns=""``).
        """
        prefix = prefix or ''
        if uri:
            old_uri = self.bindings.get(prefix)
            if old_uri is not None and prefix in self.uri_prefixes.get(old_uri, ()):
                self.uri_prefixes[old_uri].discard(prefix)
            self.bindings[prefix] = uri
            self.uri_prefixes.setdefault(uri, set()).add(prefix)
        else:
            old_uri = self.bindings.pop(prefix, None)
            if old_uri is not None:
                self.uri_prefixes.get(old_uri, set()).discard(prefix)

    def is_default_no_namespace(self):
        return self.bindings.get('') is None

    def _candidate_usable(self, candidate, uri, allow_default):
        if candidate == '' and not allow_default:
            return False
        bound = self.bindings.get(candidate)
        if bound is not None and bound != uri:
            return False
        return True

    def _synthesize(self, uri):
        base = 'ns0'
        candidate = base
        counter = 0
        while (candidate in self.bindings and self.bindings[candidate] != uri
               or candidate in self.declared_prefixes
               or candidate in self._BUILTIN_PREFIXES):
            counter += 1
            candidate = f'ns{counter}'
        return candidate

    def ensure(self, uri, allow_default):
        """Return ``(qname_part, declared)`` for a reference to ``uri``.

        ``declared`` is True when a fresh xmlns declaration must be emitted on
        the referencing element. Attributes pass ``allow_default=False`` since
        unprefixed attributes never belong to the default namespace.
        """
        existing = self.uri_prefixes.get(uri)
        candidates = []
        if existing:
            candidates.extend(sorted(existing))
        hint = self.preferred.get(uri)
        if hint:
            candidates.append(hint)
        if allow_default:
            candidates.append('')
        for candidate in candidates:
            if self._candidate_usable(candidate, uri, allow_default):
                bound = self.bindings.get(candidate)
                if bound == uri:
                    return candidate, False
                self.declare(candidate, uri)
                return candidate, True
        candidate = self._synthesize(uri)
        self.declare(candidate, uri)
        return candidate, True


def _collect_xmlns_prefixes(value, attr_prefix, cdata_key):
    """Collect every prefix used in a preserved @xmlns declaration so that
    synthesized prefixes never collide with them.
    """
    prefixes = set()

    def visit(node_value):
        if isinstance(node_value, list):
            for item in node_value:
                visit(item)
            return
        if not isinstance(node_value, dict):
            return
        xmlns = node_value.get(attr_prefix + 'xmlns')
        if isinstance(xmlns, dict):
            prefixes.update(prefix or '' for prefix in xmlns)
        for child_key, child_value in node_value.items():
            if isinstance(child_key, str) and (
                    child_key == cdata_key
                    or child_key.startswith(attr_prefix)):
                continue
            visit(child_value)

    visit(value)
    return prefixes


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
          preserve_namespace_context=False,
          ns_scope=None):
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

    if not preserve_namespace_context:
        key = _process_namespace(key, namespaces, namespace_separator, attr_prefix)
    if preprocessor is not None:
        result = preprocessor(key, value)
        if result is None:
            return
        key, value = result
    # Minimal validation to avoid breaking out of tag context
    if not preserve_namespace_context:
        _validate_name(key, "element")
    if not hasattr(value, '__iter__') or isinstance(value, (str, bytes, bytearray, memoryview, dict)):
        value = [value]
    for index, v in enumerate(value):
        if full_document and depth == 0 and index > 0:
            raise ValueError('document with multiple roots')
        if v is None:
            v = {}
        elif not isinstance(v, (dict, str)):
            if expand_iter and hasattr(v, '__iter__') and not isinstance(v, (bytes, bytearray, memoryview)):
                v = {expand_iter: v}
            else:
                v = _convert_value_to_string(v, encoding=encoding, bytes_errors=bytes_errors)
        if isinstance(v, str):
            v = {cdata_key: v}
        cdata = None
        attrs = {}
        children = []
        if preserve_namespace_context:
            element_ns, element_local = _split_expanded_name(
                key, namespace_separator)
            # Clone on entry so that declarations made for this occurrence
            # never leak onto siblings (including repeated list occurrences).
            scope = ns_scope.clone() if ns_scope is not None else _NamespaceScope(
                namespaces, namespace_separator)
            xmlns_decls = v.get(attr_prefix + 'xmlns') if isinstance(v, dict) else None
            if isinstance(xmlns_decls, dict):
                for decl_prefix, decl_uri in xmlns_decls.items():
                    _validate_name(decl_prefix or '', "namespace prefix")
                    resolved_uri = (
                        None if decl_uri is None
                        else _convert_value_to_string(
                            decl_uri, encoding=encoding,
                            bytes_errors=bytes_errors) or None)
                    scope.declare(decl_prefix or '', resolved_uri)
                    # XML 1.0 only permits undeclaring the default namespace
                    # (xmlns=""); a prefixed xmlns:p="" is not well-formed, so
                    # update the scope without emitting such an attribute.
                    if not decl_prefix or resolved_uri is not None:
                        attrs['xmlns{}'.format(
                            f':{decl_prefix}' if decl_prefix else '')] = (
                            '' if resolved_uri is None
                            else resolved_uri)
            if element_ns is None:
                qname = element_local
                if not scope.is_default_no_namespace():
                    # In scope of a default namespace: undeclare it so that
                    # the unprefixed element stays in no namespace.
                    scope.declare('', None)
                    attrs['xmlns'] = ''
            else:
                prefix, declared = scope.ensure(element_ns, True)
                qname = element_local if prefix == '' \
                    else f'{prefix}:{element_local}'
                if declared:
                    attrs['xmlns{}'.format(
                        f':{prefix}' if prefix else '')] = element_ns
            _validate_name(qname, "element")
        for ik, iv in v.items():
            if ik == cdata_key:
                if iv is None:
                    cdata = None
                else:
                    cdata = _convert_value_to_string(iv, encoding=encoding, bytes_errors=bytes_errors)
                continue
            if preserve_namespace_context and isinstance(ik, str) \
                    and ik == attr_prefix + 'xmlns':
                # Already handled when establishing the element scope.
                continue
            if isinstance(ik, str) and ik.startswith(attr_prefix):
                if not preserve_namespace_context:
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
                if preserve_namespace_context:
                    attr_key = ik[len(attr_prefix):]
                    attr_ns, attr_local = _split_expanded_name(
                        attr_key, namespace_separator)
                    if attr_ns is None:
                        attr_name = attr_local
                    else:
                        # Unprefixed attributes never inherit the default
                        # namespace, so a prefix is mandatory here.
                        attr_prefix_used, attr_declared = scope.ensure(
                            attr_ns, False)
                        attr_name = (
                            f'{attr_prefix_used}:{attr_local}'
                            if attr_prefix_used else attr_local)
                        if attr_declared:
                            attrs['xmlns:{}'.format(
                                attr_prefix_used)] = attr_ns
                else:
                    attr_name = ik[len(attr_prefix) :]
                _validate_name(attr_name, "attribute")
                attrs[attr_name] = iv
                continue
            if isinstance(iv, list) and not iv:
                continue # Skip empty lists to avoid creating empty child elements
            children.append((ik, iv))
        if isinstance(indent, int):
            indent = ' ' * indent
        if pretty:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.startElement(
            qname if preserve_namespace_context else key,
            AttributesImpl(attrs))
        if pretty and children:
            content_handler.ignorableWhitespace(newl)
        for child_key, child_value in children:
            _emit(child_key, child_value, content_handler,
                  attr_prefix, cdata_key, depth+1, preprocessor,
                  pretty, newl, indent, namespaces=namespaces,
                  namespace_separator=namespace_separator,
                  expand_iter=expand_iter, encoding=encoding,
                  bytes_errors=bytes_errors, comment_key=comment_key,
                  preserve_namespace_context=preserve_namespace_context,
                  ns_scope=scope if preserve_namespace_context else None)
        if cdata is not None:
            content_handler.characters(cdata)
        if pretty and children:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.endElement(
            qname if preserve_namespace_context else key)
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

    Pass ``preserve_namespace_context=True`` to serialize expanded
    ``URI:local`` keys using the per-element ``@xmlns`` declarations produced
    by :func:`parse` with the same option. A stack of namespace scopes is
    maintained while walking the dictionary, so inner prefix rebinding,
    ``xmlns=""`` default-namespace undeclaration, and several prefixes for one
    URI are restored correctly. Unprefixed attributes are never placed in the
    default namespace. New elements or attributes whose URI has no usable
    in-scope prefix receive a non-colliding generated prefix and a local
    declaration. The legacy global `namespaces` map, when given, acts only as a
    preferred-prefix hint.

    """
    bytes_errors = kwargs.pop('bytes_errors', 'replace')
    try:
        codecs.lookup_error(bytes_errors)
    except LookupError as exc:
        raise ValueError(f"Invalid bytes_errors handler: {bytes_errors}") from exc
    preserve_namespace_context = kwargs.pop(
        'preserve_namespace_context', False)
    attr_prefix = kwargs.get('attr_prefix', '@')
    cdata_key = kwargs.get('cdata_key', '#text')
    namespace_separator = kwargs.get('namespace_separator', ':')
    ns_scope = None
    if preserve_namespace_context:
        declared_prefixes = set()
        for root_value in input_dict.values():
            declared_prefixes |= _collect_xmlns_prefixes(
                root_value, attr_prefix, cdata_key)
        ns_scope = _NamespaceScope(
            preferred=kwargs.get('namespaces'),
            ns_sep=namespace_separator,
            declared_prefixes=declared_prefixes)

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
            preserve_namespace_context=preserve_namespace_context,
            ns_scope=ns_scope,
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
