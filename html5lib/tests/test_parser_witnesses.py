"""Executable parsing witnesses: tokens -> phases -> builder trees.

The tokenizer (``html5lib._tokenizer``) only emits tokens.  The final tree is
the product of the parser phases (``html5lib.html5parser``), the open element
stack, the active formatting element list and the form pointer
(``html5lib.treebuilders.base.TreeBuilder``), finally rendered into a concrete
tree type by one of the three built-in tree builders (``etree``, ``dom``,
``lxml``).

This module records that behaviour as executable witnesses:

* :class:`PhaseTracingParser` snapshots the phase and the three parser state
  variables after every token, so phase switches and state read/writes are
  observable without relying on serialised HTML.
* The canonical comparison model below maps each builder's concrete output
  onto one set of plain tuples so structural semantics can be compared instead
  of pretty-printed HTML strings.

Canonical comparison model
--------------------------

::

  document  := ("document", doctype, (node, ...))
  doctype   := ("doctype", name, publicId, systemId) | None | UNOBSERVABLE
  node      := element | text | comment
  element   := ("element", namespace | None, localName, attrs, (node, ...))
  attrs     := tuple, sorted by (namespace, name), of
               (namespace | None, name, value)
  text      := ("text", data)
  comment   := ("comment", data)

``UNOBSERVABLE`` means the information is genuinely not reachable from the
builder's public return value (the default etree builder returns the
``<html>`` element, so the doctype and comments outside the root cannot be
seen).  Those differences are recorded, not faked.  Attribute order is
normalised away (HTML/DOM attributes are unordered), Clark notation
(``{namespace}local``) is split into ``(namespace, local)`` and the etree
``text``/``tail`` slots are folded into document-order child nodes.
"""
from __future__ import absolute_import, division, unicode_literals

import difflib
import pprint
import warnings
from io import BytesIO

import pytest

import html5lib
from html5lib import _tokenizer
from html5lib.constants import tokenTypes
from html5lib import treebuilders
from html5lib.constants import DataLossWarning, namespaces
from html5lib.treebuilders.base import Marker
from xml.dom import Node
from xml.etree import ElementTree


HTML_NS = namespaces["html"]

# A value the returned tree cannot represent (e.g. the doctype as seen by the
# default etree builder).  Not None: None means "no doctype was parsed".
UNOBSERVABLE = ("unobservable",)


# ---------------------------------------------------------------------------
# Canonical model constructors (used for readable expected values)
# ---------------------------------------------------------------------------

def document(*children, **kwargs):
    return ("document", kwargs.pop("doctype", None), tuple(children))


def element(name, *children, **kwargs):
    ns = kwargs.pop("ns", HTML_NS)
    attrs = kwargs.pop("attrs", ())
    assert not kwargs
    return ("element", ns, name, tuple(sorted(attrs)), tuple(children))


def doctype(name, public_id="", system_id=""):
    return ("doctype", name, public_id, system_id)


def text(data):
    return ("text", data)


def comment(data):
    return ("comment", data)


# ---------------------------------------------------------------------------
# Concrete tree -> canonical model
# ---------------------------------------------------------------------------

def _split_clark_notation(tag):
    """``{namespace}local`` -> ``(namespace, local)``, else ``(None, tag)``."""
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return None, tag


def _from_etree_node(node):
    if node.tag is ElementTree.Comment:
        return comment(node.text)
    namespace, local = _split_clark_notation(node.tag)
    attrs = []
    for key, value in node.attrib.items():
        attr_ns, attr_name = _split_clark_notation(key)
        attrs.append((attr_ns, attr_name, value))
    children = []
    if node.text:
        children.append(text(node.text))
    for child in node:
        children.append(_from_etree_node(child))
        if child.tail:
            children.append(text(child.tail))
    return element(local, *children, ns=namespace, attrs=attrs)


def _from_etree(doc):
    # The default etree builder returns the <html> element itself; the
    # doctype and comments outside the root are not reachable.
    return document(_from_etree_node(doc), doctype=UNOBSERVABLE)


def _from_dom_node(node):
    if node.nodeType == Node.ELEMENT_NODE:
        attrs = []
        for index in range(node.attributes.length):
            attr = node.attributes.item(index)
            attrs.append((attr.namespaceURI, attr.localName or attr.name,
                          attr.value))
        children = [_from_dom_node(child) for child in node.childNodes]
        return element(node.localName or node.tagName, *children,
                       ns=node.namespaceURI, attrs=attrs)
    if node.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE):
        return text(node.data)
    if node.nodeType == Node.COMMENT_NODE:
        return comment(node.data)
    raise AssertionError("unexpected DOM node type %r" % (node.nodeType,))


