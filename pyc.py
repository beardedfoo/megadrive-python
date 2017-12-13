#!/usr/bin/env python3.6
import argparse
import ast
import logging
import os
import sys

from collections import namedtuple

assert sys.version_info[:2] == (3, 6)

LOG = logging.getLogger(__name__)

ScopeEntry = namedtuple('ScopeEntry', ['name', 'type', 'callable'])
BiLangScopeEntry = namedtuple('BiLangScopeEntry', ['c', 'py'])

class Scope(dict):
    def __init__(self, parent=None, prefix=None):
        self.prefix = prefix
        if parent:
            dict.__init__(self, parent)
        else:
            dict.__init__(self)

    def add_entry(self, c, py):
        self[py.name] = BiLangScopeEntry(c=c, py=py)

    def suggest_c_name(self, py_name):
        if self.prefix:
            return '{}_DOT_{}'.format(self.prefix, py_name)
        else:
            return py_name

    def resolve(self, node):
        py_name_parts = []
        def add_node(sub_node):
            if type(sub_node) == ast.Attribute:
                add_node(sub_node.value)
                py_name_parts.append(sub_node.attr)
            elif type(sub_node) == ast.Name:
                py_name_parts.append(sub_node.id)
            else:
                raise NotImplementedError('cannot resolve from {}'.format(ast.dump(sub_node)))

        add_node(node)
        LOG.debug('py_name_parts: %r', py_name_parts)
        cur = self
        for py_name in py_name_parts:
            cur = cur[py_name]
        return cur

    def dict(self) -> dict:
        return dict(self)

BUILTIN = Scope()

SegaScope = Scope()
SegaScope.add_entry(
    c=ScopeEntry(name='VDP_init', type=None, callable=True),
    py=ScopeEntry(name='init', type=None, callable=True)
)

SegaScope.add_entry(
    c=ScopeEntry(name='VDP_drawText', type=None, callable=True),
    py=ScopeEntry(name='draw_text', type=None, callable=True)
)

SysScope = Scope()
SysScope.add_entry(
    c=ScopeEntry(name='exit', type=None, callable=True),
    py=ScopeEntry(name='exit', type=None, callable=True),
)

BUILTIN_MODS = {
    'sys': SysScope,
    'vdp': SegaScope,
}

main_scope = Scope(BUILTIN, prefix='MOD___main__')


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
        c_name = self.scope.suggest_c_name(py_name)
        if py_type == 'int':
            c_type = 'int32_t'
            def_value = '0'
        elif py_type == 'str':
            c_type = 'char*'
            def_value = 'NULL'
        else:
            raise NotImplementedError('unhandled py_type: {}'.format(py_type))

        # Register the new var in the scope
        self.scope.add_entry(
            py=ScopeEntry(name=py_name, type=py_type, callable=False),
            c=ScopeEntry(name=c_name, type=c_type, callable=False),
        )

        LOG.debug('set scope entry `%s` in scope %s', py_name, self.name)
        return '{c_type} {c_name} = {def_value};'.format(
            c_type=c_type, c_name=c_name, def_value=def_value)

    def py_type(self, node):
        if type(node) in [ast.Name, ast.Attribute]:
            var = self.scope.resolve(node)
            return var.py.type
        else:
            raise NotImplementedError('cannot get type of {}'.format(ast.dump(node)))


