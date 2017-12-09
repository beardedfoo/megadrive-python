#!/usr/bin/env python3.6
import argparse
import ast
import logging
import os
import sys

LOG = logging.getLogger(__name__)

class CompileError(RuntimeError): pass

class BaseCompiler(ast.NodeVisitor):
    def __init__(self, name, root):
        self.name = name
        self.root = root
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


class LineCompiler(BaseCompiler):
    def visit_Return(self, ret_node):
        return 'return {};'.format(self.visit(ret_node.value))

    def visit_Num(self, num_node):
        return str(num_node.n)

    def compile(self) -> str:
        c_src = self.visit(self.root)
        return c_src


class FuncCompiler(BaseCompiler):
    def compile(self) -> str:
        c_src = 'int {}() {{\n'.format(self.name)
        for node in self.root.body:
            line_comp = LineCompiler('{}:{}'.format(node.lineno, node.col_offset), node)
            line_c_src = line_comp.compile()
            c_src += '  ' + line_c_src + '\n'
            LOG.debug('line_c_src: %s', line_c_src)
        c_src += '}\n'
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

        # Compile the top-level module code
        init_func_def = ast.FunctionDef(
            name='{}DOT__init__'.format(self.name),
            body=other_nodes)
        init_func_compiler = FuncCompiler(init_func_def.name, init_func_def)
        c_src += init_func_compiler.compile()

        return c_src
            

class ProgramCompiler(object):
    def __init__(self, py_src):
        self.py_src = py_src

    def compile(self) -> str:
        root = ast.parse(self.py_src)
        main_comp = ModuleCompiler('__main__', root)
        c_src = ''

        c_src += main_comp.compile()
        c_src += 'int main() {return __main__DOT__init__();}'
        return c_src


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('sourcefile', type=argparse.FileType('r'))
    return p.parse_args()


def main():
    logging.basicConfig()
    args = parse_args()
    py_src = args.sourcefile.read()
    prog_compiler = ProgramCompiler(py_src)
    print(prog_compiler.compile())
    return os.EX_OK


if __name__ == '__main__':
    sys.exit(main())