def _from_dom(doc):
    dt = None
    children = []
    for node in doc.childNodes:
        if node.nodeType == Node.DOCUMENT_TYPE_NODE:
            dt = doctype(node.name or "", node.publicId or "",
                         node.systemId or "")
        else:
            children.append(_from_dom_node(node))
    return document(*children, doctype=dt)


def _from_lxml_tree(etree_module):
    def _from_lxml_node(node):
        if isinstance(node, etree_module._Comment):
            return comment(node.text)
        namespace, local = _split_clark_notation(node.tag)
        attrs = []
        for key, value in node.attrib.items():
            attr_ns, attr_name = _split_clark_notation(key)
            attrs.append((attr_ns, attr_name, value))
        children = []
        if node.text:
            children.append(text(node.text))
        for child in node:
            children.append(_from_lxml_node(child))
            if child.tail:
                children.append(text(child.tail))
        return element(local, *children, ns=namespace, attrs=attrs)

    def _convert(doc):
        info = doc.docinfo
        # The doctype name is only exposed on the internal DTD object;
        # public/system ids live on docinfo.
        if info.internalDTD is not None:
            dt = doctype(info.internalDTD.name or "",
                         info.public_id or "", info.system_url or "")
        else:
            dt = None
        root = doc.getroot()
        top_level = list(root.itersiblings(preceding=True))[::-1]
        top_level.append(root)
        top_level.extend(root.itersiblings())
        return document(*[_from_lxml_node(node) for node in top_level],
                        doctype=dt)

    return _convert


CANONICALIZERS = {"etree": _from_etree, "dom": _from_dom}
try:
    from lxml import etree as _lxml_etree
except ImportError:  # lxml is an optional dependency
    _lxml_etree = None
else:
    CANONICALIZERS["lxml"] = _from_lxml_tree(_lxml_etree)

ALL_BUILDERS = tuple(CANONICALIZERS)
HAS_LXML = _lxml_etree is not None


def parse_record(html, builder="etree", namespace=True, source=None):
    """Parse *html* and return ``(canonical_tree, error_records)``.

    *source* (a file-like object) replaces *html* when given, which is how
    the chunk-boundary witnesses feed the tokenizer.
    """
    parser = html5lib.HTMLParser(
        tree=treebuilders.getTreeBuilder(builder),
        namespaceHTMLElements=namespace)
    data = html if source is None else source
    kwargs = {}
    if source is not None:
        # Chunked byte streams carry no transport encoding; pin UTF-8 so the
        # witness isolates chunk boundaries, not encoding detection.
        kwargs["override_encoding"] = "utf-8"
    doc = parser.parse(data, **kwargs)
    return CANONICALIZERS[builder](doc), list(parser.errors)


def error_codes(errors):
    return [code for position, code, datavars in errors]


# ---------------------------------------------------------------------------
# Failure reporting
# ---------------------------------------------------------------------------

def assert_canonical_equal(expected, actual, context):
    if expected == actual:
        return
    expected_text = pprint.pformat(expected, width=110)
    actual_text = pprint.pformat(actual, width=110)
    diff = "\n".join(difflib.unified_diff(
        expected_text.splitlines(), actual_text.splitlines(),
        fromfile="expected", tofile="actual", lineterm=""))
    pytest.fail("canonical trees differ for %s:\n%s" % (context, diff))


# ---------------------------------------------------------------------------
# Phase / state tracing: openElements, activeFormattingElements, formPointer
# ---------------------------------------------------------------------------

_TRACED_HANDLERS = (
    "processStartTag", "processEndTag", "processCharacters",
    "processSpaceCharacters", "processComment", "processDoctype",
    "processEOF")


