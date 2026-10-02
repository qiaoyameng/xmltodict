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


DEFAULT_MIXED_CONTENT_KEY = '#content'
_DROP = object()


def _validate_content_key(content_key, cdata_key, attr_prefix):
    """Validate the key used for ordered mixed-content lists."""
    if not isinstance(content_key, str) or not content_key:
        raise ValueError("content_key must be a non-empty string")
    if content_key == cdata_key:
        raise ValueError(
            "content_key must differ from cdata_key "
            "(%r)" % cdata_key)
    if content_key.startswith(attr_prefix):
        raise ValueError(
            "content_key must not start with attr_prefix "
            "(%r)" % attr_prefix)
    return content_key


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
        mixed_content=False,
        content_key=DEFAULT_MIXED_CONTENT_KEY,
    ):
        self.path = []
        self.stack = []
        self.data = []
        self.content = []
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
        self.mixed_content = mixed_content
        self.content_key = content_key

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
        if self.namespace_declarations:
            if not attrs:
                attrs = self.dict_constructor()
            attrs['xmlns'] = self.namespace_declarations
            self.namespace_declarations = self.dict_constructor()
        if self.mixed_content:
            # Any pending text belongs to the parent and precedes this child.
            self._flush_pending_text()
        self.path.append((name, attrs or None))
        if len(self.path) >= self.item_depth:
            self.stack.append((self.item, self.data, self.content))
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
            if self.mixed_content:
                self.content = []

    def endElement(self, full_name):
        name = self._build_name(full_name)
        if self.mixed_content:
            self._end_element_mixed(name)
            return
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
                self.item, self.data, _content = self.stack.pop()
            else:
                self.item = None
                self.data = []
            self.path.pop()
            return
        if self.stack:
            data = (None if not self.data
                    else self.cdata_separator.join(self.data))
            item = self.item
            self.item, self.data, _content = self.stack.pop()
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

    def _pending_text(self):
        """Return the accumulated text for the current element, or None."""
        if not self.data:
            return None
        data = self.cdata_separator.join(self.data)
        if self.strip_whitespace and not data.strip():
            # Whitespace-only runs follow strip_whitespace; text fragments
            # containing non-whitespace characters are always kept verbatim.
            return None
        return data

    def _flush_pending_text(self):
        """Move the current element's pending text into its ordered content."""
        data = self._pending_text()
        if data is not None:
            if self.postprocessor is not None:
                result = self.postprocessor(self.path, self.cdata_key, data)
                if result is not None:
                    _pkey, pvalue = result
                    if pvalue is not None:
                        self.content.append(pvalue)
            else:
                self.content.append(data)
        self.data = []

    def _end_element_mixed(self, name):
        """endElement implementation for mixed_content mode."""
        is_stream_item = (self.item_depth > 0
                          and len(self.path) == self.item_depth)
        self._flush_pending_text()
        item = self.item
        content = self.content
        if is_stream_item:
            value = self._build_mixed_value(name, item, content)
            # Emit the item and drop it instead of attaching it to the parent.
            should_continue = self.item_callback(self.path, value)
            if not should_continue:
                raise ParsingInterrupted
            if self.stack:
                self.item, self.data, self.content = self.stack.pop()
            else:
                self.item = None
                self.data = []
                self.content = []
            self.path.pop()
            return
        if self.stack:
            self.item, self.data, self.content = self.stack.pop()
            # The stack is restored to the parent context, but self.path still
            # contains this element, so postprocessors see the same path as in
            # the classic mode.
            value = self._build_mixed_value(name, item, content)
            if self.postprocessor is not None:
                result = self.postprocessor(self.path, name, value)
                if result is None:
                    value = _DROP
                else:
                    name, value = result
            if value is not _DROP:
                self.content.append((name, value))
            if not self.stack:
                # Back at document level: group the top-level element(s).
                document = self.dict_constructor()
                for child_name, child_value in self.content:
                    try:
                        existing = document[child_name]
                        if isinstance(existing, list):
                            existing.append(child_value)
                        else:
                            document[child_name] = [existing, child_value]
                    except KeyError:
                        document[child_name] = child_value
                self.item = document
                self.content = []
        else:
            value = self._build_mixed_value(name, item, content)
            self.item = value
            self.data = []
            self.content = []
        self.path.pop()

    def _build_mixed_value(self, name, item, content):
        """Build a leaf/element value from its attributes and ordered content.

        Leaf elements (no child elements) keep the classic representation:
        plain text, ``None`` for empty elements, or an attributes dict.
        Elements with child elements keep an ordered ``content_key`` list
        so that sibling and text order is preserved.
        """
        text_entries = []
        child_entries = []
        for entry in content:
            if isinstance(entry, tuple):
                child_entries.append(entry)
            else:
                text_entries.append(entry)
        if not child_entries:
            if not content:
                return item
            data = self.cdata_separator.join(text_entries)
            if data and self._should_force_cdata(name, data) and item is None:
                item = self.dict_constructor()
            if item is not None:
                if data:
                    item[self.cdata_key] = data
                return item
            return data
        # The element contains child elements: their order relative to text
        # and to each other is preserved in the ordered content list.
        ordered = list(content)
        result = item if item is not None else self.dict_constructor()
        result[self.content_key] = ordered
        return result

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
          mixed_content=False, content_key=DEFAULT_MIXED_CONTENT_KEY, **kwargs):
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

    If `mixed_content` is `True` (default `False`), the order of text and
    child elements is preserved for every element that contains child
    elements. Such an element is a dict whose `content_key` (default
    `'#content'`) value is an ordered list of content items:

    * a plain string holds a text fragment (CDATA sections are merged into
      ordinary text);
    * a `(tag, value)` tuple holds one child element. Repeated tuples with the
      same tag represent repeated sibling elements.

    Attributes remain ordinary `attr_prefix` keys on the same dict.
    Leaf elements keep the usual representation (plain text, `None` for empty
    elements, an attributes dict, or `cdata_key` text with attributes)::

        >>> xmltodict.parse('<p>before<b>bold</b>after</p>',
        ...                 mixed_content=True)
        {'p': {'#content': ['before', ('b', 'bold'), 'after']}}

        >>> xmltodict.parse('<a><b>1</b><c>2</c></a>', mixed_content=True)
        {'a': {'#content': [('b', '1'), ('c', '2')]}}

    Text fragments that contain non-whitespace characters are always kept
    verbatim; whitespace-only fragments are kept only when `strip_whitespace`
    is `False`. The representation is accepted by `unparse` without any flag;
    single-key dicts are also accepted as child items for convenience.
    `force_list` has no additional effect in this representation because each
    child is already a separate ordered entry. `content_key` must differ from
    `cdata_key` and must not start with `attr_prefix`; otherwise a
    `ValueError` is raised.
    """
    if mixed_content:
        attr_prefix = kwargs.get('attr_prefix', '@')
        cdata_key = kwargs.get('cdata_key', '#text')
        _validate_content_key(content_key, cdata_key, attr_prefix)
    handler = _DictSAXHandler(namespace_separator=namespace_separator,
                              mixed_content=mixed_content,
                              content_key=content_key,
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


def _normalize_ordered_content(content, parent_key, content_key):
    """Normalize a mixed-content list into ``(items, text_fragments)``.

    Each item is either a text fragment (string) or a ``(tag, value)``
    child-element tuple. A single-key dict ``{tag: value}`` is also accepted
    for convenience. Non-string scalars are stringified like ordinary
    character data.
    """
    if not isinstance(content, (list, tuple)):
        raise ValueError(
            "%r value of element %r must be a list"
            % (content_key, parent_key))
    items = []
    texts = []
    for item in content:
        if item is None:
            raise ValueError(
                "None is not a valid %r item of element %r"
                % (content_key, parent_key))
        if isinstance(item, (str, bytes, bytearray, memoryview)):
            text = _convert_value_to_string(item)
        elif isinstance(item, tuple):
            if len(item) != 2 or not isinstance(item[0], str):
                raise ValueError(
                    "child items in %r of element %r must be (tag, value) "
                    "pairs" % (content_key, parent_key))
            items.append(item)
            continue
        elif isinstance(item, dict):
            if len(item) != 1:
                raise ValueError(
                    "dict items in %r of element %r must contain exactly "
                    "one child key" % (content_key, parent_key))
            items.append(next(iter(item.items())))
            continue
        elif isinstance(item, (int, float, bool)):
            text = _convert_value_to_string(item)
        else:
            raise ValueError(
                "unsupported %r item type for element %r: %r"
                % (content_key, parent_key, type(item).__name__))
        items.append(text)
        texts.append(text)
    return items, texts


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
          content_key=DEFAULT_MIXED_CONTENT_KEY):
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
    _validate_content_key(content_key, cdata_key, attr_prefix)
    # Minimal validation to avoid breaking out of tag context
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
        ordered_content = v.get(content_key) if content_key in v else None
        for ik, iv in v.items():
            if ik == cdata_key:
                if iv is None:
                    cdata = None
                else:
                    cdata = _convert_value_to_string(iv, encoding=encoding, bytes_errors=bytes_errors)
                continue
            if ik == content_key:
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
            if ordered_content is not None and isinstance(ik, str) \
                    and ik == comment_key:
                # Comments are metadata, like attributes; their position
                # relative to ordered content is not tracked. Emit them first.
                comments_list = iv if isinstance(iv, list) else [iv]
                children.append((comment_key, comments_list))
                continue
            children.append((ik, iv))
        if ordered_content is not None:
            if [c for c in children if c[0] != comment_key]:
                raise ValueError(
                    "%r elements cannot mix %r with other child keys"
                    % (key, content_key))
            if cdata is not None:
                raise ValueError(
                    "%r elements cannot use both %r and %r"
                    % (key, content_key, cdata_key))
        if isinstance(indent, int):
            indent = ' ' * indent
        if pretty:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.startElement(key, AttributesImpl(attrs))
        if ordered_content is not None:
            for child_key, child_value in children:
                if child_key == comment_key:
                    _emit(child_key, child_value, content_handler,
                          attr_prefix, cdata_key, depth+1, preprocessor,
                          pretty, newl, indent, namespaces=namespaces,
                          namespace_separator=namespace_separator,
                          expand_iter=expand_iter, encoding=encoding,
                          bytes_errors=bytes_errors, comment_key=comment_key,
                          content_key=content_key)
            emit_items, emit_texts = _normalize_ordered_content(
                ordered_content, key, content_key)
            has_text = bool(emit_texts)
            if pretty and emit_items and not has_text:
                content_handler.ignorableWhitespace(newl)
            for item in emit_items:
                if isinstance(item, str):
                    content_handler.characters(item)
                else:
                    child_key, child_value = item
                    _emit(child_key, child_value, content_handler,
                          attr_prefix, cdata_key, depth+1, preprocessor,
                          pretty and not has_text, newl, indent,
                          namespaces=namespaces,
                          namespace_separator=namespace_separator,
                          expand_iter=expand_iter, encoding=encoding,
                          bytes_errors=bytes_errors, comment_key=comment_key,
                          content_key=content_key)
            if pretty and emit_items and not has_text:
                content_handler.ignorableWhitespace(depth * indent)
        elif pretty and children:
            content_handler.ignorableWhitespace(newl)
        if ordered_content is None:
            for child_key, child_value in children:
                _emit(child_key, child_value, content_handler,
                      attr_prefix, cdata_key, depth+1, preprocessor,
                      pretty, newl, indent, namespaces=namespaces,
                      namespace_separator=namespace_separator,
                      expand_iter=expand_iter, encoding=encoding,
                      bytes_errors=bytes_errors, comment_key=comment_key)
        if cdata is not None and ordered_content is None:
            content_handler.characters(cdata)
        if pretty and children and ordered_content is None:
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
            content_key=DEFAULT_MIXED_CONTENT_KEY,
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

    Mixed content produced by ``parse(..., mixed_content=True)`` is accepted
    transparently: an ordered ``content_key`` (default ``'#content'``) list may
    contain text fragments (strings) and ``(tag, value)`` child pairs in any
    interleaving. Such an element may not also contain regular child keys or
    the ``cdata_key``.

    The `pretty` parameter (default=`False`) enables pretty-printing. In this
    mode, lines are terminated with `'\n'` and indented with `'\t'`, but this
    can be customized with the `newl` and `indent` parameters.
    The `bytes_errors` parameter controls decoding errors for byte values and
    defaults to `'replace'`.

    """
    bytes_errors = kwargs.pop('bytes_errors', 'replace')
    try:
        codecs.lookup_error(bytes_errors)
    except LookupError as exc:
        raise ValueError(f"Invalid bytes_errors handler: {bytes_errors}") from exc

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
            content_key=content_key,
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
