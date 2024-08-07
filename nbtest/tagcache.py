"""
The implementation of a cell tag cache.
"""

import ast
import asyncio
import io
import re
import sys
import types
import typing
import unittest
from dataclasses import dataclass
from typing import Any, Mapping, Set, Union

import IPython.core.ultratb
import ipywidgets
from IPython.core.interactiveshell import ExecutionResult, InteractiveShell
from IPython.core.magic import Magics, cell_magic, magics_class
from IPython.display import HTML

from .analysis import TopLevelDefines
from .templ import templ
from .transforms import RewriteVariableAssignments
from .unit import AsyncFunctionTestCase, NotebookResult, NotebookTestRunner, NotebookTestSuite

nbtest_attrs = {}
runner_class = NotebookTestRunner
_last_succeeded = None


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
        global nbtest_attrs, _last_succeeded

        _last_succeeded = False
        runner_task = None

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
            _last_succeeded = False
            return HTML(templ.missing.render(missing=e))

        # Run the cell
        try:
            tree = ast.parse(cell)
            exec(compile(tree, filename="<testing>", mode="exec"), self._test_ns)
        except AssertionError as e:
            _last_succeeded = False
            return HTML(templ.assertion.render(error=e))

        # Look for async test cases.
        do_async = False
        funct_testcase = unittest.FunctionTestCase

        class async_visitor(ast.NodeVisitor):
            def visit_AsyncFunctionDef(_, node):
                nonlocal funct_testcase, do_async
                do_async = True
                funct_testcase = AsyncFunctionTestCase

        async_visitor().visit(tree)

        # Find and execute test cases.
        suite = NotebookTestSuite()
        loader = unittest.TestLoader()
        loader.suiteClass = NotebookTestSuite

        if self._test_ns["nbtest_cases"] is not None:
            # Test cases are specified
            for tc in self._test_ns["nbtest_cases"]:
                if isinstance(tc, str):
                    suite.addTest(loader.loadTestsFromName(tc))
                elif isinstance(tc, unittest.TestCase) or isinstance(tc, unittest.TestSuite):
                    suite.addTest(tc)
                elif isinstance(tc, type) and issubclass(tc, unittest.TestCase):
                    suite.addTest(loader.loadTestsFromTestCase(tc))
                elif isinstance(tc, types.ModuleType):
                    suite.addTest(loader.loadTestsFromModule(tc))
                elif isinstance(tc, types.FunctionType):
                    suite.addTest(funct_testcase(tc))
                else:
                    raise ValueError(f"""Invalid value in test_cases: {tc}.""")

        else:
            # Look for cases in the cell
            class top_test_visitor(ast.NodeVisitor):
                """Parse the input cell to find top level test class and test function defs"""

                def visit_ClassDef(_, node):
                    if isinstance(self._test_ns[node.name], type) and issubclass(
                        self._test_ns[node.name], unittest.TestCase
                    ):
                        suite.addTest(loader.loadTestsFromTestCase(self._test_ns[node.name]))
                    # do not descend

                def visit_FunctionDef(_, node):
                    if node.name.startswith("test") and callable(self._test_ns[node.name]):
                        suite.addTest(funct_testcase(self._test_ns[node.name]))
                    # do not descend

                def visit_AsyncFunctionDef(_, node):
                    if node.name.startswith("test") and callable(self._test_ns[node.name]):
                        suite.addTest(funct_testcase(self._test_ns[node.name]))
                    # do not descend

            top_test_visitor().visit(tree)

        if do_async:
            # Asyncronous execution. This has some problems.

            output = ipywidgets.Output()
            html = ipywidgets.HTML(templ.wait.render())
            output.append_display_data(html)

            async def do_run():
                global _last_succeeded
                nonlocal output
                try:
                    with output:
                        runner = NotebookTestRunner()
                        result = await runner.async_run(suite)
                        result = NotebookResult()
                        html.value = templ.result.render(result=result)
                        _last_succeeded = result.wasSuccessful()
                except Exception:
                    _last_succeeded = False
                    formatter = IPython.core.ultratb.AutoFormattedTB(
                        mode="Verbose", color_scheme="Linux"
                    )
                    output.append_stderr(formatter.text(*sys.exc_info()))

            runner_task = asyncio.create_task(do_run(), name="Test Runner")
            return output

        else:
            # Synchronous execution.
            runner = NotebookTestRunner()
            result = runner.run(suite)
            _last_succeeded = result.wasSuccessful()
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
                for x in self._docstring.split()
                if (m := re.match(r"(@\S+)", x)) is not None
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

    def _find(self, ntype: list[ast.AST]) -> dict[str, typing.Any]:
        """
        Find instances of specified definitions where:
            1. The definition happened at module scope, not inside a function or class.
            2. The symbol is defined in the notebook namespace.
        """
        tlds = TopLevelDefines()
        tlds.visit(self.tree)
        return {
            x[0]: self._shell.user_ns[x[0]]
            for x in tlds.defs
            if x[1] in ntype and x[0] in self._shell.user_ns
        }

    @property
    def functions(self) -> dict[str, types.FunctionType]:
        """
        Find all package level functions in the cell.
        """
        return self._find([ast.FunctionDef])

    @property
    def classes(self) -> dict[str, type]:
        """
        Find all package level class definitions in the cell.
        """
        return self._find([ast.ClassDef])

    @property
    def assignments(self) -> dict[str, typing.Any]:
        """
        Find all assigned variables in the cell.
        """
        return self._find([ast.Assign])

    @property
    def constants(self) -> set[typing.Any]:
        """
        Find all literal values in the cell.
        """
        return {x.value for x in ast.walk(self.tree) if x.__class__ == ast.Constant}

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
            save_idh = sys.displayhook
            save_edh = self._shell.user_ns["__builtins__"].display

            out = io.StringIO()
            err = io.StringIO()
            outputs = []
            result = None

            def explicit_displayhook(obj):
                nonlocal outputs
                if obj is not None:
                    outputs.append(obj)

            def implicit_displayhook(obj):
                nonlocal result, explicit_displayhook
                explicit_displayhook(obj)
                result = obj

            if capture:
                sys.stdout = out
                sys.stderr = err
                sys.displayhook = implicit_displayhook
                self._shell.user_ns["__builtins__"].display = explicit_displayhook

            transformer = RewriteVariableAssignments(*list(push.keys()))
            self._shell.ast_transformers.append(transformer)
            self._shell.run_cell(self._source, store_history=False, silent=False)

            if capture:
                return CellRunResult(
                    stdout=out.getvalue(),
                    stderr=err.getvalue(),
                    outputs=outputs,
                    result=result,
                )
            else:
                return None

        finally:
            sys.stdout = save_out
            sys.stderr = save_err
            sys.displayhook = save_idh
            self._shell.user_ns["__builtins__"].display = save_edh
            self._shell.ast_transformers.remove(transformer)