class PhaseTracingParser(html5lib.HTMLParser):
    """HTMLParser that records a state snapshot after every token.

    Each trace row is
    ``(phase, handler, token_name_or_type, open_elements, form_pointer, afe)``
    with the latter three reduced to plain strings, so witnesses can assert
    exactly which branch read or mutated which piece of parser state.
    """

    def __init__(self, **kwargs):
        html5lib.HTMLParser.__init__(self, **kwargs)
        self.trace = []
        # Phases use __slots__, so handlers cannot be patched per instance.
        # Build a thin tracing subclass for every phase class and rebuild the
        # registry with it; parser.reset() only touches self.phase, which
        # always points into this registry.
        traced_classes = {}
        for name, phase in self.phases.items():
            base_class = type(phase)
            if base_class not in traced_classes:
                traced_classes[base_class] = self._make_traced(base_class)
        self.phases = {
            name: traced_classes[type(phase)](self, self.tree)
            for name, phase in self.phases.items()}

    def _make_traced(self, base_class):
        parser = self

        def make_handler(handler_name):
            def traced(self, token=None):
                if handler_name == "processEOF":
                    result = super(traced_class, self).processEOF()
                else:
                    method = getattr(super(traced_class, self), handler_name)
                    result = method(token)
                tree = parser.tree
                open_names = tuple(element.name
                                   for element in tree.openElements)
                form_name = (tree.formPointer.name
                             if tree.formPointer is not None else None)
                afe = tuple("Marker" if entry is Marker else entry.name
                            for entry in tree.activeFormattingElements)
                label = None
                if token is not None:
                    label = token.get("name", token["type"])
                parser.trace.append(
                    (base_class.__name__, handler_name, label,
                     open_names, form_name, afe))
                return result
            return traced

        namespace = {}
        for handler_name in _TRACED_HANDLERS:
            namespace[handler_name] = make_handler(handler_name)
        traced_class = type(str("Traced" + base_class.__name__),
                            (base_class,), namespace)
        return traced_class

    def rows(self, handler=None, label=None, phase=None):
        rows = self.trace
        if handler is not None:
            rows = [row for row in rows if row[1] == handler]
        if label is not None:
            rows = [row for row in rows if row[2] == label]
        if phase is not None:
            rows = [row for row in rows if row[0] == phase]
        return rows


class ChunkedBytesIO(BytesIO):
    """Byte stream that pretends the OS handed over at most ``chunk`` bytes.

    This tests the tokenizer's chunk boundaries; the HTML data is unchanged.
    """

    def __init__(self, data, chunk):
        BytesIO.__init__(self, data)
        self.chunk = chunk

    def read(self, size=-1):
        if (self.chunk is not None and
                (size is None or size < 0 or size > self.chunk)):
            size = self.chunk
        return BytesIO.read(self, size)


# ---------------------------------------------------------------------------
# Witnesses: phase switches explained by token
# ---------------------------------------------------------------------------

def test_phase_switch_token_walkthrough():
    """Each token is dispatched against the current insertion mode.

    ``HTMLParser.debug`` records ``(tokenizer_state, insertion_mode,
    dispatched_phase, handler, token_info)``; the insertion mode column
    below documents how the phases walk from initial -> ... -> in cell.
    """
    parser = html5lib.HTMLParser(debug=True)
    parser.parse("<!doctype html><title>t</title>"
                 "<table><tr><td>x</td></tr></table>")
    insertion_modes = [(entry[1], entry[4].get("name", entry[4]["type"]))
                       for entry in parser.log]
    assert insertion_modes == [
        ("InitialPhase", "Doctype"),
        ("BeforeHtmlPhase", "title"),
        ("BeforeHeadPhase", "title"),
        ("InHeadPhase", "title"),       # title switches to TextPhase
        ("TextPhase", "Characters"),    # rcdata text inside <title>
        ("TextPhase", "title"),         # end title returns to InHeadPhase
        ("InHeadPhase", "table"),
        ("AfterHeadPhase", "table"),    # create body, then reprocess table
        ("InBodyPhase", "table"),       # insert table, enter InTablePhase
        ("InTablePhase", "tr"),         # implies tbody, enter InTableBody
        ("InTableBodyPhase", "tr"),     # insert tr, enter InRowPhase
        ("InRowPhase", "td"),           # insert td, enter InCellPhase
        ("InCellPhase", "Characters"),
        ("InCellPhase", "td"),          # close td, back to InRowPhase
        ("InRowPhase", "tr"),           # close tr, back to InTableBody
        ("InTableBodyPhase", "table"),  # close tbody, back to InTable
        ("InTablePhase", "table")]