class LineCompiler(BaseCompiler):
    def visit_Return(self, ret_node: ast.Return) -> str:
        return 'return {}'.format(self.visit(ret_node.value))

    def visit_Num(self, num_node: ast.Num) -> str:
        return str(num_node.n)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> str:
        py_name = node.target.id
        if py_name not in self.scope:
            raise CompileError('assignment to undeclared variable `{}` in scope {!r}'.format(py_name, self.scope))

        decl = self.scope[py_name]
        if decl.py.type == 'int':
            if type(node.value) != ast.Num:
                raise CompileError(
                    'assignment of non-numerical value {} to int variable `{}`'
                    .format(ast.dump(node), py_name))
            value_src = self.visit(node.value)
        elif decl.py.type == 'str':
            if type(node.value) != ast.Str:
                raise CompileError(
                    'assignment of non-string value {} to str variable `{}`'
                    .format(ast.dump(node), py_name))
            value_src = self.visit(node.value)
        else:
            raise NotImplementedError('unhandled py_type: {}'.format(decl.py_type))
        return '{c_name} = {value_src}'.format(
            c_name=decl.c.name, value_src=value_src)

    def _name_error(self, name):
        raise CompileError('NameError: undefined reference `{}`'.format(name))

    def visit_Name(self, node: ast.Name) -> str:
        py_name = node.id

        if py_name not in self.scope:
            self._name_error(py_name)

        c_name = self.scope[py_name].c.name
        LOG.debug('c_name(%r) == %s', py_name, c_name)
        return c_name
    
    def visit_Attribute(self, node: ast.Attribute) -> str:
        if node.value.id in BUILTIN_MODS:
            attr_scope = BUILTIN_MODS[node.value.id]
            if node.attr not in attr_scope:
                self._name_error('{}.{}'.format(node.value.id, node.attr))
            return attr_scope[node.attr].c.name
        else:
            self._name_error(node.value.id)

    def visit_Import(self, node: ast.Attribute) -> str:
        src = ''
        for alias in node.names:
            # Skip modules with an internal implementation
            if alias.name in BUILTIN_MODS:
                continue
        return src

    def visit_Pass(self, node: ast.Pass) -> str:
        return '; // do nothing\n'

    def visit_NameConstant(self, node: ast.NameConstant) -> str:
        if node.value == True:
            return 'TRUE'
        raise NotImplementedError(ast.dump(node))

    def visit_While(self, node: ast.While) -> str:
        # Compile the test condition
        test_src = self.visit(node.test)

        # Compile the body
        body_src = ''
        for body_node in node.body:
            line_name = '{}:{}'.format(body_node.lineno, body_node.col_offset)
            line_comp = LineCompiler(line_name, body_node, self.scope)
            body_src += line_comp.compile()

        if node.orelse:
            raise NotImplementedError('while...else')

        return 'while ({test_src}) {{\n{body_src}\n}}'.format(test_src=test_src, body_src=body_src)

    def visit_If(self, node: ast.If) -> str:
        # Compile the test condition
        test_src = self.visit(node.test)

        # Compile the body
        body_src = ''
        for body_node in node.body:
            line_name = '{}:{}'.format(body_node.lineno, body_node.col_offset)
            line_comp = LineCompiler(line_name, body_node, self.scope)
            body_src += line_comp.compile()

        # Compile the orelse body
        orelse_src = ''
        for orelse_node in node.orelse:
            line_name = '{}:{}'.format(body_node.lineno, body_node.col_offset)
            line_comp = LineCompiler(line_name, body_node, self.scope)
            orelse_src += line_comp.compile()

        # Build the C version
        if node.orelse:
            return 'if ({test_src}) {{\n{body_src}\n}} else {{ {orelse_src} }}'.format(
                test_src=test_src, body_src=body_src, orelse_src=orelse_src)
        else:
            return 'if ({test_src}) {{\n{body_src}\n}}'.format(test_src=test_src, body_src=body_src)

    def visit_Compare(self, node: ast.Compare) -> str:
        left_py_type = self.py_type(node.left)
        if left_py_type == 'str':
            if len(node.ops) != 1:
                raise CompileError('string comparison is only valid against a single comparator')
            op = type(node.ops[0])
            comp = node.comparators[0]

            if op == ast.Eq:
                c_test = '== 0'
            elif op == ast.Lt:
                c_test = '== -1'
            elif op == ast.Gt:
                c_test = '== 1'
            elif op == ast.NotEq:
                c_test = '!= 0'
            else:
                raise CompileError('invalid string comparison operator {}'.format(ast.dump(op)))
            
            left_src = self.visit(node.left)
            right_src = self.visit(comp)
            return 'strcmp({}, {}) {}'.format(left_src, right_src, c_test)
        else:
            parts = [self.visit(node.left)]
            for op, comp in zip(node.ops, node.comparators):
                parts.append(self.visit(op))
                parts.append(self.visit(comp))
            return ' '.join(parts)

    def visit_Eq(self, node: ast.Eq) -> str:
        return '=='

    def visit_Str(self, node: ast.Str) -> str:
        return '"{}"'.format(str(node.s))

    def visit_Call(self, node: ast.Call) -> str:
        arg_src_parts = []
        for arg in node.args:
            arg_src_parts.append(self.visit(arg))
        args_src = ', '.join(arg_src_parts)
        return '{}({})'.format(self.visit(node.func), args_src)

    def visit_Expr(self, node: ast.Expr) -> str:
        src = self.visit(node.value)
        if node == self.root:
            return src
        else:
            return '(' + src + ')'

    def compile(self) -> str:
        c_src = self.visit(self.root)
        LOG.debug('compiled line:\n\t\t%s\n\n\tinto:\n\t\t%s', ast.dump(self.root), c_src)

        if type(self.root) in [ast.Expr, ast.AnnAssign]:
            c_src += ';'
        c_src += '\n'
        return c_src

