#!/usr/bin/env python3.6
import argparse
import ast
import logging
import os
import sys

from collections import namedtuple

LOG = logging.getLogger(__name__)

ScopeEntry = namedtuple('ScopeEntry', ['c_name', 'c_type', 'py_name', 'py_type'])

class Scope(dict):
    def __init__(self, parent=None, prefix=None):
        self.prefix = prefix
        if parent:
            dict.__init__(self, parent)
        else:
            dict.__init__(self)

    def add_entry(self, py_name, py_type, c_name, c_type):
        self[py_name] = ScopeEntry(py_name=py_name, py_type=py_type, c_name=c_name, c_type=c_type)

    def c_name(self, name):
        if self.prefix:
            return '{}_DOT_{}'.format(self.prefix, name)
        else:
            return name

    def dict(self) -> dict:
        return dict(self)

BUILTIN = Scope()

class CompileError(RuntimeError): pass

class BaseCompiler(ast.NodeVisitor):
    def __init__(self, name, root, scope):
        self.name = name
        self.root = root
        self.scope = scope
        self.docstring = ''
        try:
            if type(root.body[0]) == ast.Str:
                self.docstring = root.body[0].s
        except AttributeError:
            pass

    def generic_visit(self, node):
        raise CompileError('unhandled visit: {}'.format(ast.dump(node)))

    def compiler(self) -> str:
        raise NotImplementedError()

    def declare_var(self, node: ast.AnnAssign) -> str:
        py_name = node.target.id
        py_type = node.annotation.id
        c_name = self.scope.c_name(py_name)
        if py_type == 'int':
            c_type = 'int32_t'
            def_value = '0'
        else:
            raise NotImplementedError('unhandled py_type: {}'.format(py_type))
        self.scope.add_entry(py_name=py_name, py_type=py_type, c_name=c_name, c_type=c_type)
        LOG.debug('set scope entry `%s` in compiler %s', py_name, self.name)
        return '{c_type} {c_name} = {def_value};'.format(
            c_type=c_type, c_name=c_name, def_value=def_value)


class LineCompiler(BaseCompiler):
    def visit_Return(self, ret_node) -> str:
        return 'return {};'.format(self.visit(ret_node.value))

    def visit_Num(self, num_node) -> str:
        return str(num_node.n)

    def visit_AnnAssign(self, node):
        py_name = node.target.id
        if py_name not in self.scope:
            raise CompileError('assignment to undeclared variable `{}` in scope {!r}'.format(py_name, self.scope))
        decl = self.scope[py_name]
        if decl.py_type == 'int':
            if type(node.value) != ast.Num:
                raise CompileError(
                    'assignment of non-numerical value {} to int variable `{}`'
                    .format(ast.dump(node), py_name))
            value = int(node.value.n)
        else:
            raise NotImplementedError('unhandled py_type: {}'.format(decl.py_type))
        return '{c_name} = {value};'.format(
            c_name=decl.c_name, value=value)

    def visit_Name(self, node):
        py_name = node.id
        if py_name not in self.scope:
            raise CompileError('unknown reference `{}`'.format(py_name))
        return self.scope[py_name].c_name

    def compile(self) -> str:
        c_src = self.visit(self.root)
        return c_src


class FuncCompiler(BaseCompiler):
    def compile(self) -> str:
        c_src = 'int {}() {{\n'.format(self.name)
        for node in self.root.body:
            line_name = '{}:{}'.format(node.lineno, node.col_offset)
            line_comp = LineCompiler(line_name, node, self.scope)
            line_c_src = line_comp.compile()
            c_src += '  ' + line_c_src + '\n'
            LOG.debug('line_c_src: %s', line_c_src)
        c_src += '}\n\n'
        return c_src


class ModuleCompiler(BaseCompiler):
    def compile(self) -> str:
        c_src = ''
        func_nodes = []
        other_nodes = []

        # Sort the body nodes by type (top-level code or functions)
        for node in self.root.body: 
            if type(node) == ast.FunctionDef:
                func_nodes.append(node)
            else:
                other_nodes.append(node)

        # Find all module level variable declarations
        for node in other_nodes:
            if type(node) != ast.AnnAssign:
                continue
            c_src += self.declare_var(node) + '\n'
        c_src += '\n'

        # Compile the top-level module code
        init_func_def = ast.FunctionDef(
            name='{}DOT__init__'.format(self.name),
            annotation=ast.Name(id='int'),
            body=other_nodes)
        init_func_compiler = FuncCompiler(init_func_def.name, init_func_def, self.scope)
        c_src += init_func_compiler.compile()

        return c_src
            

class ProgramCompiler(object):
    def __init__(self, name, py_src):
        self.name = name
        self.py_src = py_src

    def _pre_source(self) -> str:
        return '\n'.join(['#include <stdint.h>']) + '\n\n'

    def compile(self) -> str:
        # Use CPython's builtin source parser
        root = ast.parse(self.py_src)

        # The module exists in a new scope which inherits all builtin declarations
        main_scope = Scope(BUILTIN)
        
        # In the __main__ module, __main__ is self-referential
        main_scope['__main__'] = main_scope

        # Create a new compiler for the __main__ module
        main_comp = ModuleCompiler('__main__', root, main_scope)

        # Create and return C source for the application, which can be compiled
        # to binary form using gcc.
        c_src = self._pre_source()
        c_src += main_comp.compile()
        c_src += 'int main() {return __main__DOT__init__();}'
        return c_src


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('sourcefile', type=argparse.FileType('r'))
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.DEBUG)
    args = parse_args()
    py_src = args.sourcefile.read()
    module_name = os.path.basename(args.sourcefile.name)
    prog_compiler = ProgramCompiler(module_name, py_src)
    print(prog_compiler.compile())
    return os.EX_OK


if __name__ == '__main__':
    sys.exit(main())