def test_table_text_goes_through_in_table_text_phase():
    """Character tokens between table elements switch to the table text
    phase; when non-whitespace is seen they are flushed for foster
    parenting and control returns to the original phase."""
    parser = html5lib.HTMLParser(debug=True)
    parser.parse("<table>foo<td>bar</td></table>")
    entries = [(entry[1], entry[3], entry[4].get("name",
                                                 entry[4]["type"]))
               for entry in parser.log]
    # Characters are buffered in InTableTextPhase; the next token (<td>)
    # arrives there, triggers flushCharacters (foster parenting) and returns
    # to InTablePhase before the start tag is reprocessed.
    expected_index = entries.index(
        ("InTableTextPhase", "processStartTag", "td"))
    assert entries[expected_index - 1] == (
        "InTablePhase", "processCharacters", "Characters")
    assert entries[expected_index + 1] == (
        "InTablePhase", "processStartTag", "td")


# ---------------------------------------------------------------------------
# Witnesses: openElements / activeFormattingElements / formPointer
# ---------------------------------------------------------------------------

def test_form_pointer_is_set_read_and_cleared():
    parser = PhaseTracingParser()
    parser.parse("<form><div><form><input></form>")

    first_form = parser.rows("processStartTag", "form", "InBodyPhase")[0]
    # startTagForm (InBodyPhase) writes formPointer to the new element.
    assert first_form[3] == ("html", "body", "form")
    assert first_form[4] == "form"

    second_form = parser.rows("processStartTag", "form", "InBodyPhase")[1]
    # The nested <form> token hits the "formPointer is non-null" branch:
    # the stack is not extended and formPointer is left untouched.
    assert second_form[3] == ("html", "body", "form", "div")
    assert second_form[4] == "form"
    assert error_codes(parser.errors) == [
        "expected-doctype-but-got-start-tag",
        "unexpected-start-tag",
        "end-tag-too-early-ignored",
        "expected-closing-tag-but-got-eof"]

    end_form = parser.rows("processEndTag", "form", "InBodyPhase")[0]
    # endTagForm reads the pointer, clears it, then pops up to that element.
    assert end_form[4] is None
    assert end_form[3] == ("html", "body", "div")


def test_active_formatting_elements_drive_adoption_agency():
    parser = PhaseTracingParser()
    parser.parse("<b><i>x</b></i>")

    start_b = parser.rows("processStartTag", "b", "InBodyPhase")[0]
    start_i = parser.rows("processStartTag", "i", "InBodyPhase")[0]
    assert start_b[5] == ("b",)
    assert start_i[5] == ("b", "i")

    # </b> with <i> above it runs the adoption agency algorithm: both
    # elements leave the open element stack, but <i> stays in the active
    # formatting element list and is reconstructed on demand.
    end_b = parser.rows("processEndTag", "b", "InBodyPhase")[0]
    assert end_b[3] == ("html", "body")
    assert end_b[5] == ("i",)

    end_i = parser.rows("processEndTag", "i", "InBodyPhase")[0]
    assert end_i[5] == ()
    assert [code for code in error_codes(parser.errors)
            if code.startswith("adoption-agency")] == [
        "adoption-agency-1.3", "adoption-agency-1.2"]


def test_table_phases_append_the_formatting_marker():
    parser = PhaseTracingParser()
    parser.parse("<b><table><td>x")
    # Entering a cell appends a marker to activeFormattingElements, so
    # formatting opened outside the table is reconstructed outside it.
    start_td = [row for row in parser.rows("processStartTag", "td")
                if row[0] == "InRowPhase"][0]
    assert start_td[5] == ("b", "Marker")


def test_eof_closes_unclosed_elements():
    parser = PhaseTracingParser()
    parser.parse("<b>x")
    # InBodyPhase.processEOF reports the error and stops; the tree builder
    # keeps the element, which still yields the same document tree.
    eof_row = parser.rows("processEOF", phase="InBodyPhase")[-1]
    assert eof_row[3] == ("html", "body", "b")
    assert error_codes(parser.errors) == [
        "expected-doctype-but-got-start-tag",
        "expected-closing-tag-but-got-eof"]
    tree, _ = parse_record("<b>x")
    assert_canonical_equal(
        tree, skeleton([element("b", text("x"))]), "<b>x EOF tree")


# ---------------------------------------------------------------------------
# Minimal one-token-difference HTML pairs
# ---------------------------------------------------------------------------
# Every pair below differs by exactly one token; that token is named in the
# case id and comments.

