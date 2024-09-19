"""
Helpers that analyze code cells.
"""

import ast
import types
import typing


class AnalysisNode:
    """
    An API for simple syntax analysis.
    """

    def __init__(self, tree, source):
        """
        Create an analysis node with optional source code.

        tree: The parse tree for this node (may be a subtree)
        source: The source corresponding to tree
        """

        self._tree = tree
        self._source = source

    @property
    def source(self) -> str:
        """The source corresponding to the tokens in tree."""
        src = ast.get_source_segment(self._source, self._tree)
        if src is None:
            return self._source
        else:
            return src

    @property
    def tree(self) -> ast.Module:
        """The parse tree generated from parsing the cell source. See ast.parse()"""
        if self._tree.__class__ == MarkerNode:
            # Don't show users my fake node.
            return self._tree._real_node
        else:
            return self._tree

    @property
    def docstring(self) -> typing.Union[str, None]:
        """The docstring of the source or `None` if there is no docstring."""
        if self._tree.__class__ == MarkerNode:
            return ast.get_docstring(self._tree._real_node)
        return ast.get_docstring(self._tree)

    @property
    def tokens(self) -> set:
        """A set of token classes from the parsed source."""
        return set((x.__class__ for x in ast.walk(self._tree)))

    @property
    def functions(self) -> dict[str, types.FunctionType]:
        """
        Find all package level functions in the cell.
        """
        found = {}

        class FindFunctions(RootNodeFinder):
            """
            A visitor that finds function definitions at the top level of the tree.
            """

            def visit_FunctionDef(_, node: ast.FunctionDef):
                found[node.name] = AnalysisNode(MarkerNode(node), self._source)

            def visit_AsyncFunctionDef(_, node: ast.FunctionDef):
                found[node.name] = AnalysisNode(MarkerNode(node), self._source)

        finder = FindFunctions()
        finder.visit(self._tree)
        return found

    @property
    def classes(self) -> dict[str, type]:
        """
        Find all package level class definitions in the cell.
        """
        found = {}

        class FindClasses(RootNodeFinder):
            """
            A visitor that finds class definitions at the top level of the tree.
            """

            def visit_ClassDef(_, node: ast.ClassDef):
                found[node.name] = AnalysisNode(MarkerNode(node), self._source)

        finder = FindClasses()
        finder.visit(self._tree)
        return found

    @property
    def assignments(self) -> dict[str, typing.Any]:
        """
        Find all assigned variables in the cell.
        """
        found = []

        class FindAssignments(RootNodeFinder):
            def visit_Name(self, node: ast.Name):
                if isinstance(node.ctx, ast.Store):
                    found.append(node.id)

        finder = FindAssignments()
        finder.visit(self._tree)
        return set(found)

    @property
    def constants(self) -> set[typing.Any]:
        """
        Find all literal values in the cell.
        """
        found = []

        class FindConstants(RootNodeFinder):
            def visit_Constant(self, node: ast.Constant):
                found.append(node.value)

        finder = FindConstants()
        finder.visit(self._tree)
        return set(found)

    @property
    def calls(self) -> set[str]:
        """
        Find all function calls.
        """
        found = []

        class FindConstants(RootNodeFinder):
            def visit_Call(self, node: ast.Call):
                found.append(node.func.id if hasattr(node.func, "id") else node.func.attr)

        finder = FindConstants()
        finder.visit(self._tree)
        return set(found)

    @property
    def arguments(self) -> set[str]:
        """
        If the current node is a function definition, return the set of the arguments.
        """
        found = []

        t = self.tree
        if t.__class__ not in (ast.FunctionDef, ast.AsyncFunctionDef):
            raise ValueError("Cannot call arguments on a non-function.")

        for arg in t.args.posonlyargs + t.args.args + t.args.kwonlyargs:
            found.append(arg.arg)

        if t.args.vararg is not None:
            found.append(f"*{t.args.vararg.arg}")

        if t.args.kwarg is not None:
            found.append(f"**{t.args.kwarg.arg}")

        return set(found)


class MarkerNode(ast.AST):
    """
    A fake Module node to use as the root of a tree that is rooted on a ast.Class or ast.Function
    node. Using this as the root makes the visitors that refuse to descend into function or
    class definitions just work.
    """

    def __init__(self, root: ast.AST):
        """
        Initialize a marker node from a new root.
        """
        self._real_node = root

        # Preserve source information
        self.lineno = root.lineno
        self.end_lineno = root.end_lineno
        self.col_offset = root.col_offset
        self.end_col_offset = root.end_col_offset

        self._fields = ["body"]
        self.body = root.body


class RootNodeFinder(ast.NodeVisitor):
    """
    A visitor that does not descend into class or function definitions.
    """

    def visit_ClassDef(self, node: ast.ClassDef):
        pass

    def visit_FunctionDef(self, node: ast.FunctionDef):
        pass

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        pass
