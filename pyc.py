#!/usr/bin/env python
import argparse
import ast
import logging
import os
import sys

LOG = logging.getLogger(__name__)

BUILTIN_FUNCS = {
    'print': ast.parse('def printf(s: str): pass').body[0],
}

BUILTIN_TYPES = {
    'int': 'int32_t',
    'str': 'char*',
}

# A static prefix/suffix for module level things
MOD_PREFIX = 'PYMOD_'
MOD_INIT_SUFFIX = '_INIT'

# How are dots from python references represented in C?
DOT = '_DOT_'

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

    def _ctype(self, node):
        """Distill an ast node down the C type which it will evaluate to

        This is actually quite tricky, as a node can be a function call,
        a reference to a local or global variable, a constant, etc.
        """
        LOG.debug('determining type of %r', node)
        LOG.debug('determining type of %s', ast.dump(node))
        # For function calls, use the function definition of the callee
        if type(node) == ast.Call:
            return self._ctype(node.func)

        # For function definitions use the return type annotated
        if type(node) == ast.FunctionDef:
            return self._ctype(node.returns)

        # If a variable was declared with an annotated assignment return the
        # type annotated at the time of assignment
        if type(node) == ast.AnnAssign:
            return self._ctype(node.annotation)

        # If a const value is passed in return the C type for the python type
        if type(node) == ast.Num:
            return BUILTIN_TYPES['int']
        if type(node) == ast.Str:
            return BUILTIN_TYPES['str']

        # If the node passed in is a reference
        if type(node) == ast.Name:
            # If the reference is to a builtin type, return that type
            if node.id in BUILTIN_TYPES:
                return BUILTIN_TYPES[node.id]

            # Load the declaration for the variable referenced and return the
            # storage type annotated at the time of declaration
            _, val = self._load_name(node)
            return self._ctype(val)

        # Look for references to None
        if type(node) == ast.NameConstant and node.value == None:
            return 'void'

        # Nothing was found (probably a bug)
        raise LookupError(
            'BUG: cannot determine C type for {}'.format(ast.dump(node)))

    def _cvalue(self, value):
        if type(value) == ast.Str:
            return '"{}"'.format(value.s.replace('"', '\\"'))
        if type(value) == ast.Num:
            return str(value.n)
        raise ValueError()

    def _fn_ret_ctype(self, fn: ast.FunctionDef):
        if self.node.returns == None:
            for fn_node in ast.walk(self.node):
                if type(fn_node) == ast.Return:
                    raise CompileError(
                        'missing return type annotation for function `{}`'
                        .format(self.node.name), self.node)
            else:
                return 'void'
        else:
            try:
                return self._ctype(self.node.returns)
            except LookupError:
                raise CompileError(
                    'unsupported return type `{}` for function `{}`'
                    .format(ast.dump(self.node.returns), fn_name),
                    self.node)

    def compile(self):
        LOG.debug('Compiling: ' + ast.dump(self.node))

        # Fill the locals with parameters passed into the function
        if type(self.node) == ast.FunctionDef:
            # This is confusing for sure...here is an example data structure:
            # FunctionDef(name='main', args=arguments(args=[arg(arg='x', annotation=None), ...
            # See the docs on ast.FunctionDef, ast.arguments, ast.args, and ast.arg
            for arg in self.node.args.args:
                self.locals[arg.arg] = arg

        src = ''
        for body_node in self.node.body:
            node_src = self.visit(body_node)
            LOG.debug('source for node %s: %r', body_node, node_src)
            if node_src:
                src += node_src + '\n'

        # Check for use of decorators, which is not supported
        if type(self.node) == ast.FunctionDef and self.node.decorator_list:
            raise CompileError(
                'function decorators are not supported',
                self.node.decorator_list[0])

        # Get the return type for this function
        if type(self.node) != ast.Module:
            ret_type = self._fn_ret_ctype(self.node)
            if ret_type == None:
                raise CompileError(
                    'unable to determine return type of function `{}`'
                    .format(self.node.name), self.node)
        else:
            # Modules always return int32
            ret_type = 'int32_t'

        # Convert the arg specifications
        if type(self.node) == ast.Module:
            # Modules have no parameters
            args_src = ''
        else:
            c_args = []
            for arg in self.node.args.args:
                arg_name = arg.arg

                # Ensure the type was annotated for this argument
                if not arg.annotation:
                    raise CompileError(
                        'missing type annotation for parameter `{}`'
                        .format(arg_name), arg)

                try:
                    arg_ctype = self._ctype(arg.annotation)
                except LookupError:
                    raise CompileError(
                        'unknown type `{}` for argument `{}`'
                        .format(ast.dump(arg.annotation), arg_name), arg)
                c_args.append('{} {}'.format(arg_ctype, arg_name))

            args_src = ', '.join(c_args)

        if type(self.node) == ast.Module:
            fn_name = MOD_PREFIX + self.module_name + MOD_INIT_SUFFIX
        else:
            fn_name, _ = self._load_name(self.node)

        # TODO: Create pre_src with #include stmts
        pre_src = ''
        return '{pre_src}\n{ret_type} {fn_name}({args}) {{\n{body}}}\n'.format(
            pre_src=pre_src, ret_type=ret_type, fn_name=fn_name, args=args_src,
            body=src,
        )

    def generic_visit(self, node):
        LOG.error('Encounteder unsupported node: %r', node)
        raise CompileError(
            'Unsupported ast node: {}'.format(ast.dump(node)), node)

    def _load_name(self, node):
        if type(node) == ast.FunctionDef:
            lookup_name = node.name
        else:
            lookup_name = node.id

        if lookup_name in self.locals:
            # items at the local scope have the same variable name
            return [lookup_name, self.locals[lookup_name]]
        elif lookup_name in self.module_compiler.globals:
            # items resolving at the module level have the module name
            # prefixed in C
            return [
                '{}{}{}{}'.format(
                    MOD_PREFIX, self.module_name, DOT, lookup_name),
                self.module_compiler.globals[lookup_name]
            ]
        elif lookup_name in BUILTIN_FUNCS:
            # builtints preserve the name
            return BUILTIN_FUNCS[lookup_name].name, BUILTIN_FUNCS[lookup_name]
        else:
            raise LookupError()

    def visit_If(self, node: ast.If):
        """Return the C representation of a python if statement"""
        test_src = self.visit(node.test)

        body_src = ''
        for body_node in node.body:
            body_src += self.visit(body_node)

        orelse_src = ''
        for orelse_node in node.orelse:
            orelse_src += self.visit(orelse_node)

        return 'if ({test}) {{\n{body}\n}} else {{\n{orelse}\n}}'.format(
            test=test_src, body=body_src, orelse=orelse_src
        )

    def visit_Eq(self, node:ast.Eq):
        return '=='

    def visit_Compare(self, node:ast.Compare):
        """Return the C representation of comparison tests"""
        # Find the C type of the left value
        _, left_val = self._load_name(node.left)
        left_type = self._ctype(left_val)

        # Find the C type of the right values and ensure they are same as left
        for cmp in node.comparators:
            # Lookup ast.Name nodes first, then convert their value to a type
            if type(cmp) == ast.Name:
                _, right_val = self._load_name(cmp)
                right_type = self._ctype(right_val)
            else:
                # For other things like ast.Str and ast.Num just convert to a
                # type directly
                right_type = self._ctype(cmp)

            # Enforce that the two are the same C type
            if left_type != right_type:
                raise CompileError('mismatched types in comparison', node.left)

        parts = []
        parts.append(self.visit(node.left))
        for op, cmp in zip(node.ops, node.comparators):
            parts.append(self.visit(op))
            parts.append(self.visit(cmp))
        LOG.debug('compare parts: %r', parts)
        return ' '.join(parts)

    def visit_Call(self, node:ast.Call):
        # Find the function being called
        try:
            func_name, func = self._load_name(node.func)
        except LookupError:
            raise CompileError(
                'reference to unknown function `{}`'.format(node.func.id),
                node)

        # Ensure the function being called is infact a function
        if type(func) != ast.FunctionDef:
            raise CompileError(
                'call to non-function `{}` of type `{}`'
                .format(node.func.id, func), node)

        # TODO: Check arguments
        cargs = []
        for arg in node.args:
            cargs.append(self.visit(arg))
        return '{}({})'.format(func_name, ', '.join(cargs))

    def visit_Str(self, node:ast.Str):
        """Return the C representation of a python string"""
        # C strings need to have double quote escaped with backslash
        return '"{}"'.format(node.s.replace('"', '\\"'))

    def visit_Name(self, node:ast.Name):
        """Returns the C name of a python variable"""
        # _load_name returns both the cname and the ast node, but we only need
        # the name.
        cname, _ = self._load_name(node)
        return cname

    def visit_Num(self, node: ast.Num):
        """Return the C representation of a python numerical value"""
        # Numbers are represented just the same in C as they are in python, so
        # just convert to string and return the C representation
        return str(node.n)

    def visit_Return(self, node: ast.Return):
        """Return the C representation of a python 'return' statement"""
        return 'return {};'.format(self.visit(node.value))

    def visit_Expr(self, node: ast.Expr):
        """Return the C representation of a python expression"""
        return self.visit(node.value) + ';'

    def visit_Import(self, node: ast.Import):
        LOG.debug(ast.dump(node))
        for alias in node.names:
            return '#include "{}.h"\n'.format(alias.name)

    def visit_Pass(self, node: ast.Pass):
        pass

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if type(self.node) != ast.Module:
            raise CompileError('Inner functions are not supported', node)

    def visit_Assign(self, node: ast.Assign):
        # Python allows multiple assignments on one line, but that
        # isn't implemented here yet.
        if len(node.targets) > 1:
            raise CompileError(
                'Use of unsupported feature: multiple assignment', node.targets)
        target = node.targets[0]

        # Do not support attribute assignment (object.x = 123)
        if type(target) == ast.Attribute:
            raise CompileError(
                'Use of unsupported feature: attribute assignment', node)

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
            return '{} = {};\n'.format(target.id, node.value.n)

        # Handle assignment of string constants to variables
        elif type(node.value) == ast.Str:
            # ensure target is a string
            if self.locals[target.id] != 'str':
                raise CompileError(
                    'assignment of str to incompatible {} var `{}`'
                    .format(self.locals[target.id], target.id), node.value)

            # output the code
            return '{} = "{}";\n'.format(
                target.id, node.value.s.replace('"', '\\"'))
        else:
            raise CompileError(
                'Unsupported assignment of type `{}` to `{}` of type `{}`'
                .format(type(node.value), target.id, self.locals[target.id]),
                node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        # Sort out whether this is a new local declaration
        if node.target.id in self.locals:
            raise CompilerError(
                'Local var `{}` has already been declared'
                .format(node.target.id))

        # Sort out the C storage type
        if node.annotation.id == 'int':
            ctype = self._ctype(node.annotation)
            var_src = '{} {}'.format(ctype, node.target.id)

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

        self.locals[node.target.id] = node.annotation.id
        return var_src


class ModuleCompiler(ast.NodeVisitor):
    def __init__(self, module_name, source_filename, node, dunder_name):
        self.module_name = module_name
        self.globals = {}
        self.node = node
        self.source_filename = source_filename
        self.__name__ = dunder_name

        # Declare __name__ as a string global
        self.globals['__name__'] = ast.AnnAssign(annotation=ast.Name(id='str'))

    def _initial_module_source(self):
        return '\n'.join([
            '#include <stdint.h>',
            '#define {prefix}{mod_name}{DOT}__name__ "{dunder_name}"'.format(
                prefix=MOD_PREFIX, mod_name=self.module_name, DOT=DOT,
                dunder_name=self.__name__),
        ]) + '\n\n'

    def generic_visit(self, node):
        raise CompileError(
            'No matching compiler handler for node {!r}'
            .format(node), node)

    def compile(self):
        src = self._initial_module_source()
        func_compilers = []

        # Build a compiler for the top-level function
        top_func_compiler = FunctionCompiler(self.module_name, self, self.node)
        func_compilers.append(top_func_compiler)

        # Build a compiler for all other functions
        for mod_node in self.node.body:
            if type(mod_node) == ast.FunctionDef:
                self.globals[mod_node.name] = mod_node
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
    logging.basicConfig(level=logging.DEBUG)
    args = parse_args()
    src = ''
    for i, module_name in enumerate(args.input_modules):
        # Figure out what __name__ will be for this module. The first module
        # listed will be __main__.
        if i == 0:
            dunder_name = '__main__'
        else:
            dunder_name = module_name

        # Build a path name for the python module
        filename = module_name.replace('.', '/') + '.py'

        # Open parse, and compile the file
        with open(filename) as fh:
            # Parse the file using standard python parser which gives back a
            # data structure called an AST representing the code
            module = ast.parse(fh.read())

            # Using the returned AST generate C code
            compiler = ModuleCompiler(
                module_name, filename, module, dunder_name)

            # Add the generated C code to the project source
            src += compiler.compile()

    # Add a main() fn
    src += 'int main() {{return {}{}{}();}}\n'.format(
        MOD_PREFIX, args.input_modules[0], MOD_INIT_SUFFIX)

    print(src)
    return os.EX_OK


if __name__ == '__main__':
    sys.exit(main())