def skeleton(body_children, dt=None):
    body = element("body", *body_children)
    # Witnesses use the default (non-fullTree) etree builder in the pair
    # tests, for which document-level nodes are UNOBSERVABLE.
    return document(element("html", element("head"), body),
                    doctype=UNOBSERVABLE if dt is None else dt)


_DOCTYPE_ERROR = "expected-doctype-but-got-start-tag"


WITNESS_PAIRS = {
    # Second side adds a single Characters token "foo" between table tags:
    # foster parenting moves it ahead of the table in body.
    "table_text": (
        ("<table><td>bar</td></table>",
         skeleton([element("table", element(
             "tbody", element("tr", element("td", text("bar")))))]),
         [_DOCTYPE_ERROR, "unexpected-cell-in-table-body"]),
        ("<table>foo<td>bar</td></table>",
         skeleton([text("foo"), element("table", element(
             "tbody", element("tr", element("td", text("bar")))))]),
         [_DOCTYPE_ERROR, "unexpected-cell-in-table-body"])),
    # Second side changes one EndTag token (</b> -> </b> stays in A;
    # B swaps the closing tokens around): the adoption agency algorithm
    # reconstructs the exact tree produced by the well-formed pair.
    "mismatched_b_i": (
        ("<b><i>x</i></b>",
         skeleton([element("b", element("i", text("x")))]),
         [_DOCTYPE_ERROR]),
        ("<b><i>x</b></i>",
         skeleton([element("b", element("i", text("x")))]),
         [_DOCTYPE_ERROR, "adoption-agency-1.3", "adoption-agency-1.2"])),
    # Second side adds one <form> start tag token: it is discarded while
    # formPointer is set, so the trees are identical.
    "nested_form": (
        ("<form><input></form>",
         skeleton([element("form", element("input"))]),
         [_DOCTYPE_ERROR]),
        ("<form><form><input></form>",
         skeleton([element("form", element("input"))]),
         [_DOCTYPE_ERROR, "unexpected-start-tag"])),
    # Second side changes one StartTag token (table -> template): this
    # snapshot has no "in template" insertion mode, so the tr/td tokens are
    # handled as stray in-body tags instead of building a table.
    "template_content": (
        ("<table><tr><td>x",
         skeleton([element("table", element(
             "tbody", element("tr", element("td", text("x")))))]),
         [_DOCTYPE_ERROR, "expected-closing-tag-but-got-eof"]),
        ("<template><tr><td>x",
         skeleton([element("template", text("x"))]),
         [_DOCTYPE_ERROR, "unexpected-start-tag-ignored",
          "unexpected-start-tag-ignored",
          "expected-closing-tag-but-got-eof"])),
    # Second side deletes one EndTag token: EOF closes the element, so the
    # trees are identical but B records the expected-closing parse error.
    "eof_unclosed": (
        ("<b>x</b>",
         skeleton([element("b", text("x"))]),
         [_DOCTYPE_ERROR]),
        ("<b>x",
         skeleton([element("b", text("x"))]),
         [_DOCTYPE_ERROR, "expected-closing-tag-but-got-eof"])),
}


@pytest.mark.parametrize("case_id,side", [
    (case_id, side) for case_id in WITNESS_PAIRS for side in (0, 1)])
def test_minimal_token_pairs(case_id, side):
    html, expected_tree, expected_errors = WITNESS_PAIRS[case_id][side]
    actual_tree, errors = parse_record(html)
    assert_canonical_equal(
        expected_tree, actual_tree,
        "%s side %d: %r" % (case_id, side, html))
    assert error_codes(errors) == expected_errors, (
        "%s side %d parse errors: %r" % (case_id, side,
                                         error_codes(errors)))


