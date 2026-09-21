.. _parsing-witnesses:

============================================
Parsing witnesses: tokens, phases and trees
============================================

The tokenizer (``html5lib._tokenizer``) only emits tokens.  It never decides
which element a text node belongs to, whether a start tag is ignored, or how
misnested formatting tags are repaired.  Those decisions are made by the
insertion-mode phases in ``html5lib.html5parser`` together with three pieces
of state held by the tree builder (``html5lib.treebuilders.base.TreeBuilder``):

* ``openElements`` -- the stack of open elements;
* ``activeFormattingElements`` -- the list of active formatting elements,
  divided by markers;
* ``formPointer`` -- at most one open ``<form>`` element.

The concrete tree type only appears at the end: ``treebuilders/etree.py``,
``treebuilders/dom.py`` and ``treebuilders/etree_lxml.py`` consume the same
phase/state decisions and wrap them in different objects.

``html5lib/tests/test_parser_witnesses.py`` records this contract as
executable tests instead of snapshots of serialised HTML.

Token-by-token: how the phases switch
=====================================

Every token is dispatched by ``HTMLParser.mainLoop`` to the handler of the
current phase (``html5parser.py``).  With ``debug=True`` the parser logs
``(tokenizerState, insertionMode, dispatchedPhase, handler, tokenInfo)`` per
token.  Parsing::

  <!doctype html><title>t</title><table><tr><td>x</td></tr></table>

produces the following walk (asserted by
``test_phase_switch_token_walkthrough``):

#. ``initial`` handles the doctype, then moves to ``before html``.
#. The ``title`` start tag is reprocessed through ``before html`` (creates
   ``<html>``), ``before head`` (creates ``<head>``) and finally
   ``in head``, which inserts ``title`` and switches the tokenizer to RCDATA.
#. RCDATA characters and the ``title`` end tag run in the temporary
   ``text`` phase, which returns to ``in head``.
#. ``table`` in ``in head`` falls through to ``after head``; that creates
   ``<body>`` and reprocesses the token, so ``in body`` inserts the table and
   enters the ``in table`` phase.
#. ``<tr>`` in ``in table`` inserts an implied ``<tbody>`` and enters
   ``in table body``; that inserts the row and enters ``in row``.
#. ``<td>`` in ``in row`` inserts the cell and enters ``in cell``.  Text and
   the cell end tag are handled there; the ``</td>``, ``</tr>`` and
   ``</table>`` tokens unwind the phases back to ``in body``.

Characters inside a table (but outside a cell) take a detour: ``in table``
switches to the temporary ``in table text`` phase, which buffers character
tokens.  When the next non-character token arrives, non-whitespace is flushed
through foster parenting and the token is reprocessed in the original phase
(see ``InTableTextPhase`` and
``TreeBuilder.getTableMisnestedNodePosition``).

Where the state is read and written
===================================

The tests observe these branches directly with a thin tracing subclass
(``PhaseTracingParser``), which snapshots ``openElements``,
``activeFormattingElements`` and ``formPointer`` after every token:

* **Open element stack.**  Elements are pushed in ``TreeBuilder.insertElement``
  and popped by end-tag handlers and phase switches (e.g. leaving a cell, row
  or table body).  It is also scanned, never mutated, by
  ``elementInScope`` and the foster-parent position calculation.
* **Active formatting elements.**  Formatting start tags (``a``, ``b``,
  ``i``, ...) are appended by ``InBodyPhase.startTagFormatting``;
  ``reconstructActiveFormattingElements`` (``treebuilders/base.py``) rebuilds
  missing elements when text or another formatting start tag arrives; the
  adoption agency algorithm in ``InBodyPhase.endTagFormatting`` removes,
  clones and reopens entries when a formatting end tag is misnested.
  Entering a cell appends a ``Marker`` so that formatting opened outside a
  table is not reconstructed inside it.
* **Form pointer.**  ``InBodyPhase.startTagForm`` *reads* ``formPointer``: if
  it is non-null the new ``<form>`` token is a parse error
  ("unexpected-start-tag") and is discarded without touching the stack;
  otherwise the new element is inserted and the pointer is *written*.
  ``endTagForm`` reads and clears the pointer, then pops up to the form
  element.  The table-phases ``form`` handler keeps the pointer but inserts
  the form outside the table.

The minimal one-token-difference pairs
======================================

