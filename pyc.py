#!/usr/bin/env python3.6
import argparse
import ast
import logging
import os
import sys

LOG = logging.getLogger(__name__)

# These functions are called in python by name on left and in C name by name
# on right.
BUILTIN_FUNCS = {
    'print': ast.parse('def printf(s: str): pass').body[0],
}

# Define some constants for the names of python types
PYTYPE_INT = int
CTYPE_INT = 'int32_t'

PYTYPE_STR = str
CTYPE_STR = 'char*'

PYTYPE_NONE = type(None)
CTYPE_NONE = 'void'

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


class Scope(object):
    def __init__(self, parent=None, locals={}):
        self.parent = parent
        self.locals = locals

    def get(self, pyname):
        if pyname in self.locals:
            return self.locals[pyname]
        if self.parent:
            return self.parent.get(pyname)

    def set(self, pyname, pyvalue):
        self.locals[pyname] = pyvalue

    def contains(self, key):
        if key in self.locals:
            return True
        if self.parent:
            return self.parent.contains(key)
        return False


class PyType(object):
    def __init__(self, pytype, ctype):
        self._pytype = pytype
        self._ctype = ctype

    def pytype(self):
        return self._pytype

    def ctype(self):
        return self._ctype


IntType = PyType(PYTYPE_INT, CTYPE_INT)
StrType = PyType(PYTYPE_STR, CTYPE_STR)

class FuncType(PyType):
    def __init__(self, args):
        self.args = args

def pytype_from_str(type_name):
    if type_name == 'int':
        return StrType
    if type_name == 'str':
        return IntType
    raise LookupError()