@pytest.mark.parametrize("case_id", sorted(WITNESS_PAIRS))
def test_pair_sides_differ_by_one_token_only(case_id):
    # Guard against accidental drift: tokenise both sides and require the
    # sequences to differ at exactly one position (or by one inserted token).
    pair = WITNESS_PAIRS[case_id]
    html_a = pair[0][0]
    html_b = pair[1][0]
    tokens_a = list(_tokenizer.HTMLTokenizer(html_a))
    tokens_b = list(_tokenizer.HTMLTokenizer(html_b))

    def signature(token):
        return (token["type"], token.get("name"), token.get("data"),
                token.get("selfClosing"))
    sig_a = [signature(token) for token in tokens_a
             if token["type"] != tokenTypes["ParseError"]]
    sig_b = [signature(token) for token in tokens_b
             if token["type"] != tokenTypes["ParseError"]]
    if len(sig_a) == len(sig_b):
        differences = [i for i in range(len(sig_a))
                       if sig_a[i] != sig_b[i]]
        if len(differences) == 2:
            first, second = differences
            # The mismatched b/i pair swaps two adjacent end-tag tokens.
            assert second - first == 1, (case_id, sig_a, sig_b)
            assert sig_a[first] == sig_b[second], (case_id, sig_a, sig_b)
            assert sig_a[second] == sig_b[first], (case_id, sig_a, sig_b)
        else:
            assert len(differences) == 1, (case_id, sig_a, sig_b)
    else:
        # Insertion/deletion of a single token (the table-text Characters
        # token and the extra <form> start tag).
        assert abs(len(sig_a) - len(sig_b)) == 1, (case_id, sig_a, sig_b)
        shorter, longer = sorted((sig_a, sig_b), key=len)
        for deletion in range(len(longer)):
            candidate = longer[:deletion] + longer[deletion + 1:]
            if candidate == shorter:
                break
        else:
            pytest.fail("%s is not a one-token edit:\n%r\n%r" % (
                case_id, sig_a, sig_b))


# ---------------------------------------------------------------------------
# Cross-builder structural contract
# ---------------------------------------------------------------------------

# Inputs exercising document-level nodes, foreign content, attribute
# adjustment and foster parenting.
CROSS_BUILDER_INPUTS = [
    ("minimal", "<!doctype html>"),
    ("comments",
     "<!--before--><!doctype html><p>a<!--mid-->b</p><!--after-->"),
    ("foster", "<!doctype html><table>foo<td>bar</td></table>"),
    ("foreign",
     '<!doctype html><svg viewBox="0 0 1 1"><path d="M0 0"/></svg>'),
    ("attributes", '<!doctype html><p class="x" id="y" title="a&amp;b">z</p>'),
]


@pytest.mark.parametrize("label,html", CROSS_BUILDER_INPUTS)
def test_builders_agree_on_structure(label, html):
    trees = {}
    errors = {}
    for builder in ALL_BUILDERS:
        trees[builder], recorded = parse_record(html, builder=builder)
        errors[builder] = error_codes(recorded)

    # The <html> subtree must be identical for every builder.
    roots = {builder: canonical[2][-1] for builder, canonical in trees.items()}
    reference_builder = sorted(roots)[0]
    reference = roots[reference_builder]
    for builder, root in roots.items():
        assert_canonical_equal(
            reference, root,
            "%r: %s vs %s" % (html, reference_builder, builder))

    # Document-level comments and the doctype only where a builder genuinely
    # exposes them; default etree hides both on purpose.
    doctypes = set(canonical[1] for canonical in trees.values()
                   if canonical[1] is not UNOBSERVABLE)
    assert len(doctypes) == 1, trees
    extra = {
        builder: tuple(node for node in canonical[2]
                       if node is not roots[builder])
        for builder, canonical in trees.items()}
    observant = {builder: nodes for builder, nodes in extra.items()
                 if builder != "etree"}
    if observant:
        reference_nodes = observant[sorted(observant)[0]]
        for builder, nodes in observant.items():
            assert nodes == reference_nodes, (builder, html, nodes)

    # Parse error streams are a parser concern; builders must not change them.
    reference_errors = errors[reference_builder]
    for builder, recorded_errors in errors.items():
        assert recorded_errors == reference_errors, (builder, html)


@pytest.mark.parametrize("builder", ALL_BUILDERS)
def test_attribute_order_is_not_semantic(builder):
    html_a = '<!doctype html><p a="1" b="2">x</p>'
    html_b = '<!doctype html><p b="2" a="1">x</p>'
    tree_a, _ = parse_record(html_a, builder=builder)
    tree_b, _ = parse_record(html_b, builder=builder)
    assert_canonical_equal(tree_a, tree_b,
                           "attribute order for " + builder)