Each pair in ``WITNESS_PAIRS`` differs by exactly one token, and both the
canonical tree and the full ordered list of parse error codes are asserted,
so a change to the algorithm shows up as a structural or error-count
difference:

* **table text** -- adding one Characters token between table tags
  demonstrates foster parenting: the text appears before the table in the
  body.
* **mismatched b/i** -- swapping one end tag token (``</b></i>`` vs
  ``</i></b>``) runs the adoption agency algorithm; it emits
  ``adoption-agency-1.3``/``adoption-agency-1.2`` errors but reconstructs the
  exact tree of the well-formed pair.
* **nested form** -- adding one ``<form>`` start tag changes only the error
  list; the second token is discarded by the form-pointer branch.
* **template content** -- changing one start tag token from ``table`` to
  ``template`` changes which phases run.  This snapshot has no "in template"
  insertion mode, so the ``<tr>``/``<td>`` tokens are stray in-body tags and
  are ignored ("unexpected-start-tag-ignored") rather than building
  tbody/row/cell elements.  This is recorded as supported behaviour, not
  smoothed over.
* **EOF with unclosed elements** -- deleting one end tag leaves the tree
  unchanged (``InBodyPhase.processEOF`` reports the error and stops; the
  builder retains the element) but adds
  "expected-closing-tag-but-got-eof".

Canonical comparison model
==========================

Trees are compared as plain nested tuples::

  document  := ("document", doctype, (node, ...))
  doctype   := ("doctype", name, publicId, systemId) | None | UNOBSERVABLE
  element   := ("element", namespace | None, localName, attrs, children)
  attrs     := sorted tuple of (namespace | None, name, value)
  text      := ("text", data)
  comment   := ("comment", data)

The normalisations are deliberate and limited to non-semantic surface
differences:

* Clark notation (``{ns}local``) from both etree variants becomes explicit
  ``(namespace, localName)`` pairs;
* attribute dictionaries/``NamedNodeMap`` become sorted tuples, so source
  attribute order never breaks or hides a comparison;
* etree ``text``/``tail`` slots are folded into document-order children,
  matching how DOM stores text nodes;
* the etree ``fullTree`` ``DOCUMENT_ROOT`` wrapper and the lxml
  ``ElementTree`` wrapper are unwrapped into the same document tuple.

Recorded builder differences (not flattened)
============================================

* The default ``etree`` builder returns the ``<html>`` element, so the
  doctype and comments outside the root are genuinely unreachable; they map
  to ``UNOBSERVABLE``.  The ``dom`` builder keeps a document-type node and
  document-level comments.  ``fullTree=True`` etree exposes them under a
  ``DOCUMENT_ROOT`` and then matches dom.
* ``lxml`` cannot represent a doctype without a name: ``<!doctype>`` emits a
  ``DataLossWarning`` ("lxml cannot represent empty doctype") and the
  doctype is absent from the returned tree, while dom retains an
  empty-named document-type node.  The spec-required parse errors are the
  same on both paths.

Chunk boundaries
================

``ChunkedBytesIO`` feeds one input in fixed-size reads (1, 2, 3, 5, 7, 13, 64
and the whole stream).  These cuts only move tokenizer chunk boundaries;
the HTML data, including multi-byte UTF-8 characters split across reads, is
identical.  Every cut must produce the same canonical tree and the same
error records (positions included).

Complexity
==========

* The canonical conversion is linear in tree size per builder; one
  cross-builder comparison of *b* builders over a tree with *n* nodes costs
  O(bn) time and O(n) memory.
* ``PhaseTracingParser`` records O(t) constant-size rows for *t* tokens; the
  dynamic subclasses add no per-token object allocation beyond the rows.
* The chunk matrix runs O(c * m) parses for *c* chunk sizes over *m* inputs;
  the witnesses are deliberately a few dozen bytes each, so the full module
  stays well below a second.

Compatibility
=============

* No network, clock waits or filesystem traversal are used; all inputs are
  inline literals.
* ``lxml`` is optional: its witnesses are collected when the module imports
  and skipped with a reason otherwise; ``etree`` and ``dom`` always run on
  the standard library.
* The module targets the same Python 2/3 range as the rest of html5lib and
  changes no parsing behaviour -- parse error codes and counts are part of
  the asserted output, so they cannot be altered by these tests.
