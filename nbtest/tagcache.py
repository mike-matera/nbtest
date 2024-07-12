"""
The implementation of a cell tag cache.
"""

import ast
import io
import re
import sys
import types
import typing
import unittest
from dataclasses import dataclass
from typing import Any, Mapping, Set, Union

from IPython.core.interactiveshell import ExecutionResult, InteractiveShell
from IPython.core.magic import Magics, cell_magic, magics_class
from IPython.display import HTML

from .analysis import TopLevelDefines
from .templ import templ
from .transforms import RewriteVariableAssignments
from .unit import NotebookTestRunner

nbtest_attrs = {}
runner_class = NotebookTestRunner


@dataclass
class CellRunResult:
    """The result of calling run() on a TagCacheEntry"""

    stdout: str
    stderr: str
    outputs: list[Any]
    result: Any


@magics_class
class TagCache(Magics):
    """
    A stateful cell magic and event handler that maintains a cache of executed
    cells that is used to run unit tests on code in different cells.
    """

    def __init__(self, shell: InteractiveShell):
        """Initialize the plugin."""
        super().__init__(shell)
        self._cache = {}
        self._test_ns = {"shell": self.shell}

    @cell_magic
    def testing(self, line: str, cell: str) -> HTML:
        """
        A cell magic that finds and runs unit tests on selected symbols.
        """
        global nbtest_attrs

        self._test_ns["nbtest_cases"] = None
        nbtest_attrs.clear()

        # Find the symbols mentioned in the cell magic
        try:
            for attr, value in (
                (
                    x[1:],
                    self._cache[x],
                )
                if x.startswith("@")
                else (
                    x,
                    self.shell.user_ns[x],
                )
                for x in line.split()
            ):
                self._test_ns[attr] = value
                nbtest_attrs[attr] = value
        except KeyError as e:
            return HTML(templ.missing.render(missing=e))

        # Run the cell
        try:
            tree = ast.parse(cell)
            exec(compile(tree, filename="<testing>", mode="exec"), self._test_ns)
        except AssertionError as e:
            return HTML(templ.assertion.render(error=e))

        # Find and execute test cases.
        suite = unittest.TestSuite()

        if self._test_ns["nbtest_cases"] is not None:
            # Test cases are specified
            for tc in self._test_ns["nbtest_cases"]:
                match tc:
                    case str():
                        suite.addTest(unittest.defaultTestLoader.loadTestsFromName(tc))
                    case _ if isinstance(tc, unittest.TestCase) or isinstance(
                        tc, unittest.TestSuite
                    ):
                        suite.addTest(tc)
                    case type() if issubclass(tc, unittest.TestCase):
                        suite.addTest(unittest.defaultTestLoader.loadTestsFromTestCase(tc))
                    case types.ModuleType():
                        suite.addTest(unittest.defaultTestLoader.loadTestsFromModule(tc))
                    case _:
                        raise ValueError(
                            f"Invalid value in test_cases: {tc}. Entries must be a string, TestCase class or instance, a TestSuite, or a module."
                        )

        else:
            # Look for cases in the cell
            class top_test_visitor(ast.NodeVisitor):
                """Parse the input cell to find top level test class and test function defs"""

                def visit_ClassDef(_, node):
                    if isinstance(self._test_ns[node.name], type) and issubclass(
                        self._test_ns[node.name], unittest.TestCase
                    ):
                        suite.addTest(
                            unittest.defaultTestLoader.loadTestsFromTestCase(
                                self._test_ns[node.name]
                            )
                        )
                    # do not descend

                def visit_FunctionDef(_, node):
                    if node.name.startswith("test") and callable(self._test_ns[node.name]):
                        suite.addTest(unittest.FunctionTestCase(self._test_ns[node.name]))
                    # do not descend

            top_test_visitor().visit(tree)

        # Run tests
        runner = runner_class()
        result = runner.run(suite)
        return HTML(templ.result.render(result=result))

    def post_run_cell(self, result):
        """
        Callback after a cell has run.
        """
        if result.execution_count is not None:
            # Avoid caching on run().
            entry = TagCacheEntry(result, self.shell)
            for tag in entry.tags:
                self._cache[tag] = entry


class TagCacheEntry:
    """
    Information about an executed cell.
    """

    def __init__(self, result, shell):
        """Create an entry."""

        self._id = result.info.cell_id
        self._result = result
        self._shell = shell
        self._source = shell.transform_cell(result.info.raw_cell)

        try:
            self._tree = ast.parse(self.source)
            self._docstring = ast.get_docstring(self.tree)
            self._tokens = set((x.__class__ for x in ast.walk(self.tree)))

        except SyntaxError:
            self._tree = None
            self._docstring = None
            self._tokens = None

        if self._docstring is not None:
            self._tags = [
                m.group(1)
                for x in self._docstring.split("\n")
                if (m := re.match(r"(@\S+)", x.strip())) is not None
            ]
        else:
            self._tags = []

    @property
    def id(self) -> str:
        """The unique identifier of the notebook cell."""
        return self._id

    @property
    def result(self) -> ExecutionResult:
        """The ExecutionResult from running the cell in IPython."""
        return self._result

    @property
    def source(self) -> str:
        """The processed source of the cell."""
        return self._source

    @property
    def tree(self) -> ast.Module:
        """The parse tree generated from parsing the cell source. See ast.parse()"""
        return self._tree

    @property
    def docstring(self) -> Union[str, None]:
        """The docstring of the cell or `None` if there is no docstring."""
        return self._docstring

    @property
    def tokens(self) -> Set:
        """A set of token classes from the parsed source."""
        return self._tokens

    @property
    def tags(self) -> Set[str]:
        """A set of the tags found in the cell."""
        return set(self._tags)

    def _find(self, ntype: list[ast.AST]) -> list[typing.Any]:
        tlds = TopLevelDefines()
        tlds.visit(self.tree)
        return {x[0]: self._shell.user_ns[x[0]] for x in tlds.defs if x[1] in ntype}

    @property
    def functions(self) -> list[types.FunctionType]:
        """
        Find all package level functions in a cell.
        """
        return self._find([ast.FunctionDef])

    @property
    def classes(self) -> list[type]:
        """
        Find all"""
        return self._find([ast.ClassDef])

    @property
    def assignments(self) -> list[type]:
        return self._find([ast.Assign])

    def run(self, push: Mapping = {}, capture: bool = True) -> Union[CellRunResult, None]:
        """
        Run the contents of a cached cell.

        push: Update variables in the notebook namespace with names and values in `push` before running the contents.
        capture: Set to `True` (the default) to capture stdout, stderr and output. If `False` run() returns `None`
        """
        self._shell.push(push)
        try:
            save_out = sys.stdout
            save_err = sys.stderr
            save_dh = sys.displayhook

            out = io.StringIO()
            err = io.StringIO()
            outputs = []

            def dh(obj):
                nonlocal outputs
                outputs.append(obj)

            if capture:
                sys.stdout = out
                sys.stderr = err
                sys.displayhook = dh

            transformer = RewriteVariableAssignments(*list(push.keys()))
            self._shell.ast_transformers.append(transformer)
            self._shell.run_cell(self._source, store_history=False, silent=False)

            if capture:
                return CellRunResult(
                    stdout=out.getvalue(),
                    stderr=err.getvalue(),
                    outputs=outputs,
                    result=outputs[-1],
                )
            else:
                return None

        finally:
            sys.stdout = save_out
            sys.stderr = save_err
            sys.displayhook = save_dh
            self._shell.ast_transformers.remove(transformer)