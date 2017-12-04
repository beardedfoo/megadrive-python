#!/usr/bin/env python
import argparse
import ast
import os
import sys


class CompileError(RuntimeError):
    def __init__(self, msg, node):
        if type(node) != ast.Module:
            self._msg = '{}:{} {}'.format(node.lineno, node.col_offset, msg)
        else:
            self._msg = msg

    def msg(self):
        return 'CompileError: {}'.format(self._msg)

    def __str__(self):
        return self._msg


class FunctionCompiler(ast.NodeVisitor):
    def __init__(self, module_name, module_compiler, node):
        self.module_name = module_name
        self.module_compiler = module_compiler
        self.node = node

        self.locals = {}
        self.src = ''
        self.pre_src = ''

    def compile(self):
        self.src = ''
        for body_node in self.node.body:
            self.visit(body_node)

        # Check for use of decorators
        if type(self.node) == ast.FunctionDef and self.node.decorator_list:
            raise CompileError(
                'function decorators are not supported',
                self.node.decorator_list[0])

        # Check for use of decorators
        if type(self.node) == ast.FunctionDef and self.node.returns == None:
            raise CompileError(
                'missing return type annotation for function `{}`'
                .format(self.node.name), self.node)

        if type(self.node) == ast.Module:
            return self.pre_src + '\nvoid ' + self.module_name + '() {\n' + self.src + '}\n'
        else:
            return self.pre_src + '\nvoid ' + self.module_name + '_' + self.node.name + '() {\n' + self.src + '}\n'

    def generic_visit(self, node):
        raise CompileError(
            'No matching compiler handler for node {!r}'
            .format(node), node)

    def visit_Import(self, node):
        print(ast.dump(node))
        for alias in node.names:
            self.pre_src += '#include "{}.h"\n'.format(alias.name)

    def visit_Pass(self, node):
        pass

    def visit_FunctionDef(self, node):
        if type(self.node) != ast.Module:
            raise CompileError('Inner functions are not supported', node)

    def visit_Assign(self, node):
        # Python allows multiple assignments on one line, but that
        # isn't implemented here yet.
        if len(node.targets) > 1:
            raise CompileError(
                'Multiple assignment is unsupported', node.targets)
        target = node.targets[0]

        # Do not support attribute assignment (object.x = 123)
        if type(target) == ast.Attribute:
            raise CompileError(
                'Assignment to attributes is not supported', node)

        # Ensure the variable has been declared
        if target.id not in self.locals:
            raise CompileError(
                'Cannot assign to undeclared local var `{}`'
                .format(target.id), node)

        # Handle assingent of numerical constants to variables
        if type(node.value) == ast.Num:
            # ensure target is an int
            if self.locals[target.id] != 'int':
                raise CompileError(
                    'assignment of int to incompatible {} var {}'
                    .format(self.locals[target.id], target.id), node.value)

            # prevent float assignment
            if '.' in str(node.value.n):
                raise CompileError(
                    'assignment of float to incompatible {} var {}'
                    .format(self.locals[target.id], target.id), node.value)

            # output the code
            self.src += '{} = {};\n'.format(target.id, node.value.n)

        # Handle assignment of string constants to variables
        elif type(node.value) == ast.Str:
            # ensure target is a string
            if self.locals[target.id] != 'str':
                raise CompileError(
                    'assignment of str to incompatible {} var `{}`'
                    .format(self.locals[target.id], target.id), node.value)

            # output the code
            self.src += '{} = "{}";\n'.format(
                target.id, node.value.s.replace('"', '\\"'))
        else:
            raise CompileError(
                "I don't know how to assign a {} to `{}`"
                .format(type(node.value), target.id), node.value)

    def visit_AnnAssign(self, node):
        # Sort out whether this is a new local declaration
        if node.target.id in self.locals:
            raise CompilerError(
                'Local var `{}` has already been declared'
                .format(node.target.id))

        # Sort out the C storage type
        if node.annotation.id == 'int':
            var_src = 'int32_t {}'.format(node.target.id)

            # Sort out whether any initial value is needed
            if type(node.value) == ast.Num:
                var_src += ' = {}'.format(int(node.value.n))

            # Other value types are unhandled
            elif node.value != None:
                raise CompileError(
                    'assignment expected type int, not {}'
                    .format(type(node.value)), node.value)
        elif node.annotation.id == 'str':
            var_src = 'char* {}'.format(node.target.id)
            if node.value:
                c_str = node.value.s
                var_src += ' = "{}"'.format(c_str.replace('"', '\\"'))

            # Other value types are unhandled
            elif node.value != None:
                raise CompileError(
                    'assignment expected type int, not {}'
                    .format(type(node.value)), node.value)
        else:
            raise NotImplementedError(
                'Unknown type {}'
                .format(type(node.annotation.id)), node.annotation)

        var_src += ';\n'

        self.src += var_src
        self.locals[node.target.id] = node.annotation.id


class ModuleCompiler(ast.NodeVisitor):
    def __init__(self, module_name, source_filename, node):
        self.module_name = module_name
        self.node = node
        self.source_filename = source_filename

    def generic_visit(self, node):
        raise CompileError(
            'No matching compiler handler for node {!r}'
            .format(node), node)

    def compile(self):
        src = ''
        func_compilers = []

        # Build a compiler for the top-level function
        top_func_compiler = FunctionCompiler(self.module_name, self, self.node)
        func_compilers.append(top_func_compiler)

        # Build a compiler for all other functions
        for mod_node in self.node.body:
            if type(mod_node) == ast.FunctionDef:
                func_compiler = FunctionCompiler(
                    self.module_name, self, mod_node)
                func_compilers.append(func_compiler)

        # Run the compilers
        try:
            for compiler in func_compilers:
                src += compiler.compile() + '\n'
        except CompileError as e:
            e._msg = '{}:{}'.format(self.source_filename, e._msg)
            raise

        return src


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        'input_modules', nargs='+', help='Python module names for compilation')
    return p.parse_args()


def main():
    args = parse_args()
    src = ''
    for module_name in args.input_modules:
        filename = module_name.replace('.', '/') + '.py'
        with open(filename) as fh:
            module = ast.parse(fh.read())
            compiler = ModuleCompiler(module_name, filename, module)
            src += compiler.compile()
    print(src)
    return os.EX_OK


if __name__ == '__main__':
    sys.exit(main())