class FunctionCompiler(ast.NodeVisitor):
    def __init__(self, module_name, module_compiler, node):
        self.module_name = module_name
        self.module_compiler = module_compiler
        self.node = node
        self.scope = Scope(parent=module_compiler.scope)

    def _fn_ret_ctype(self, fn: ast.FunctionDef):
        # It is okay for functions to lack annotations for return types, but
        # only if they do not contain any return statement
        if self.node.returns == None:
            for fn_node in ast.walk(self.node):
                if type(fn_node) == ast.Return:
                    raise CompileError(
                        'missing return type annotation for function `{}`'
                        .format(self.node.name), self.node)
            else:
                return BUILTIN_TYPES[PYTYPE_NONE]
        else:
            # For functions with return type annotations, determine the C type
            # for the annotated python type
            try:
                return self._ctype(self.node.returns)
            except LookupError:
                raise CompileError(
                    'unsupported return type `{}` for function `{}`'
                    .format(ast.dump(self.node.returns), fn_name),
                    self.node)

    def compile(self):
        LOG.debug('Compiling: ' + ast.dump(self.node))

        # Check for use of decorators, which is not supported
        if type(self.node) == ast.FunctionDef and self.node.decorator_list:
            raise CompileError(
                'function decorators are not supported',
                self.node.decorator_list[0])

        # Define a local variable for each argument this function
        # will receive at runtime.
        if type(self.node) == ast.FunctionDef:
            # This is confusing for sure...here is an example data structure:
            # FunctionDef(name='main', args=arguments(args=[arg(arg='x', annotation=None), ...
            # See the docs on ast.FunctionDef, ast.arguments, ast.args, and ast.arg
            for arg in self.node.args.args:
                raise RuntimeError(ast.dump(arg.annotation))
                self.scope.set(arg.arg, arg.annotation)

        # Generate C source for each AST node under this function
        src = ''
        for body_node in self.node.body:
            node_src = self.visit(body_node)
            LOG.debug('source for node %s: %r', body_node, node_src)
            if node_src:
                src += node_src + '\n'

        # Get the return type for this function
        if type(self.node) != ast.Module:
            ret_type = self._fn_ret_ctype(self.node)
            if ret_type == None:
                raise CompileError(
                    'unable to determine return type of function `{}`'
                    .format(self.node.name), self.node)
        else:
            # Modules always return int32
            ret_type = CTYPE_INT

        # Convert the arg specifications for this function/module into a C
        # function signature. Modules are just code blocks, so they too are
        # implemented as functions in C.
        if type(self.node) == ast.Module:
            # Modules have no parameters
            args_src = ''
        else:
            # Convert each argument individually to C source, and then join
            # them all together into a function signature.
            c_args = []
            for arg in self.node.args.args:
                arg_name = arg.arg

                # Ensure the type was annotated for this argument
                if not arg.annotation:
                    raise CompileError(
                        'missing type annotation for parameter `{}`'
                        .format(arg_name), arg)

                # Using the annotation, sort out which C type should be used
                # for this argument.
                try:
                    arg_ctype = self._ctype(arg.annotation)
                except LookupError:
                    raise CompileError(
                        'unknown type `{}` for argument `{}`'
                        .format(ast.dump(arg.annotation), arg_name), arg)

                # Add the C source for this argument to a list, which is later
                # used to build the function signature.
                c_args.append('{} {}'.format(arg_ctype, arg_name))

            # Join the individual C source for each argument into one string
            args_src = ', '.join(c_args)

        # Sort out what the name of this C functon will be. Modules need a
        # special format, but regular functions are a bit simpler.
        if type(self.node) == ast.Module:
            fn_cname = MOD_PREFIX + self.module_name + MOD_INIT_SUFFIX
        else:
            fn_cname, _ = self._load_name(self.node)

        return '{ret_type} {fn_cname}({args}) {{\n{body}}}\n'.format(
            ret_type=ret_type, fn_cname=fn_cname, args=args_src, body=src)

    def generic_visit(self, node):
        # This function is called by ast.visit() if there is no such visit_XXX
        # function matching the type of the node in the AST tree being visited.
        # In other words, this compiler has no function implemented to handle
        # the node being passed in. There's nothing to do here except raise an
        # error about how there is a missing function in the compiler.
        LOG.error('Encountered unsupported node: %r', node)
        raise CompileError(
            'Unsupported ast node: {}'.format(ast.dump(node)), node)

    def visit_Attribute(self, node: ast.Attribute):
        """Resolve refrences to attributes of objects (such as a.b.c)"""
        return '{}.{}'.format(self.visit(node.value), node.attr)

    def visit_If(self, node: ast.If):
        """Return the C representation of a python if statement"""
        # Compile the test part of the if block into C code. This is the part
        # coming directly after "if", which tests some boolean case.
        test_src = self.visit(node.test)

        # Compile the body to C source. This is the part executed when the test
        # evaluates to True.
        body_src = ''
        for body_node in node.body:
            body_src += self.visit(body_node)

        # Compile the orelse to C source. This is the part executed when the
        # test evaluates to False.
        if node.orelse:
            orelse_src = ''
            for orelse_node in node.orelse:
                orelse_src += self.visit(orelse_node)

            # Return all compiled source in an if...else... format
            return 'if ({test}) {{\n{body}\n}} else {{\n{orelse}\n}}'.format(
                test=test_src, body=body_src, orelse=orelse_src)
        else:
            # If there wasn't an else block, return a simpler format
            return 'if ({test}) {{\n{body}\n}}'.format(
                test=test_src, body=body_src)

    def visit_Eq(self, node: ast.Eq):
        return '=='

    def visit_Gt(self, node: ast.Gt):
        return '>'

    def visit_Lt(self, node: ast.Lt):
        return '<'

    def visit_BoolOp(self, node: ast.BoolOp):
        return ' {} '.format(self.visit(node.op)).join([
            '({})'.format(self.visit(v)) for v in node.values])

    def visit_BinOp(self, node: ast.BinOp):
        left_src = self.visit(node.left)
        right_src = self.visit(node.right)
        op_src = self.visit(node.op)
        return '{left} {op} {right}'.format(
            left=left_src, op=op_src, right=right_src)

    def visit_Add(self, node: ast.Add):
        return '+'

    def visit_And(self, node: ast.And):
        return '&&'

    def visit_Or(self, node: ast.Or):
        return '||'

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

        # String comparison is a different matter in C
        if left_type == BUILTIN_TYPES[PYTYPE_STR]:
            # Ensure exactly two items are being compared (left + 1 comparator)
            if len(node.comparators) != 1:
                raise CompileError(
                    'string comparisons must be of exactly two items', node)

            # Determine which strcmp() return value to expect
            op = node.ops[0]
            comp = '=='
            if type(op) == ast.Eq:
                expect = 0
            elif type(op) == ast.NotEq:
                expect = 0
                comp = '!='
            elif type(op) == ast.Lt:
                expect = -1
            elif type(op) == ast.Gt:
                expect = 1
            else:
                raise CompileError(
                    'unsupported string comparison op: {}'
                    .format(ast.dump(op)), node)

            return 'strcmp({}, {}) {} {}'.format(
                self.visit(node.left), self.visit(node.comparators[0]), comp,
                expect)
        else:
            # For non-string comparisons just translate the symbols
            parts = []
            parts.append(self.visit(node.left))
            for op, cmp in zip(node.ops, node.comparators):
                parts.append(self.visit(op))
                parts.append(self.visit(cmp))
            LOG.debug('compare parts: %r', parts)
            return ' '.join(parts)

    def visit_Call(self, node:ast.Call):
        # TODO: Check that the function exists
        # TODO: Check arguments
        cargs = []
        for arg in node.args:
            cargs.append(self.visit(arg))
        return '{}({})'.format(node.func.id, ', '.join(cargs))

    def visit_Str(self, node:ast.Str):
        """Return the C representation of a python string"""
        # Convert strings to hex byte arrays, and include a null termination
        return '(const char[]){{{}}}'.format(
            ', '.join([hex(ord(c)) for c in node.s + '\0']))

    def visit_Name(self, node:ast.Name):
        """Returns the C name of a python variable"""
        # _load_name returns both the cname and the ast node, but we only need
        # the name.
        return node.id

    def visit_NameConstant(self, node: ast.NameConstant):
        """Returns the C name of a python constant"""
        # Check if the node refers to the python constant 'True'
        if node.value == True:
            return 'true'
        # Check if the node refers to the python constant 'False'
        elif node.value == False:
            return 'false'
        # There are certainly other constants with no implementation here, so
        # just raise an error
        else:
            raise CompileError(
                'could not compile constant `{}`'.format(ast.dump(node)), node)

    def visit_Num(self, node: ast.Num):
        """Return the C representation of a python numerical value"""
        # Numbers are represented just the same in C as they are in python, so
        # just convert to string and return the C representation
        return str(node.n)

    def visit_Return(self, node: ast.Return):
        """Return the C representation of a python 'return' statement"""
        # Ensure the value being returned matches the annotated type of this
        # function.
        # TODO: Check that the type is valid to return
        return 'return {};'.format(self.visit(node.value))

    def visit_Expr(self, node: ast.Expr):
        """Return the C representation of a python expression"""
        return self.visit(node.value) + ';'

    def visit_Import(self, node: ast.Import):
        LOG.debug(ast.dump(node))
        for alias in node.names:
            return '#include "{}.h"\n'.format(alias.name)

    def visit_Pass(self, node: ast.Pass):
        # Unlike python, no special keywords are required for a NOP body, so we
        # don't actually need to do anything here
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
        if  not self.scope.contains(target.id):
            raise CompileError(
                'Cannot assign to undeclared local var `{}`'
                .format(target.id), node)

        # Handle assingent of numerical constants to variables
        if type(node.value) == ast.Num:
            # ensure target is an int
            if self.scope.get(target.id).pytype() != PYTYPE_INT:
                raise CompileError(
                    'assignment of int to incompatible {} var {}'
                    .format(self.scope.get(target.id), target.id), node.value)

            # prevent float assignment
            if '.' in str(node.value.n):
                raise CompileError(
                    'assignment of float to incompatible {} var {}'
                    .format(self.scope.get(target.id), target.id), node.value)

            # output the code
            return '{} = {};\n'.format(target.id, node.value.n)

        # Handle assignment of string constants to variables
        elif type(node.value) == ast.Str:
            # ensure target is a string
            if self.scope[target.id] != PYTYPE_STR:
                raise CompileError(
                    'assignment of str to incompatible {} var `{}`'
                    .format(self.scope.get(target.id), target.id), node.value)

            # output the code
            return '{} = "{}";\n'.format(
                target.id, node.value.s.replace('"', '\\"'))
        else:
            raise CompileError(
                'Unsupported assignment of type `{}` to `{}` of type `{}`'
                .format(type(node.value), target.id, self.scope.get(target.id)),
                node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        # Sort out whether this is a new local declaration
        if self.scope.contains(node.target.id):
            raise CompilerError(
                'Local var `{}` has already been declared'
                .format(node.target.id))

        # TODO: Ensure the data types match for assignment

        # Store this declaration in the locals table
        # TODO: Do not assume node.annotation has an `id` attr
        self.scope.set(node.target.id, pytype_from_str(node.annotation.id))

        # Generate C code for the assignment
        target_src = self.visit(node.target)
        value_src = self.visit(node.value)
        return 'int {} = {};'.format(target_src, value_src)


class ModuleCompiler(ast.NodeVisitor):
    def __init__(self, module_name, source_filename, node, dunder_name):
        self.module_name = module_name
        self.scope = Scope()
        self.node = node
        self.source_filename = source_filename
        self.__name__ = dunder_name

        # Declare __name__ as a string global
        self.scope.set('__name__', StrType)

    def _initial_module_source(self):
        return '\n'.join([
            '#include <stdint.h>',
            '#define true 1',
            '#define false 0',
            'extern int strcmp(const char*, const char*);',
            'extern int printf(const char*, ...);',
            '#define {prefix}{mod_name}{DOT}__name__ "{dunder_name}"'.format(
                prefix=MOD_PREFIX, mod_name=self.module_name, DOT=DOT,
                dunder_name=self.__name__),
        ]) + '\n\n'

    def generic_visit(self, node):
        raise CompileError(
            'No matching compiler handler for node {!r}'
            .format(node), node)

    def compile(self):
        # Store the generated C source code for program in one big string
        src = ''

        # Look for import statements
        for node in ast.walk(self.node):
            if type(node) != ast.Import:
                continue

            # Look at what module is being imported
            for alias in node.names:
                # If the import isn't the form of "import ... as ..." then
                # juse use the normal name for the missing "as ..."
                asname = alias.asname if alias.asname else alias.name

                # Ensure the asname isn't taken
                if self.scope.contains(asname):
                    raise CompileError(
                        'cannot import `{}` multiple times'.format(
                            asname), node)

                # Look for the module relative to the current working dir
                filename = alias.name.replace('.', '/') + '.py'

                # Open the file
                with open(filename) as fh:
                    # Parse the root node using the ast module
                    root_node = ast.parse(fh.read())

                # Create a compiler for this module
                compiler = ModuleCompiler(
                    alias.name, filename, root_node, asname)

                # Ensure the module is initialized later
                imported_modules.append(asname)

                # Expose that module as a global in this module
                self.scope.set(asname, ModuleType(scope=compiler.scope))

                # Compile the source and add it to the current source string
                src += compiler.compile()

        # Add some pre-code for the module
        src += self._initial_module_source()

        # Keep track of compilers for the functions in this module
        func_compilers = []

        # Build a compiler for the top-level function
        top_func_compiler = FunctionCompiler(self.module_name, self, self.node)
        func_compilers.append(top_func_compiler)

        # Build a compiler for all other functions
        for mod_node in self.node.body:
            if type(mod_node) == ast.FunctionDef:
                self.scope.set(mod_node.name, FuncType(args=mod_name.args))
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

        # Return the generated C source code
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

    # Add a main() fn, calling the first module given on the CLI
    # TODO: initialize imported functions in main() or somewhere similar
    src += 'int main() {{return {}{}{}();}}\n'.format(
        MOD_PREFIX, args.input_modules[0], MOD_INIT_SUFFIX)

    print(src)
    return os.EX_OK


if __name__ == '__main__':
    sys.exit(main())