class FuncCompiler(BaseCompiler):
    def compile(self) -> str:
        c_src = 'void {}() {{\n'.format(self.scope.suggest_c_name(self.name))
        for node in self.root.body:
            line_name = '{}:{}'.format(node.lineno, node.col_offset)
            line_comp = LineCompiler(line_name, node, self.scope)
            c_src += line_comp.compile() 
        c_src += '}\n\n'
        return c_src


class ModuleCompiler(BaseCompiler):
    def compile(self) -> str:
        # Add a var for __name__
        dunder_name_c_name = self.scope.suggest_c_name('__name__')
        self.scope.add_entry(
            c=ScopeEntry(name=dunder_name_c_name, type='const char*',
                         callable=False),
            py=ScopeEntry(name='__name__', type='str', callable=False),
        )
        c_src = 'const char* {} = "{}";\n'.format(dunder_name_c_name, self.name.replace('.', '_DOT_'))

        # Sort the body nodes by type (top-level code or functions)
        func_nodes = []
        other_nodes = []
        for node in self.root.body: 
            if type(node) == ast.FunctionDef:
                func_nodes.append(node)
            else:
                other_nodes.append(node)

        # Find all module level variable declarations
        for node in other_nodes:
            for sub_node in ast.walk(node):
                if type(sub_node) != ast.AnnAssign:
                    continue
                c_src += self.declare_var(sub_node) + '\n'
        c_src += '\n'

        # Compile the top-level module code
        init_func_def = ast.FunctionDef(
            name='__init__',
            annotation=ast.Name(id='int'),
            body=other_nodes)
        init_func_compiler = FuncCompiler(init_func_def.name, init_func_def, self.scope)
        c_src += init_func_compiler.compile()

        return c_src
            

class ProgramCompiler(object):
    def __init__(self, name, py_src, platform):
        self.platform = platform
        self.name = name
        self.py_src = py_src

    def _pre_source(self) -> str:
        if self.platform == 'unix':
            return '\n'.join([
                '#include <stdint.h>',
                '#include <string.h>',
                '#include <stdlib.h>',]) + '\n\n'
        elif self.platform == 'md':
            return '#include <genesis.h>\n\n'

    def compile(self) -> str:
        # Use CPython's builtin source parser
        root = ast.parse(self.py_src)
        module_name = '__main__'

        # Create a new compiler for the __main__ module
        main_comp = ModuleCompiler(module_name, root, main_scope)

        # Create and return C source for the application, which can be compiled
        # to binary form using gcc.
        c_src = self._pre_source()
        c_src += main_comp.compile()
        c_src += 'int main() {{{}(); return 0;}}'.format(
            main_scope.suggest_c_name('__init__'))
        return c_src


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('sourcefile', type=argparse.FileType('r'))
    p.add_argument('--platform', '-p', choices=['md', 'unix'], default='sega')
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.DEBUG)
    args = parse_args()
    py_src = args.sourcefile.read()
    module_name = os.path.basename(args.sourcefile.name)
    prog_compiler = ProgramCompiler(module_name, py_src, args.platform)
    print(prog_compiler.compile())
    return os.EX_OK


if __name__ == '__main__':
    sys.exit(main())