@pytest.mark.parametrize("builder", ALL_BUILDERS)
def test_namespacing_agreement(builder):
    namespaced, _ = parse_record("<p>x</p>", builder=builder, namespace=True)
    plain, _ = parse_record("<p>x</p>", builder=builder, namespace=False)

    def strip_ns(node):
        kind = node[0]
        if kind != "element":
            return node
        _, _, name, attrs, children = node
        return ("element", None, name,
                tuple((None, attr_name, value)
                      for _, attr_name, value in attrs),
                tuple(strip_ns(child) for child in children))
    assert strip_ns(namespaced[2][0]) == plain[2][0]
    # With namespaceHTMLElements=True every builder reports the HTML ns.
    assert namespaced[2][0][1] == HTML_NS
    assert plain[2][0][1] is None


# ---------------------------------------------------------------------------
# Tokenizer chunk boundaries must not change the tree
# ---------------------------------------------------------------------------

CHUNK_INPUTS = [
    "<table>foo<td>bar</td></table>",
    "<b><i>x</b></i>",
    "<form><form><input></form>",
    "<template><tr><td>x",
    # multi-byte UTF-8 characters and a numeric entity that may be split
    "<p>caf\u00e9 \u65e5\u672c &#x1F600; &amp;</p>",
]


@pytest.mark.parametrize("html", CHUNK_INPUTS)
@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 13, 64, None])
def test_chunk_boundaries_are_invisible(html, chunk):
    reference_tree, reference_errors = parse_record(html)
    stream = ChunkedBytesIO(html.encode("utf-8"), chunk)
    chunked_tree, chunked_errors = parse_record(html, source=stream)
    assert_canonical_equal(
        reference_tree, chunked_tree,
        "chunk=%d: %r" % (-1 if chunk is None else chunk, html))
    # Positions and codes are part of the parse record too.
    assert chunked_errors == reference_errors, (chunk, html)


# ---------------------------------------------------------------------------
# Supported, documented differences between builders (recorded, not fixed)
# ---------------------------------------------------------------------------

def test_etree_default_hides_document_level_nodes():
    # The default etree builder returns <html>: doctype and comments outside
    # the root are outside the return type.  This is a supported API shape,
    # normalised to UNOBSERVABLE in the comparison model.
    html = "<!--pre--><!doctype html><p>x</p>"
    etree_doc, _ = parse_record(html, "etree")
    assert etree_doc[1] is UNOBSERVABLE
    assert etree_doc[2] == (element(
        "html", element("head"), element("body", element("p", text("x")))),)

    dom_doc, _ = parse_record(html, "dom")
    assert dom_doc[1] == doctype("html")
    assert dom_doc[2][0] == comment("pre")
    assert dom_doc[2][1:] == etree_doc[2]


def _from_etree_full_tree(doc_root):
    """Canonicalise an etree ``fullTree`` DOCUMENT_ROOT wrapper."""
    dt = None
    children = []
    for child in doc_root:
        if child.tag is ElementTree.Comment:
            children.append(comment(child.text))
        elif child.tag == "<!DOCTYPE>":
            dt = doctype(child.text or "",
                         child.get("publicId") or "",
                         child.get("systemId") or "")
        else:
            children.append(_from_etree_node(child))
        if child.tail:
            children.append(text(child.tail))
    return document(*children, doctype=dt)


def test_etree_full_tree_exposes_document_level_nodes():
    # fullTree wraps document children under a DOCUMENT_ROOT element; the
    # comparison model unwraps it and matches the dom representation.
    from xml.etree import ElementTree
    html = "<!--pre--><!doctype html><p>x</p>"
    full_builder = treebuilders.getTreeBuilder("etree", ElementTree,
                                               fullTree=True)
    parser = html5lib.HTMLParser(tree=full_builder)
    full_doc = _from_etree_full_tree(parser.parse(html))

    dom_doc, _ = parse_record(html, "dom")
    assert_canonical_equal(dom_doc, full_doc, "fullTree etree vs dom")


@pytest.mark.skipif(not HAS_LXML, reason="lxml is not installed")
def test_lxml_empty_doctype_is_reported_as_data_loss():
    # libxml2 cannot represent a doctype without a name.  html5lib emits a
    # DataLossWarning and drops it; the DOM builder keeps the node.  The
    # spec-required parse errors are unchanged on either path.
    html = "<!doctype>"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lxml_doc, lxml_errors = parse_record(html, "lxml")
    assert any(issubclass(item.category, DataLossWarning)
               for item in caught)
    assert lxml_doc[1] is None

    dom_doc, dom_errors = parse_record(html, "dom")
    assert dom_doc[1] == doctype("")
    assert error_codes(lxml_errors) == error_codes(dom_errors)
